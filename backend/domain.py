"""Domain rules: channel safe areas, shared/private scope, timeline
validation and the pre-publish dependency report.

Design notes
------------
* A project timeline is authored ONCE on a 1920x1080 "script canvas".
  Vertical channel outputs are derived by cover-cropping that canvas, so
  the checker has to map every essential prompt through the crop window
  before applying channel safe insets.  A thumbnail being viewable proves
  nothing: publishing with ``thumbnail_only`` assets is rejected outright.
* Shared/private space is enforced two ways: the asset master is tagged
  ``scope`` (shared lobby vs private room), and every timeline clip repeats
  the scope + room it is used for.  Lobby footage can never be attached as a
  room's private space.
* Every rule / facility card carries the exact room id and version/feature
  id plus an effective-date window, so cards cannot silently drift onto the
  wrong room or an expired rule.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import store
from .db import active_on, date_le

# ---------------------------------------------------------------- channels

@dataclass(frozen=True)
class Channel:
    key: str
    label: str
    width: int
    height: int
    # insets as fractions of the cropped channel frame
    inset_left: float
    inset_right: float
    inset_top: float
    inset_bottom: float
    # burned-in caption band (fraction of channel frame, bottom-anchored)
    caption: dict[str, float]


CHANNELS: dict[str, Channel] = {
    "landscape_169": Channel("landscape_169", "横版 16:9（视频号/YouTube）", 1920, 1080,
                             0.05, 0.05, 0.05, 0.08,
                             {"x": 0.08, "y": 0.86, "w": 0.84, "h": 0.10}),
    "douyin": Channel("douyin", "抖音 9:16", 1080, 1920,
                      0.075, 0.075, 0.08, 0.20,
                      {"x": 0.10, "y": 0.74, "w": 0.80, "h": 0.08}),
    "kuaishou": Channel("kuaishou", "快手 9:16", 1080, 1920,
                        0.08, 0.08, 0.08, 0.21,
                        {"x": 0.10, "y": 0.73, "w": 0.80, "h": 0.08}),
    "wechat_story": Channel("wechat_story", "微信视频号竖版 9:16", 1080, 1920,
                            0.07, 0.07, 0.09, 0.16,
                            {"x": 0.10, "y": 0.78, "w": 0.80, "h": 0.08}),
    "xiaohongshu": Channel("xiaohongshu", "小红书 3:4", 1080, 1440,
                           0.08, 0.08, 0.07, 0.13,
                           {"x": 0.10, "y": 0.82, "w": 0.80, "h": 0.08}),
}

# shared script canvas
CANVAS = {"width": 1920, "height": 1080}


def channel_dicts() -> list[dict]:
    return [c.__dict__ | {"aspect": c.width / c.height} for c in CHANNELS.values()]


def crop_window(channel: Channel) -> tuple[float, float, float, float]:
    """Center "cover" crop window of the channel inside the 16:9 script
    canvas, returned as (x0, y0, x1, y1) in canvas-normalized coordinates."""
    r_canvas = CANVAS["width"] / CANVAS["height"]      # 1.7778
    r_channel = channel.width / channel.height
    if r_channel < r_canvas:        # target taller -> crop the sides
        w = r_channel / r_canvas
        return ((1 - w) / 2, 0.0, (1 + w) / 2, 1.0)
    if r_channel > r_canvas:        # target wider -> crop top/bottom
        h = r_canvas / r_channel
        return (0.0, (1 - h) / 2, 1.0, (1 + h) / 2)
    return (0.0, 0.0, 1.0, 1.0)


def _map_through_crop(rect: dict, crop: tuple) -> dict | None:
    x0, y0, x1, y1 = crop
    cw, ch = x1 - x0, y1 - y0
    # reject rectangles that do not even lie inside 0..1
    rx, ry, rw, rh = rect["x"], rect["y"], rect["w"], rect["h"]
    inside = rx >= x0 and ry >= y0 and rx + rw <= x1 and ry + rh <= y1
    mapped = {"x": (rx - x0) / cw, "y": (ry - y0) / ch,
              "w": rw / cw, "h": rh / ch}
    return mapped if inside else None


def safe_area_report(channel_key: str, timeline: dict) -> list[dict]:
    """Check every essential prompt / overlay of every clip for one channel.

    Things checked per clip:
      * embedded prompts baked into the master footage (``embedded_prompts``)
      - timeline overlays / cards (``overlays``)
    A prompt fully clipped by the crop is always an error; a prompt inside
    the crop but outside channel safe insets is an error when ``essential``
    and a warning otherwise.
    """
    ch = CHANNELS[channel_key]
    crop = crop_window(ch)
    issues: list[dict] = []

    def check_rect(clip_id: str, rect: dict, what: str) -> None:
        essential = bool(rect.get("essential", True))
        mapped = _map_through_crop(rect, crop)
        if mapped is None:
            issues.append({
                "code": "CROP_CLIPS_ESSENTIAL" if essential else "CROP_CLIPS_OVERLAY",
                "severity": "error" if essential else "warning",
                "clip_id": clip_id,
                "channel": channel_key,
                "detail": f"{what}「{rect.get('label', '')}」在{ch.label}裁切后被遮掉",
            })
            return
        if (mapped["x"] < ch.inset_left or mapped["y"] < ch.inset_top
                or mapped["x"] + mapped["w"] > 1 - ch.inset_right
                or mapped["y"] + mapped["h"] > 1 - ch.inset_bottom):
            issues.append({
                "code": "OVERLAY_OUTSIDE_SAFE",
                "severity": "error" if essential else "warning",
                "clip_id": clip_id,
                "channel": channel_key,
                "detail": f"{what}「{rect.get('label', '')}」落在{ch.label}安全区之外",
            })

    for item in timeline.get("items", []):
        cid = item["clip_id"]
        # 1) prompts baked INTO master footage: geometry lives on the 16:9
        #    script canvas and MUST survive center-crop + safe insets.
        for p in item.get("embedded_prompts", []):
            check_rect(cid, p, "素材内必要提示")
        # 2) timeline overlays / cards:
        #    - caption type is re-laid-out per channel into that channel's
        #      caption band at render time, so it never needs crop mapping;
        #    - other cards must provide per-channel geometry in `channels`,
        #      authored directly in channel-frame coordinates; a card with
        #      no geometry for a selected channel is an error (this is what
        #      stops reusing one thumbnail/position as "probably fine").
        for o in item.get("overlays", []):
            if o.get("type") == "caption":
                continue
            geo = (o.get("channels") or {}).get(channel_key)
            if geo is None:
                issues.append({
                    "code": "OVERLAY_NO_CHANNEL_GEOMETRY", "severity": "error",
                    "clip_id": cid, "channel": channel_key,
                    "detail": f"卡片「{o.get('label', '')}」缺少 {ch.label} 的独立安全区坐标，"
                              "不能只靠横竖版共用位置或缩略图判断",
                })
                continue
            rect = {"label": o.get("label", ""),
                    "essential": bool(o.get("essential", True)), **geo}
            if (rect["x"] < ch.inset_left or rect["y"] < ch.inset_top
                    or rect["x"] + rect["w"] > 1 - ch.inset_right
                    or rect["y"] + rect["h"] > 1 - ch.inset_bottom):
                issues.append({
                    "code": "OVERLAY_OUTSIDE_SAFE",
                    "severity": "error" if rect["essential"] else "warning",
                    "clip_id": cid, "channel": channel_key,
                    "detail": f"卡片「{o.get('label', '')}」落在{ch.label}安全区之外",
                })
    return issues


# ------------------------------------------------------ timeline validation

class ValidationError(Exception):
    def __init__(self, issues: list[dict]):
        super().__init__(f"{len(issues)} timeline validation issue(s)")
        self.issues = issues


def validate_timeline(timeline: dict, property_id: int) -> list[dict]:
    """Structural + binding validation executed on every timeline save."""
    issues: list[dict] = []
    if not isinstance(timeline.get("items"), list):
        return [{"code": "TIMELINE_SHAPE", "severity": "error",
                 "detail": "timeline.items must be a list"}]
    channels = timeline.get("channels") or []
    if not isinstance(channels, list) or not channels:
        issues.append({"code": "NO_CHANNELS", "severity": "error",
                       "detail": "至少选择一个输出渠道"})
    else:
        for ch in channels:
            if ch not in CHANNELS:
                issues.append({"code": "UNKNOWN_CHANNEL", "severity": "error",
                               "detail": f"未知渠道 {ch}"})
    seen: set[str] = set()
    room_ids = {r["id"] for r in store.list_room_types(property_id)}
    for i, item in enumerate(timeline["items"]):
        cid = item.get("clip_id", f"#{i}")
        if cid in seen:
            issues.append({"code": "DUP_CLIP_ID", "severity": "error", "clip_id": cid})
        seen.add(cid)
        scope = item.get("scope")
        if scope not in ("shared", "private"):
            issues.append({"code": "BAD_SCOPE", "severity": "error", "clip_id": cid})
        room_id = item.get("room_type_id")
        if scope == "private":
            if not room_id or room_id not in room_ids:
                issues.append({"code": "PRIVATE_CLIP_NEEDS_ROOM", "severity": "error",
                               "clip_id": cid, "detail": "私有空间镜头必须绑定具体房型"})
        elif scope == "shared" and room_id:
            issues.append({"code": "SHARED_CLIP_HAS_ROOM", "severity": "error",
                           "clip_id": cid, "detail": "公共片段不可绑定房型"})

        # ---- master asset binding
        asset = store.get_asset(item.get("asset_id", 0)) if item.get("asset_id") else None
        if asset is None:
            issues.append({"code": "ASSET_MISSING", "severity": "error", "clip_id": cid})
            continue
        if asset["property_id"] != property_id:
            issues.append({"code": "ASSET_CROSS_PROPERTY", "severity": "error", "clip_id": cid})
        if scope == "private" and asset["scope"] == "shared":
            issues.append({
                "code": "SHARED_ASSET_MARKED_PRIVATE", "severity": "error", "clip_id": cid,
                "detail": f"大厅等公共素材「{asset['title']}」不能被标成房型私有空间"})
        if scope == "shared" and asset["scope"] == "private":
            issues.append({"code": "PRIVATE_ASSET_USED_SHARED", "severity": "error",
                           "clip_id": cid})
        if asset["scope"] == "private" and room_id and asset["room_type_id"] != room_id:
            issues.append({"code": "ROOM_MISMATCH", "severity": "error", "clip_id": cid,
                           "detail": "素材属于其它房型，不能挂到当前房型"})

        ver = store.get_asset_version(asset["id"], item.get("asset_version", 1))
        if ver is None:
            issues.append({"code": "ASSET_VERSION_MISSING", "severity": "error", "clip_id": cid})

        if item.get("mode") not in ("reference", "copy"):
            issues.append({"code": "BAD_MODE", "severity": "error", "clip_id": cid})

        def _geo_ok(rect: dict) -> bool:
            return all(isinstance(rect.get(k), (int, float)) and 0 <= rect[k] <= 1
                       for k in ("x", "y", "w", "h"))

        for rect in item.get("embedded_prompts", []):
            if not _geo_ok(rect):
                issues.append({"code": "BAD_GEOMETRY", "severity": "error",
                               "clip_id": cid, "detail": "内嵌提示坐标必须在 0..1 之间"})

        selected = set(timeline.get("channels", ["landscape_169"]))
        for o in item.get("overlays", []):
            if o.get("type") == "caption":
                continue
            geos = o.get("channels")
            # per-channel geometry must exist for every selected channel
            missing_ch = sorted(ch for ch in selected if not isinstance(
                (geos or {}).get(ch), dict) or not _geo_ok(geos[ch]))
            if missing_ch:
                issues.append({"code": "OVERLAY_NO_CHANNEL_GEOMETRY", "severity": "error",
                               "clip_id": cid,
                               "detail": f"卡片「{o.get('label', '')}」缺少渠道坐标: {', '.join(missing_ch)}"})

        for o in item.get("overlays", []):
            kind = o.get("type")
            if kind == "rule_card":
                if not o.get("room_type_id") or not o.get("rule_version_id"):
                    issues.append({"code": "RULE_CARD_UNBOUND", "severity": "error",
                                   "clip_id": cid, "detail": "规则卡片必须绑定房型和规则版本"})
            elif kind in ("facility_card", "window_card"):
                if not o.get("feature_id") or not o.get("room_type_id"):
                    issues.append({"code": "FEATURE_CARD_UNBOUND", "severity": "error",
                                   "clip_id": cid, "detail": "设施/窗景卡片必须绑定房型与有效记录"})
            elif kind == "recommendation":
                if not o.get("source_ref"):
                    issues.append({"code": "RECOMMENDATION_UNSUPPORTED", "severity": "error",
                                   "clip_id": cid, "detail": "周边推荐必须填写依据来源"})
    return issues


# ------------------------------------------------- pre-publish dependency check

def _issue(code: str, severity: str, clip_id: str, detail: str) -> dict:
    return {"code": code, "severity": severity, "clip_id": clip_id, "detail": detail}


def dependency_report(project: dict, timeline: dict, as_of: str,
                      storage) -> dict:
    """Re-verify every media/rule/facility dependency at publish time.

    ``as_of`` (YYYY-MM-DD) makes the result deterministic and is what old
    exports were checked against; the worker recomputes the same report when
    it runs so a queued job cannot bypass revoked licenses.
    """
    issues: list[dict] = []
    property_id = project["property_id"]
    frozen_items: list[dict] = []

    for item in timeline.get("items", []):
        cid = item["clip_id"]
        asset = store.get_asset(item["asset_id"])
        pinned_v = item.get("asset_version", 1)
        ver = store.get_asset_version(item["asset_id"], pinned_v) if asset else None
        if asset is None or ver is None:
            issues.append(_issue("ASSET_MISSING", "error", cid, "母素材或其版本已不存在"))
            continue

        # scope can never have been bypassed by an older client
        if item["scope"] == "private" and asset["scope"] == "shared":
            issues.append(_issue("SHARED_ASSET_MARKED_PRIVATE", "error", cid,
                                 "公共素材被标为私有空间"))

        # thumbnail is not usable footage
        if asset["thumbnail_only"]:
            issues.append(_issue("THUMBNAIL_USED_AS_MEDIA", "error", cid,
                                 "仅有缩略图代理，缺少完整分辨率素材"))

        # license window + withdrawal
        lic = store.get_license_for(asset["id"], pinned_v)
        if lic is None:
            issues.append(_issue("LICENSE_MISSING", "error", cid,
                                 f"素材「{asset['title']}」无授权记录"))
        else:
            if lic["withdrawn"]:
                issues.append(_issue("LICENSE_WITHDRAWN", "error", cid,
                                     f"授权已撤回：{lic['withdrawn_reason'] or '未填写原因'}"))
            elif not active_on(lic["valid_from"], lic["valid_to"], as_of):
                issues.append(_issue("LICENSE_EXPIRED", "error", cid,
                                     f"授权在 {as_of} 不在有效期内"))

        # photo source liveness
        if ver["source_status"] == "unreachable":
            issues.append(_issue("PHOTO_SOURCE_UNREACHABLE", "error", cid,
                                 f"照片来源失效：{ver['source_uri']}"))

        # reference vs copy semantics
        if item["mode"] == "reference":
            if asset["current_version"] > pinned_v:
                issues.append(_issue("MASTER_UPDATED", "warning", cid,
                    f"母素材已更新到 v{asset['current_version']}，本项目引用 v{pinned_v}；"
                    "历史可复现，但建议确认是否升级"))
        else:  # copy: the bytes must physically still exist in project storage
            if not storage.blob_exists(ver["sha256"]):
                issues.append(_issue("COPY_BLOB_MISSING", "error", cid,
                                     "复制进项目的素材文件丢失"))

        # cards
        frozen_overlays = []
        for o in item.get("overlays", []):
            kind = o.get("type")
            if kind == "rule_card":
                rv = store.get_rule_version(o["rule_version_id"])
                if rv is None or rv["room_type_id"] != o["room_type_id"]:
                    issues.append(_issue("RULE_CARD_UNBOUND", "error", cid, "规则卡片绑定失效"))
                else:
                    if not active_on(rv["effective_from"], rv["effective_to"], as_of):
                        issues.append(_issue("RULE_NOT_EFFECTIVE", "error", cid,
                                             "所引用规则版本在发布日不生效"))
                    current = store.effective_rule(o["room_type_id"], as_of)
                    if current and current["id"] != rv["id"]:
                        issues.append(_issue("RULE_REVISION_STALE", "error", cid,
                            f"入住规则已修订（当前生效 v{current['version']}，"
                            f"卡片仍是 v{rv['version']}），请确认后重挂新版本"))
                    frozen_overlays.append(o | {"_frozen_rule": rv["rules_json"]})
            elif kind in ("facility_card", "window_card"):
                f = store.get_feature(o["feature_id"])
                want = "facility" if kind == "facility_card" else "window_view"
                if f is None or f["room_type_id"] != o["room_type_id"] or f["kind"] != want:
                    issues.append(_issue("FEATURE_CARD_UNBOUND", "error", cid,
                                         "设施/窗景卡片绑定失效"))
                elif not active_on(f["valid_from"], f["valid_to"], as_of):
                    issues.append(_issue("FEATURE_INVALID", "error", cid,
                                         f"「{f['label']}」在 {as_of} 不在有效期"))
            elif kind == "recommendation":
                if not o.get("source_ref"):
                    issues.append(_issue("RECOMMENDATION_UNSUPPORTED", "error", cid,
                                         "周边推荐缺少依据来源"))
            frozen_overlays.append(o) if kind != "rule_card" else None

        room = store.get_room_type(item["room_type_id"]) if item.get("room_type_id") else None
        frozen_items.append({
            **item,
            "_frozen_asset": {"id": asset["id"], "title": asset["title"],
                              "version": pinned_v, "sha256": ver["sha256"],
                              "space_label": asset["space_label"],
                              "scope": asset["scope"]},
            "_frozen_room": ({"id": room["id"], "name": room["name"]} if room else None),
            "overlays": frozen_overlays,
        })

    # channel safe-area checks
    channel_issues: dict[str, list] = {}
    for ch_key in timeline.get("channels", ["landscape_169"]):
        if ch_key not in CHANNELS:
            issues.append(_issue("UNKNOWN_CHANNEL", "error", "-", f"未知渠道 {ch_key}"))
            continue
        ch_issues = safe_area_report(ch_key, timeline)
        channel_issues[ch_key] = ch_issues
        issues.extend(ch_issues)

    errors = [i for i in issues if i["severity"] == "error"]
    warnings = [i for i in issues if i["severity"] == "warning"]
    return {
        "as_of": as_of,
        "issues": issues,
        "errors": errors,
        "warnings": warnings,
        "ok": not errors,
        "channels": channel_issues,
        "frozen_items": frozen_items,
    }
