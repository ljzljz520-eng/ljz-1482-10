"""Media worker: turns a frozen project version into deliverables.

Outputs per channel
-------------------
* ``subtitles``  - SRT generated from clip captions on the real timeline
                   timing (cumulative clip durations).
* ``shot_list``  - CSV manifest: clip id, room binding, scope, asset version,
                   timing, essential prompts, safe-area status.
* ``contact_sheet`` - SVG proof of every clip with overlays and the channel
                   crop + safe-area guides drawn on top.
* ``preview``    - SVG storyboard (always); upgraded to MP4 when ffmpeg is
                   available.  Thumbnail proxies are never accepted as
                   footage, so a missing full-res source fails the job.

The worker RECOMPUTES the dependency report when it picks the job up.  A
queued job cannot survive a revoked license or a photo source going dead
between clicking "publish" and rendering.
"""
from __future__ import annotations

import csv
import io
import json
import os
import shutil
import subprocess
from datetime import date

from backend import domain, store
from backend.storage import OUTPUT_DIR, output_path


# --------------------------------------------------------------- timing

def clip_timeline(timeline: dict) -> list[dict]:
    """Attach start/end seconds to each clip from its duration (default 4s)."""
    t = 0.0
    out = []
    for item in timeline["items"]:
        dur = float(item.get("duration") or 4.0)
        out.append({**item, "_start": t, "_end": t + dur})
        t += dur
    return out


def _srt_time(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def build_srt(timed: list[dict]) -> str:
    buf = io.StringIO()
    n = 1
    for clip in timed:
        text = clip.get("caption", "").strip()
        if not text:
            continue
        buf.write(f"{n}\n{_srt_time(clip['_start'])} --> {_srt_time(clip['_end'])}\n{text}\n\n")
        n += 1
    return buf.getvalue()


def build_shot_list(timed: list[dict], channel_key: str, checks: dict) -> str:
    ch = domain.CHANNELS[channel_key]
    issue_map: dict[str, list[str]] = {}
    for iss in checks.get("channels", {}).get(channel_key, []):
        issue_map.setdefault(iss["clip_id"], []).append(f"{iss['code']}")
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["seq", "clip_id", "scope", "room_type", "space_label", "asset",
                "asset_version", "mode", "start", "end", "channel",
                "embedded_prompts", "safe_area_flags"])
    for i, clip in enumerate(timed, 1):
        w.writerow([
            i, clip["clip_id"], clip["scope"],
            clip.get("_frozen_room", {}).get("name", "") if clip.get("_frozen_room") else "",
            clip["_frozen_asset"]["space_label"], clip["_frozen_asset"]["title"],
            clip["_frozen_asset"]["version"], clip["mode"],
            f"{clip['_start']:.1f}", f"{clip['_end']:.1f}", ch.label,
            " | ".join(p.get("label", "") for p in clip.get("embedded_prompts", [])),
            ";".join(issue_map.get(clip["clip_id"], [])),
        ])
    return out.getvalue()


# -------------------------------------------------------------- graphics

_COLORS = ["#355c7d", "#6c5b7b", "#c06c84", "#f67280", "#f8b195",
           "#2a9d8f", "#e9c46a", "#264653"]


def build_contact_sheet(timed: list[dict], channel_key: str) -> str:
    """One tile per clip showing the channel crop window, safe-area guides,
    embedded prompts and overlays — this is the human proof that vertical
    cropping does not eat essential prompts."""
    ch = domain.CHANNELS[channel_key]
    crop = domain.crop_window(ch)
    tw, th = 360, int(360 * ch.height / ch.width)
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{tw}" '
             f'height="{(th + 60) * len(timed) + 40}" font-family="sans-serif">']
    for row, clip in enumerate(timed):
        y0 = 20 + row * (th + 60)
        color = _COLORS[row % len(_COLORS)]
        parts.append(f'<rect x="10" y="{y0}" width="{tw}" height="{th}" fill="{color}"/>')
        # canvas->tile mapping: draw 16:9 canvas then crop window
        sx, sy = tw / domain.CANVAS["width"], th / domain.CANVAS["height"]
        # channel frame itself maps via crop
        cx0, cy0, cx1, cy1 = crop

        def tx(x):  # canvas-normalized -> tile x through channel crop
            return 10 + (x - cx0) / (cx1 - cx0) * tw

        def ty(y):
            return y0 + (y - cy0) / (cy1 - cy0) * th

        # safe-area guide
        parts.append(
            f'<rect x="{10 + tw * ch.inset_left:.1f}" y="{y0 + th * ch.inset_top:.1f}" '
            f'width="{tw * (1 - ch.inset_left - ch.inset_right):.1f}" '
            f'height="{th * (1 - ch.inset_top - ch.inset_bottom):.1f}" '
            'fill="none" stroke="#ffffff" stroke-dasharray="6 4" stroke-width="2"/>')
        for rect in clip.get("embedded_prompts", []):
            x, yy = tx(rect["x"]), ty(rect["y"])
            ww, hh = tw * rect["w"] / (cx1 - cx0), th * rect["h"] / (cy1 - cy0)
            visible = (rect["x"] >= cx0 and rect["y"] >= cy0
                       and rect["x"] + rect["w"] <= cx1 and rect["y"] + rect["h"] <= cy1)
            stroke = "#9be564" if visible else "#ff4444"
            parts.append(f'<rect x="{x:.1f}" y="{yy:.1f}" width="{ww:.1f}" height="{hh:.1f}" '
                         f'rx="6" fill="rgba(0,0,0,0.45)" stroke="{stroke}" stroke-width="2"/>')
            parts.append(f'<text x="{x + 6:.1f}" y="{yy + 18:.1f}" fill="#fff" '
                         f'font-size="13">[内嵌] {rect.get("label", "")[:20]}</text>')

        def frame_rect(rect: dict, label: str, stroke: str = "#9be564"):
            x = 10 + rect["x"] * tw
            yy = y0 + rect["y"] * th
            ww, hh = rect["w"] * tw, rect["h"] * th
            parts.append(f'<rect x="{x:.1f}" y="{yy:.1f}" width="{ww:.1f}" height="{hh:.1f}" '
                         f'rx="6" fill="rgba(0,0,0,0.45)" stroke="{stroke}" stroke-width="2"/>')
            parts.append(f'<text x="{x + 6:.1f}" y="{yy + 18:.1f}" fill="#fff" '
                         f'font-size="13">{label[:24]}</text>')

        # captions always live inside the channel caption band
        if clip.get("caption"):
            frame_rect(ch.caption, f'[字幕] {clip["caption"]}', "#7fd1ff")
        # cards use their own per-channel geometry
        for o in clip.get("overlays", []):
            if o.get("type") == "caption":
                continue
            geo = (o.get("channels") or {}).get(channel_key)
            if geo:
                frame_rect(geo, f'[卡片] {o.get("label", "")}')
        room = clip.get("_frozen_room") or {}
        parts.append(f'<text x="14" y="{y0 + th + 22}" fill="#111" font-size="14">'
                     f'{clip["clip_id"]} · {clip["scope"]} · {room.get("name", clip["_frozen_asset"]["space_label"])}'
                     f' · {clip["_frozen_asset"]["title"]} v{clip["_frozen_asset"]["version"]}</text>')
    parts.append("</svg>")
    return "\n".join(parts)


def build_storyboard(timed: list[dict], channel_key: str) -> str:
    ch = domain.CHANNELS[channel_key]
    tile_w = 240
    tile_h = int(tile_w * ch.height / ch.width)
    frames = len(timed)
    w = tile_w * min(frames, 4) + 20
    rows = (frames + 3) // 4
    h = (tile_h + 44) * rows + 20
    svg = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" font-family="sans-serif">']
    for i, clip in enumerate(timed):
        r, c = divmod(i, 4)
        x, y = 10 + c * tile_w, 10 + r * (tile_h + 44)
        svg.append(f'<rect x="{x}" y="{y}" width="{tile_w - 8}" height="{tile_h}" '
                   f'fill="{_COLORS[i % len(_COLORS)]}"/>')
        cap = (clip.get("caption") or "")[:18]
        svg.append(f'<text x="{x + 8}" y="{y + tile_h - 12}" fill="#fff" font-size="14">{cap}</text>')
        svg.append(f'<text x="{x}" y="{y + tile_h + 20}" fill="#111" font-size="12">'
                   f'{clip["clip_id"]} {clip["_start"]:.1f}-{clip["_end"]:.1f}s</text>')
    svg.append("</svg>")
    return "\n".join(svg)


# --------------------------------------------------------------- snapshot

def build_snapshot(timed: list[dict], channels: list[str], as_of: str) -> dict:
    """The frozen truth kept with the export.  It deliberately copies values
    rather than pointing at mutable room/facility tables, so later edits to
    room descriptions never rewrite an old export, and no recommendation is
    ever injected without a source recorded in the timeline."""
    return {
        "produced_as_of": as_of,
        "channels": channels,
        "items": [
            {
                "clip_id": c["clip_id"],
                "asset": c["_frozen_asset"],
                "room": c.get("_frozen_room"),
                "scope": c["scope"],
                "caption": c.get("caption", ""),
                "overlays": [
                    {"type": o["type"], "label": o.get("label", ""),
                     "source_ref": o.get("source_ref"),
                     "frozen_rule": o.get("_frozen_rule")}
                    for o in c.get("overlays", [])
                ],
            }
            for c in timed
        ],
    }


# ------------------------------------------------------------- execution

def process_job(job_id: int, as_of: str | None = None) -> dict:
    """Claim + render one job.  Reused by the worker loop and the API's
    inline-run (tests / single-process demo)."""
    job = store.get_job(job_id)
    if job is None:
        raise ValueError(f"job {job_id} not found")
    pv = store.get_project_version_by_id(job["project_version_id"])
    project = store.get_project(job["project_id"])
    timeline = pv["timeline_json"]
    as_of = as_of or date.today().isoformat()

    # 1) authoritative re-check at render time
    from backend.storage import blob_exists
    checks = domain.dependency_report(project, timeline, as_of,
                                      type("S", (), {"blob_exists": staticmethod(blob_exists)})())
    if not checks["ok"]:
        store.finish_job(job_id, "failed",
                         error="; ".join(e["detail"] for e in checks["errors"]))
        return {"status": "failed", "checks": checks}

    timed = clip_timeline({"items": checks["frozen_items"]})
    channels = timeline.get("channels", ["landscape_169"])
    snapshot = build_snapshot(timed, channels, as_of)

    # 2) create the immutable export row
    export_id = store.create_export(project["id"], pv["id"], channels, snapshot,
                                    checks, job["created_by"])
    job_dir = f"export_{export_id}"

    # 3) per-channel deliverables
    for ch_key in channels:
        srt = build_srt(timed)
        srt_path = output_path(job_dir, f"{ch_key}.srt")
        with open(srt_path, "w", encoding="utf-8") as f:
            f.write(srt)
        store.add_export_output(export_id, ch_key, "subtitles",
                                os.path.relpath(srt_path, OUTPUT_DIR))

        csv_path = output_path(job_dir, f"{ch_key}_shots.csv")
        with open(csv_path, "w", encoding="utf-8", newline="") as f:
            f.write(build_shot_list(timed, ch_key, checks))
        store.add_export_output(export_id, ch_key, "shot_list",
                                os.path.relpath(csv_path, OUTPUT_DIR))

        sheet_path = output_path(job_dir, f"{ch_key}_contact.svg")
        with open(sheet_path, "w", encoding="utf-8") as f:
            f.write(build_contact_sheet(timed, ch_key))
        store.add_export_output(export_id, ch_key, "contact_sheet",
                                os.path.relpath(sheet_path, OUTPUT_DIR))

        sb_path = output_path(job_dir, f"{ch_key}_storyboard.svg")
        with open(sb_path, "w", encoding="utf-8") as f:
            f.write(build_storyboard(timed, ch_key))
        rel = os.path.relpath(sb_path, OUTPUT_DIR)
        mp4 = _try_mp4(timed, ch_key, output_path(job_dir, f"{ch_key}_preview.mp4"))
        if mp4:
            rel = os.path.relpath(mp4, OUTPUT_DIR)
        store.add_export_output(export_id, ch_key, "preview", rel)

    store.finish_job(job_id, "done", export_id=export_id)
    return {"status": "done", "export_id": export_id, "checks": checks}


def _try_mp4(timed: list[dict], channel_key: str, dest: str) -> str | None:
    """Optional enhanced preview: real ffmpeg render from the copied/referenced
    blobs.  Absent ffmpeg (or non-video placeholders) -> SVG storyboard."""
    if not shutil.which("ffmpeg"):
        return None
    ch = domain.CHANNELS[channel_key]
    from backend.storage import open_blob
    listfile = dest + ".txt"
    usable = [c for c in timed if c["_frozen_asset"]["sha256"]
              and c["mode"] == "copy" and not c["_frozen_asset"].get("_placeholder")]
    if not usable:
        return None
    try:
        with open(listfile, "w") as f:
            for c in usable:
                p = os.path.abspath(open_blob(c["_frozen_asset"]["sha256"]).name)
                f.write(f"file '{p}'\nduration {c.get('duration') or 4}\n")
        subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", listfile,
                        "-vf", f"scale={ch.width}:{ch.height}:force_original_aspect_ratio=increase,"
                               f"crop={ch.width}:{ch.height}",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", dest],
                       check=True, capture_output=True, timeout=120)
        return dest
    except Exception:
        return None
    finally:
        if os.path.exists(listfile):
            os.remove(listfile)


def run_loop(once: bool = False) -> None:
    """Simple queue poller.  A real deployment would swap claim_next_job for
    a real queue; semantics (re-check at render time) stay identical."""
    import time
    while True:
        job = store.claim_next_job()
        if job is None:
            if once:
                return
            time.sleep(1)
            continue
        try:
            process_job(job["id"])
        except Exception as exc:   # never let one bad job kill the worker
            store.finish_job(job["id"], "failed", error=str(exc))
        if once:
            return
