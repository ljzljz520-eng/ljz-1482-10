"""WSGI API for the workbench.

Auth: the ``X-User-Id`` header identifies the acting user (the bundled UI
has a user switcher; a production deployment replaces this with a session).

Conventions
-----------
* 403   - membership changed / role too weak ("resume last project after a
          permission change" is rejected here, never silently degraded).
* 409   - two people edited the same project version (save conflict).
* 422   - publish gate failed (media/rule/safe-area dependency errors).
* timeline saves are structural-only validation; full dependency re-check
  happens at publish AND again inside the worker.
"""
from __future__ import annotations

import json
import re
import traceback
import uuid
from datetime import date

from . import domain, store
from .db import init_db
from .storage import (blob_exists, ensure_dirs, open_blob, put_blob,
                      put_bytes, UPLOAD_DIR)


class HttpError(Exception):
    def __init__(self, status: int, code: str, detail: str = "", extra: dict | None = None):
        super().__init__(detail or code)
        self.status = status
        self.code = code
        self.detail = detail
        self.extra = extra or {}


# --------------------------------------------------------------- app

class App:
    def __init__(self, run_jobs_inline: bool = True):
        # (method, regex, handler_name)
        self.run_jobs_inline = run_jobs_inline
        self.routes: list[tuple] = []

    def route(self, method: str, pattern: str):
        rx = re.compile("^" + pattern + "$")

        def deco(fn):
            self.routes.append((method, rx, fn))
            return fn
        return deco

    def __call__(self, environ, start_response):
        try:
            init_db()
            ensure_dirs()
            path = environ["PATH_INFO"].split("?", 1)[0]
            if not environ.get("QUERY_STRING") and "?" in environ["PATH_INFO"]:
                environ["QUERY_STRING"] = environ["PATH_INFO"].split("?", 1)[1]
            for method, rx, fn in self.routes:
                if method != environ["REQUEST_METHOD"]:
                    continue
                m = rx.match(path)
                if not m:
                    continue
                ctx = Ctx(environ)
                data = fn(ctx, **m.groupdict())
                return self._json(start_response, 200, data)
            raise HttpError(404, "NOT_FOUND", f"{environ['REQUEST_METHOD']} {path}")
        except HttpError as e:
            body = {"error": e.code, "detail": e.detail, **e.extra}
            return self._json(start_response, e.status, body)
        except store.ConflictError as e:
            body = {"error": "VERSION_CONFLICT", "detail": str(e),
                    "current_version": e.current_version}
            return self._json(start_response, 409, body)
        except Exception:
            traceback.print_exc()
            return self._json(start_response, 500,
                              {"error": "INTERNAL", "detail": "unhandled server error"})

    @staticmethod
    def _json(start_response, status, data):
        raw = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        start_response(f"{status} {_STATUS.get(status, 'OK')}", [
            ("Content-Type", "application/json; charset=utf-8"),
            ("Content-Length", str(len(raw)))])
        return [raw]


_STATUS = {200: "OK", 403: "Forbidden", 404: "Not Found", 409: "Conflict",
          422: "Unprocessable Entity", 500: "Internal Server Error"}


class Ctx:
    def __init__(self, environ):
        self.environ = environ
        uid = environ.get("HTTP_X_USER_ID")
        self.user_id = int(uid) if uid else None
        self._body = None
        self._raw = None

    @property
    def user(self) -> dict:
        if self.user_id is None:
            raise HttpError(401, "NO_USER", "缺少 X-User-Id")
        u = store.get_user(self.user_id)
        if u is None:
            raise HttpError(401, "NO_USER", "用户不存在")
        return u

    def json(self) -> dict:
        if self._body is None:
            length = int(self.environ.get("CONTENT_LENGTH") or 0)
            raw = self.environ["wsgi.input"].read(length) if length else b"{}"
            try:
                self._body = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                raise HttpError(400, "BAD_JSON", "请求体不是合法 JSON")
        return self._body

    def raw_body(self) -> bytes:
        length = int(self.environ.get("CONTENT_LENGTH") or 0)
        return self.environ["wsgi.input"].read(length) if length else b""

    def query(self) -> dict:
        from urllib.parse import parse_qs
        q = parse_qs(self.environ.get("QUERY_STRING", ""))
        return {k: v[0] for k, v in q.items()}


def require_role(user_id: int, property_id: int,
                 minimum: tuple = ("viewer", "editor", "owner")) -> str:
    role = store.get_role(user_id, property_id)
    if role is None:
        # explicit permission change -> 403, even for users who could before
        if store.get_property(property_id) is None:
            raise HttpError(404, "NOT_FOUND", "房源不存在")
        raise HttpError(403, "MEMBERSHIP_REQUIRED", "你已不是该房源成员，无法打开项目")
    if role not in minimum:
        raise HttpError(403, "ROLE_FORBIDDEN", f"角色 {role} 无权执行该操作")
    return role


EDITOR = ("editor", "owner")
OWNER = ("owner",)


app = App(run_jobs_inline=__import__("os").environ.get("WORKBENCH_INLINE", "1") == "1")
route = app.route


# ----------------------------------------------------------------- meta

@route("GET", r"/health")
def health(ctx: Ctx):
    return {"ok": True, "date": date.today().isoformat()}


@route("GET", r"/api/users")
def users(ctx: Ctx):
    return {"users": store.list_users()}


@route("GET", r"/api/channels")
def channels(ctx: Ctx):
    return {"channels": domain.channel_dicts(), "canvas": domain.CANVAS}


@route("GET", r"/api/home")
def home(ctx: Ctx):
    """Homepage: projects WITH the exact project version each cursor points
    at, not the mutable latest."""
    u = ctx.user
    rows = store.list_projects_for_user(u["id"])
    out = []
    for r in rows:
        out.append({
            "project_id": r["id"], "title": r["title"],
            "property_id": r["property_id"], "role": r["role"],
            "cursor_version": r["cursor_version"],
            "cursor_version_id": r["cursor_version_id"],
        })
    return {"date": date.today().isoformat(), "projects": out}


# ------------------------------------------------------------- properties

@route("POST", r"/api/properties")
def create_property(ctx: Ctx):
    u = ctx.user
    b = ctx.json()
    pid = store.create_property(b["name"], b.get("address", ""))
    store.add_member(pid, u["id"], "owner")
    return {"property_id": pid}


@route("GET", r"/api/properties/(?P<pid>\d+)")
def get_property(ctx: Ctx, pid: str):
    require_role(ctx.user_id, int(pid))
    p = store.get_property(int(pid))
    p["room_types"] = store.list_room_types(int(pid))
    p["members"] = _members(int(pid))
    return p


def _members(pid: int):
    from .db import get_conn
    with get_conn() as c:
        return [dict(r) for r in c.execute("""SELECT pm.user_id, u.username,
                    u.display_name, pm.role FROM property_members pm
                    JOIN users u ON u.id=pm.user_id WHERE pm.property_id=?""", (pid,))]


@route("POST", r"/api/properties/(?P<pid>\d+)/members")
def add_member(ctx: Ctx, pid: str):
    require_role(ctx.user_id, int(pid), OWNER)
    b = ctx.json()
    store.add_member(int(pid), int(b["user_id"]), b["role"])
    return {"ok": True, "members": _members(int(pid))}


# ------------------------------------------------------------- room types

@route("POST", r"/api/properties/(?P<pid>\d+)/room-types")
def create_room(ctx: Ctx, pid: str):
    require_role(ctx.user_id, int(pid), EDITOR)
    rid = store.create_room_type(int(pid), ctx.json()["name"])
    return {"room_type_id": rid}


@route("GET", r"/api/room-types/(?P<rid>\d+)")
def get_room(ctx: Ctx, rid: str):
    room = store.get_room_type(int(rid))
    if room is None:
        raise HttpError(404, "NOT_FOUND", "房型不存在")
    require_role(ctx.user_id, room["property_id"])
    room["rule_versions"] = store.list_rule_versions(int(rid))
    room["features"] = store.list_features(int(rid))
    as_of = ctx.query().get("as_of", date.today().isoformat())
    room["effective_rule"] = store.effective_rule(int(rid), as_of)
    return room


# ------------------------------------------------------ rule revisions

@route("POST", r"/api/room-types/(?P<rid>\d+)/rules")
def add_rule(ctx: Ctx, rid: str):
    room = store.get_room_type(int(rid))
    if room is None:
        raise HttpError(404, "NOT_FOUND", "房型不存在")
    require_role(ctx.user_id, room["property_id"], EDITOR)
    b = ctx.json()
    needed = {"check_in", "check_out"}
    missing = needed - set(b.get("rules", {}))
    if missing:
        raise HttpError(400, "BAD_RULE", f"规则缺少字段: {sorted(missing)}")
    rvid = store.add_rule_revision(int(rid), b["rules"], b["effective_from"],
                                   b.get("effective_to"), ctx.user_id)
    return {"rule_version_id": rvid,
            "version": store.get_rule_version(rvid)["version"]}


# --------------------------------------------------------------- features

@route("POST", r"/api/room-types/(?P<rid>\d+)/features")
def add_feature(ctx: Ctx, rid: str):
    room = store.get_room_type(int(rid))
    if room is None:
        raise HttpError(404, "NOT_FOUND", "房型不存在")
    require_role(ctx.user_id, room["property_id"], EDITOR)
    b = ctx.json()
    if b["kind"] not in ("facility", "window_view"):
        raise HttpError(400, "BAD_FEATURE", "kind 必须是 facility/window_view")
    fid = store.add_feature(int(rid), b["kind"], b["label"], b["valid_from"],
                            b.get("valid_to"), b.get("source_note", ""))
    return {"feature_id": fid}


# ---------------------------------------------------------------- assets

@route("GET", r"/api/assets")
def list_assets(ctx: Ctx):
    pid = int(ctx.query().get("property_id", 0))
    require_role(ctx.user_id, pid)
    assets = store.list_assets(pid)
    for a in assets:
        a["license"] = store.get_license_for(a["id"], a["current_version"])
    return {"assets": assets}


@route("POST", r"/api/assets")
def create_asset(ctx: Ctx):
    b = ctx.json()
    pid = int(b["property_id"])
    require_role(ctx.user_id, pid, EDITOR)
    _validate_scope(pid, b["scope"], b.get("room_type_id"))
    if not blob_exists(b["sha256"]):
        raise HttpError(400, "BLOB_MISSING", "请先上传文件拿到 sha256")
    aid = store.create_asset(
        property_id=pid, kind=b["kind"], title=b["title"], scope=b["scope"],
        room_type_id=b.get("room_type_id"), space_label=b.get("space_label", ""),
        sha256=b["sha256"], size_bytes=int(b["size_bytes"]),
        source_uri=b["source_uri"], width=b.get("width"), height=b.get("height"),
        duration=b.get("duration"), thumbnail_only=int(b.get("thumbnail_only", 0)),
        created_by=ctx.user_id)
    if b.get("license"):
        av = store.get_asset_version(aid, 1)
        lic = b["license"]
        store.add_license(av["id"], lic.get("holder", ""), lic["valid_from"],
                          lic.get("valid_to"), lic.get("note", ""))
    return {"asset_id": aid}


def _validate_scope(pid: int, scope: str, room_type_id: int | None):
    if scope == "shared":
        if room_type_id:
            raise HttpError(422, "SHARED_HAS_ROOM",
                            "大厅等公共素材不能绑定任何房型")
    elif scope == "private":
        room = store.get_room_type(room_type_id or 0)
        if room is None or room["property_id"] != pid:
            raise HttpError(422, "PRIVATE_NEEDS_ROOM",
                            "私有素材必须绑定本房源下的具体房型")
    else:
        raise HttpError(400, "BAD_SCOPE", "scope 必须是 shared/private")


@route("GET", r"/api/assets/(?P<aid>\d+)")
def get_asset(ctx: Ctx, aid: str):
    a = store.get_asset(int(aid))
    if a is None:
        raise HttpError(404, "NOT_FOUND", "素材不存在")
    require_role(ctx.user_id, a["property_id"])
    a["versions"] = store.list_asset_versions(int(aid))
    a["license"] = store.get_license_for(int(aid), a["current_version"])
    return a


@route("POST", r"/api/assets/(?P<aid>\d+)/versions")
def add_asset_version(ctx: Ctx, aid: str):
    a = store.get_asset(int(aid))
    if a is None:
        raise HttpError(404, "NOT_FOUND")
    require_role(ctx.user_id, a["property_id"], EDITOR)
    b = ctx.json()
    if not blob_exists(b["sha256"]):
        raise HttpError(400, "BLOB_MISSING", "新版本文件未上传")
    nv = store.add_asset_version(int(aid), sha256=b["sha256"],
                                 size_bytes=int(b["size_bytes"]),
                                 source_uri=b["source_uri"],
                                 width=b.get("width"), height=b.get("height"),
                                 duration=b.get("duration"))
    if b.get("license"):
        av = store.get_asset_version(int(aid), nv)
        lic = b["license"]
        store.add_license(av["id"], lic.get("holder", ""), lic["valid_from"],
                          lic.get("valid_to"), lic.get("note", ""))
    return {"version": nv}


@route("POST", r"/api/assets/(?P<aid>\d+)/license")
def attach_license(ctx: Ctx, aid: str):
    a = store.get_asset(int(aid))
    if a is None:
        raise HttpError(404, "NOT_FOUND")
    require_role(ctx.user_id, a["property_id"], EDITOR)
    b = ctx.json()
    ver = store.get_asset_version(int(aid), int(b.get("version", a["current_version"])))
    lid = store.add_license(ver["id"], b.get("holder", ""), b["valid_from"],
                            b.get("valid_to"), b.get("note", ""))
    return {"license_id": lid}


@route("POST", r"/api/assets/(?P<aid>\d+)/withdraw")
def withdraw(ctx: Ctx, aid: str):
    a = store.get_asset(int(aid))
    if a is None:
        raise HttpError(404, "NOT_FOUND")
    require_role(ctx.user_id, a["property_id"], EDITOR)
    b = ctx.json()
    store.withdraw_license(int(aid), int(b.get("version", a["current_version"])),
                           b.get("reason", ""))
    return {"ok": True}


@route("POST", r"/api/assets/(?P<aid>\d+)/source-status")
def source_status(ctx: Ctx, aid: str):
    """Record an external photo-source health probe (ok/unreachable)."""
    a = store.get_asset(int(aid))
    if a is None:
        raise HttpError(404, "NOT_FOUND")
    require_role(ctx.user_id, a["property_id"], EDITOR)
    b = ctx.json()
    store.set_source_status(int(aid), int(b.get("version", a["current_version"])),
                            b["status"])
    return {"ok": True}


# --------------------------------------------------------------- uploads

@route("POST", r"/api/uploads")
def upload_init(ctx: Ctx):
    ctx.user
    b = ctx.json()
    uid = uuid.uuid4().hex
    store.create_upload(uid, b["filename"], int(b["total_size"]),
                        int(b.get("chunk_size", 1024 * 1024)), ctx.user_id)
    return {"upload_id": uid, "received": 0}


@route("GET", r"/api/uploads/(?P<uid>[0-9a-f]+)")
def upload_status(ctx: Ctx, uid: str):
    s = store.get_upload(uid)
    if s is None:
        raise HttpError(404, "NOT_FOUND", "上传会话不存在")
    return s


@route("PUT", r"/api/uploads/(?P<uid>[0-9a-f]+)/chunks/(?P<idx>\d+)")
def upload_chunk(ctx: Ctx, uid: str, idx: str):
    s = store.get_upload(uid)
    if s is None:
        raise HttpError(404, "NOT_FOUND", "上传会话不存在")
    if s["user_id"] != ctx.user_id:
        raise HttpError(403, "NOT_YOUR_UPLOAD")
    if s["status"] != "open":
        return {"received": s["received"], "already": True}
    data = ctx.raw_body()
    # chunks land at byte offset idx*chunk_size; writing a chunk twice
    # (retry) is idempotent because the full file is hashed at completion.
    start = int(idx) * s["chunk_size"]
    import os
    tmp = os.path.join(os.path.dirname(UPLOAD_DIR), "chunks", uid + ".part")
    os.makedirs(os.path.dirname(tmp), exist_ok=True)
    with open(tmp, "r+b") if os.path.exists(tmp) else open(tmp, "w+b") as f:
        f.seek(start)
        f.write(data)
        new_len = max(s["received"], start + len(data))
    store.bump_upload(uid, new_len)
    return {"received": new_len}


@route("POST", r"/api/uploads/(?P<uid>[0-9a-f]+)/complete")
def upload_complete(ctx: Ctx, uid: str):
    s = store.get_upload(uid)
    if s is None:
        raise HttpError(404, "NOT_FOUND")
    if s["user_id"] != ctx.user_id:
        raise HttpError(403, "NOT_YOUR_UPLOAD")
    import os
    tmp = os.path.join(os.path.dirname(UPLOAD_DIR), "chunks", uid + ".part")
    if s["status"] == "completed":
        return {"sha256": s["sha256"], "size_bytes": s["total_size"], "already": True}
    with open(tmp, "rb") as f:
        digest, size = put_blob(f)
    if size != s["total_size"]:
        raise HttpError(400, "SIZE_MISMATCH",
                        f"收到 {size} 字节，期望 {s['total_size']}，可重试缺失分片")
    store.bump_upload(uid, size, digest, "completed")
    return {"sha256": digest, "size_bytes": size}


# -------------------------------------------------------------- projects

def _empty_timeline(channels=None):
    return {"channels": channels or ["landscape_169"], "items": []}


@route("POST", r"/api/projects")
def create_project(ctx: Ctx):
    b = ctx.json()
    require_role(ctx.user_id, int(b["property_id"]), EDITOR)
    tl = b.get("timeline") or _empty_timeline(b.get("channels"))
    issues = domain.validate_timeline(tl, int(b["property_id"]))
    errs = [i for i in issues if i["severity"] == "error"]
    if errs:
        raise HttpError(422, "TIMELINE_INVALID", "初始时间线不合法", {"issues": issues})
    pid = store.create_project(int(b["property_id"]), b["title"], ctx.user_id, tl)
    return {"project_id": pid, "version": 1}


@route("GET", r"/api/projects/(?P<pid>\d+)")
def get_project(ctx: Ctx, pid: str):
    p = store.get_project(int(pid))
    if p is None:
        raise HttpError(404, "NOT_FOUND", "项目不存在")
    role = require_role(ctx.user_id, p["property_id"])
    cur = store.latest_project_version(int(pid))
    return {"project": p, "role": role, "latest_version": cur["version"],
            "timeline": cur["timeline_json"]}


@route("POST", r"/api/projects/(?P<pid>\d+)/timeline")
def save_timeline(ctx: Ctx, pid: str):
    p = store.get_project(int(pid))
    if p is None:
        raise HttpError(404, "NOT_FOUND")
    require_role(ctx.user_id, p["property_id"], EDITOR)
    b = ctx.json()
    issues = domain.validate_timeline(b["timeline"], p["property_id"])
    errs = [i for i in issues if i["severity"] == "error"]
    if errs:
        raise HttpError(422, "TIMELINE_INVALID", "时间线校验失败", {"issues": issues})
    pv = store.save_project_version(int(pid), b["timeline"], ctx.user_id,
                                    int(b["base_version"]))
    # cursor follows explicit saves
    store.update_cursor(ctx.user_id, int(pid), pv["id"])
    return {"ok": True, "version": pv["version"], "warnings":
            [i for i in issues if i["severity"] == "warning"]}


@route("POST", r"/api/projects/(?P<pid>\d+)/resume")
def resume(ctx: Ctx, pid: str):
    """Open 'last project': membership is re-checked RIGHT NOW.  If the user
    lost access since the cursor was saved -> 403 (not an empty project)."""
    p = store.get_project(int(pid))
    if p is None:
        raise HttpError(404, "NOT_FOUND")
    require_role(ctx.user_id, p["property_id"])   # raises 403 on change
    cur = store.latest_project_version(int(pid))
    store.update_cursor(ctx.user_id, int(pid), cur["id"])
    return {"project_id": int(pid), "resumed_version": cur["version"],
            "timeline": cur["timeline_json"]}


@route("POST", r"/api/projects/(?P<pid>\d+)/copy-clip")
def copy_clip(ctx: Ctx, pid: str):
    """Switch one clip from master-reference to project-owned copy.  Bytes
    are content-addressed (no duplication), but the timeline now pins a
    copy that survives master replacement/withdrawal per policy."""
    p = store.get_project(int(pid))
    if p is None:
        raise HttpError(404, "NOT_FOUND")
    require_role(ctx.user_id, p["property_id"], EDITOR)
    b = ctx.json()
    cur = store.latest_project_version(int(pid))
    tl = cur["timeline_json"]
    found = False
    for item in tl["items"]:
        if item["clip_id"] == b["clip_id"]:
            ver = store.get_asset_version(item["asset_id"], item["asset_version"])
            if not blob_exists(ver["sha256"]):
                raise HttpError(422, "COPY_BLOB_MISSING", "源文件不可用，无法复制")
            item["mode"] = "copy"
            item["copied_sha256"] = ver["sha256"]
            found = True
            break
    if not found:
        raise HttpError(404, "CLIP_NOT_FOUND", "镜头不在时间线上")
    pv = store.save_project_version(int(pid), tl, ctx.user_id, cur["version"])
    return {"ok": True, "version": pv["version"]}


@route("GET", r"/api/projects/(?P<pid>\d+)/checks")
def checks(ctx: Ctx, pid: str):
    p = store.get_project(int(pid))
    if p is None:
        raise HttpError(404, "NOT_FOUND")
    require_role(ctx.user_id, p["property_id"])
    as_of = ctx.query().get("as_of", date.today().isoformat())
    pv = store.latest_project_version(int(pid))
    report = domain.dependency_report(p, pv["timeline_json"], as_of, _StorageView())
    report.pop("frozen_items", None)
    return report


@route("POST", r"/api/projects/(?P<pid>\d+)/publish")
def publish(ctx: Ctx, pid: str):
    p = store.get_project(int(pid))
    if p is None:
        raise HttpError(404, "NOT_FOUND")
    require_role(ctx.user_id, p["property_id"], EDITOR)
    b = ctx.json()
    as_of = b.get("as_of", date.today().isoformat())
    pv = store.latest_project_version(int(pid))
    report = domain.dependency_report(p, pv["timeline_json"], as_of, _StorageView())
    if not report["ok"]:
        # pre-publish gate blocks; nothing is enqueued
        raise HttpError(422, "PUBLISH_BLOCKED",
                        f"{len(report['errors'])} 个阻断问题，发布中止",
                        {"checks": {k: v for k, v in report.items() if k != "frozen_items"}})
    job_id = store.enqueue_job(int(pid), pv["id"], ctx.user_id)
    export_id = None
    if app.run_jobs_inline:
        from worker.render import process_job
        result = process_job(job_id, as_of=as_of)
        if result["status"] == "failed":
            raise HttpError(422, "WORKER_RECHECK_FAILED",
                            "工作器渲染前复检未通过（授权/来源状态可能在排队期间变化）",
                            {"checks": {k: v for k, v in result["checks"].items()
                                        if k != "frozen_items"}})
        export_id = result["export_id"]
    return {"ok": True, "job_id": job_id, "export_id": export_id,
            "warnings": report["warnings"]}


@route("POST", r"/api/jobs/(?P<jid>\d+)/run")
def run_job(ctx: Ctx, jid: str):
    job = store.get_job(int(jid))
    if job is None:
        raise HttpError(404, "NOT_FOUND")
    p = store.get_project(job["project_id"])
    require_role(ctx.user_id, p["property_id"], OWNER)
    as_of = ctx.json().get("as_of", date.today().isoformat())
    from worker.render import process_job
    result = process_job(int(jid), as_of=as_of)
    return result


@route("GET", r"/api/jobs/(?P<jid>\d+)")
def get_job(ctx: Ctx, jid: str):
    job = store.get_job(int(jid))
    if job is None:
        raise HttpError(404, "NOT_FOUND")
    p = store.get_project(job["project_id"])
    require_role(ctx.user_id, p["property_id"])
    return job


@route("GET", r"/api/projects/(?P<pid>\d+)/exports")
def list_exports(ctx: Ctx, pid: str):
    p = store.get_project(int(pid))
    if p is None:
        raise HttpError(404, "NOT_FOUND")
    require_role(ctx.user_id, p["property_id"])
    return {"exports": store.list_exports(int(pid))}


@route("GET", r"/api/exports/(?P<eid>\d+)")
def get_export(ctx: Ctx, eid: str):
    e = store.get_export(int(eid))
    if e is None:
        raise HttpError(404, "NOT_FOUND")
    p = store.get_project(e["project_id"])
    require_role(ctx.user_id, p["property_id"])
    return e


class _StorageView:
    # dependency_report only needs blob existence
    @staticmethod
    def blob_exists(sha: str) -> bool:
        return blob_exists(sha)
