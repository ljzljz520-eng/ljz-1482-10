"""Repository layer: all SQL for the workbench lives here.

Entities
--------
users / properties / property_members / room_types / room_rule_versions /
room_features / assets / asset_versions / asset_licenses / projects /
project_versions / home_cursors / jobs / exports / export_outputs /
upload_sessions.

Versioning rules
----------------
* room rule: insert new version row with its own effective window; never
  overwrite an old revision (needed for history reproduction).
* project: every timeline save gets a monotonic version; optimistic
  concurrency via expected base version.
* asset: master has current_version pointer; old version rows are retained.
* export: immutable snapshot; no update path exists in code.
"""
from __future__ import annotations

import json
import uuid
from typing import Any

from .db import get_conn, now, row_to_dict, tx

# ---------------------------------------------------------------- users

def list_users() -> list[dict]:
    with get_conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM users ORDER BY id")]


def get_user(user_id: int) -> dict | None:
    with get_conn() as c:
        return row_to_dict(c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone())


def create_user(username: str, display_name: str) -> int:
    with get_conn() as c:
        cur = c.execute("INSERT INTO users(username, display_name) VALUES(?,?)",
                        (username, display_name))
        return cur.lastrowid


# ------------------------------------------------------------ properties

def create_property(name: str, address: str = "") -> int:
    with get_conn() as c:
        return c.execute("INSERT INTO properties(name, address, created_at) VALUES(?,?,?)",
                         (name, address, now())).lastrowid


def get_property(property_id: int) -> dict | None:
    with get_conn() as c:
        return row_to_dict(c.execute("SELECT * FROM properties WHERE id=?",
                                     (property_id,)).fetchone())


def list_properties_for_user(user_id: int) -> list[dict]:
    with get_conn() as c:
        return [dict(r) for r in c.execute("""
            SELECT p.*, pm.role FROM properties p
            JOIN property_members pm ON pm.property_id = p.id
            WHERE pm.user_id=? ORDER BY p.id
        """, (user_id,))]


def add_member(property_id: int, user_id: int, role: str) -> None:
    with get_conn() as c:
        c.execute("INSERT OR REPLACE INTO property_members(property_id,user_id,role) VALUES(?,?,?)",
                  (property_id, user_id, role))


def get_role(user_id: int, property_id: int) -> str | None:
    """None means no membership (access denied to project/asset ops)."""
    with get_conn() as c:
        r = c.execute("SELECT role FROM property_members WHERE user_id=? AND property_id=?",
                      (user_id, property_id)).fetchone()
        return r["role"] if r else None


# ------------------------------------------------------------ room types

def create_room_type(property_id: int, name: str) -> int:
    with get_conn() as c:
        return c.execute("INSERT INTO room_types(property_id,name,created_at) VALUES(?,?,?)",
                         (property_id, name, now())).lastrowid


def get_room_type(room_type_id: int) -> dict | None:
    with get_conn() as c:
        return row_to_dict(c.execute("SELECT * FROM room_types WHERE id=?",
                                     (room_type_id,)).fetchone())


def list_room_types(property_id: int) -> list[dict]:
    with get_conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM room_types WHERE property_id=? ORDER BY id", (property_id,))]


# ------------------------------------------------------- rule revisions

def latest_rule_version(room_type_id: int) -> int:
    with get_conn() as c:
        r = c.execute("SELECT COALESCE(MAX(version),0) v FROM room_rule_versions WHERE room_type_id=?",
                      (room_type_id,)).fetchone()
        return r["v"]


def add_rule_revision(room_type_id: int, rules: dict, effective_from: str,
                      effective_to: str | None, changed_by: int) -> int:
    """Insert a new revision; closes any open-ended previous revision the
    night before (windows must not overlap for deterministic resolution)."""
    with get_conn() as c, tx(c):
        prev = c.execute("""SELECT id, effective_from, effective_to FROM room_rule_versions
                            WHERE room_type_id=? AND effective_to IS NULL
                            ORDER BY version DESC""", (room_type_id,)).fetchone()
        if prev:
            # previous open window ends one day before the new window starts
            y, m, d = effective_from.split("-")
            from datetime import date, timedelta
            end_day = date(int(y), int(m), int(d)) - timedelta(days=1)
            c.execute("UPDATE room_rule_versions SET effective_to=? WHERE id=?",
                      (end_day.isoformat(), prev["id"]))
        version = latest_rule_version(room_type_id) + 1
        rid = c.execute("""INSERT INTO room_rule_versions
                (room_type_id, version, rules_json, effective_from, effective_to,
                 changed_by, created_at) VALUES(?,?,?,?,?,?,?)""",
                (room_type_id, version, json.dumps(rules, ensure_ascii=False),
                 effective_from, effective_to, changed_by, now())).lastrowid
        return rid


def list_rule_versions(room_type_id: int) -> list[dict]:
    with get_conn() as c:
        rows = c.execute("""SELECT rv.*, u.display_name AS changed_by_name
                            FROM room_rule_versions rv
                            LEFT JOIN users u ON u.id = rv.changed_by
                            WHERE room_type_id=? ORDER BY version""",
                         (room_type_id,)).fetchall()
        return [row_to_dict(r) for r in rows]


def get_rule_version(rule_version_id: int) -> dict | None:
    with get_conn() as c:
        return row_to_dict(c.execute(
            "SELECT * FROM room_rule_versions WHERE id=?", (rule_version_id,)).fetchone())


def effective_rule(room_type_id: int, day: str) -> dict | None:
    """Exactly one revision should be effective on `day`; pick highest version."""
    with get_conn() as c:
        rows = c.execute("""SELECT * FROM room_rule_versions
                            WHERE room_type_id=? AND effective_from<=?
                              AND (effective_to IS NULL OR effective_to>=?)
                            ORDER BY version DESC""",
                         (room_type_id, day, day)).fetchall()
        return row_to_dict(rows[0]) if rows else None


# --------------------------------------------------------------- features

def add_feature(room_type_id: int, kind: str, label: str, valid_from: str,
                valid_to: str | None, source_note: str) -> int:
    with get_conn() as c:
        return c.execute("""INSERT INTO room_features
                (room_type_id,kind,label,valid_from,valid_to,source_note,created_at)
                VALUES(?,?,?,?,?,?,?)""",
                (room_type_id, kind, label, valid_from, valid_to, source_note, now())).lastrowid


def list_features(room_type_id: int | None = None) -> list[dict]:
    with get_conn() as c:
        if room_type_id is None:
            return [dict(r) for r in c.execute("SELECT * FROM room_features ORDER BY id")]
        return [dict(r) for r in c.execute(
            "SELECT * FROM room_features WHERE room_type_id=? ORDER BY id",
            (room_type_id,))]


def get_feature(feature_id: int) -> dict | None:
    with get_conn() as c:
        return row_to_dict(c.execute("SELECT * FROM room_features WHERE id=?",
                                     (feature_id,)).fetchone())


# ----------------------------------------------------------------- assets

def create_asset(*, property_id: int, kind: str, title: str, scope: str,
                 room_type_id: int | None, space_label: str, sha256: str,
                 size_bytes: int, source_uri: str, width: int | None,
                 height: int | None, duration: float | None,
                 thumbnail_only: int, created_by: int) -> int:
    with get_conn() as c, tx(c):
        aid = c.execute("""INSERT INTO assets
                (property_id,kind,title,scope,room_type_id,space_label,
                 current_version,thumbnail_only,created_by,created_at)
                VALUES(?,?,?,?,?,?,1,?,?,?)""",
                (property_id, kind, title, scope, room_type_id, space_label,
                 thumbnail_only, created_by, now())).lastrowid
        c.execute("""INSERT INTO asset_versions
                (asset_id,version,sha256,size_bytes,source_uri,width,height,duration,created_at)
                VALUES(?,?,?,?,?,?,?,?,?)""",
                (aid, 1, sha256, size_bytes, source_uri, width, height, duration, now()))
        return aid


def get_asset(asset_id: int) -> dict | None:
    with get_conn() as c:
        return row_to_dict(c.execute("SELECT * FROM assets WHERE id=?",
                                     (asset_id,)).fetchone())


def list_assets(property_id: int) -> list[dict]:
    with get_conn() as c:
        rows = c.execute("SELECT * FROM assets WHERE property_id=? ORDER BY id",
                         (property_id,)).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["version"] = get_asset_version(r["id"], r["current_version"])
            result.append(d)
        return result


def add_asset_version(asset_id: int, *, sha256: str, size_bytes: int,
                      source_uri: str, width: int | None, height: int | None,
                      duration: float | None) -> int:
    with get_conn() as c, tx(c):
        r = c.execute("SELECT COALESCE(MAX(version),0) v FROM asset_versions WHERE asset_id=?",
                      (asset_id,)).fetchone()
        nv = r["v"] + 1
        c.execute("""INSERT INTO asset_versions
                (asset_id,version,sha256,size_bytes,source_uri,width,height,duration,created_at)
                VALUES(?,?,?,?,?,?,?,?,?)""",
                (asset_id, nv, sha256, size_bytes, source_uri, width, height, duration, now()))
        c.execute("UPDATE assets SET current_version=?, thumbnail_only=0 WHERE id=?",
                  (nv, asset_id))
        return nv


def get_asset_version(asset_id: int, version: int) -> dict | None:
    with get_conn() as c:
        return row_to_dict(c.execute("SELECT * FROM asset_versions WHERE asset_id=? AND version=?",
                                     (asset_id, version)).fetchone())


def list_asset_versions(asset_id: int) -> list[dict]:
    with get_conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM asset_versions WHERE asset_id=? ORDER BY version", (asset_id,))]


def set_source_status(asset_id: int, version: int, status: str) -> None:
    with get_conn() as c:
        c.execute("UPDATE asset_versions SET source_status=? WHERE asset_id=? AND version=?",
                  (status, asset_id, version))


# --------------------------------------------------------------- licenses

def add_license(asset_version_id: int, holder: str, valid_from: str,
                valid_to: str | None, note: str) -> int:
    with get_conn() as c:
        return c.execute("""INSERT INTO asset_licenses
                (asset_version_id,holder,valid_from,valid_to,note,created_at)
                VALUES(?,?,?,?,?,?)""",
                (asset_version_id, holder, valid_from, valid_to, note, now())).lastrowid


def get_license_for(asset_id: int, version: int) -> dict | None:
    with get_conn() as c:
        r = c.execute("""SELECT l.* FROM asset_licenses l
                         JOIN asset_versions av ON av.id = l.asset_version_id
                         WHERE av.asset_id=? AND av.version=?""",
                      (asset_id, version)).fetchone()
        return dict(r) if r else None


def withdraw_license(asset_id: int, version: int, reason: str) -> None:
    with get_conn() as c:
        c.execute("""UPDATE asset_licenses SET withdrawn=1, withdrawn_at=?, withdrawn_reason=?
                     WHERE asset_version_id=(
                        SELECT id FROM asset_versions WHERE asset_id=? AND version=?)""",
                  (now(), reason, asset_id, version))


# --------------------------------------------------------------- projects

def create_project(property_id: int, title: str, user_id: int,
                   initial_timeline: dict) -> int:
    with get_conn() as c, tx(c):
        pid = c.execute("INSERT INTO projects(property_id,title,created_by,created_at) VALUES(?,?,?,?)",
                        (property_id, title, user_id, now())).lastrowid
        c.execute("""INSERT INTO project_versions(project_id,version,timeline_json,edited_by,created_at)
                     VALUES(?,?,?,?,?)""",
                  (pid, 1, json.dumps(initial_timeline, ensure_ascii=False), user_id, now()))
        vid = None
        # pin homepage cursor to v1
        pv = c.execute("SELECT id FROM project_versions WHERE project_id=? AND version=1",
                       (pid,)).fetchone()
        c.execute("""INSERT INTO home_cursors(user_id,project_id,project_version_id,updated_at)
                     VALUES(?,?,?,?)""", (user_id, pid, pv["id"], now()))
        return pid


def get_project(project_id: int) -> dict | None:
    with get_conn() as c:
        return row_to_dict(c.execute("SELECT * FROM projects WHERE id=?",
                                     (project_id,)).fetchone())


def list_projects_for_user(user_id: int) -> list[dict]:
    with get_conn() as c:
        rows = c.execute("""SELECT p.*, pm.role, hc.project_version_id AS cursor_version_id,
                                   pv.version AS cursor_version
                            FROM projects p
                            JOIN property_members pm ON pm.property_id=p.property_id AND pm.user_id=?
                            LEFT JOIN home_cursors hc ON hc.project_id=p.id AND hc.user_id=?
                            LEFT JOIN project_versions pv ON pv.id=hc.project_version_id
                            ORDER BY p.id DESC""", (user_id, user_id)).fetchall()
        return [dict(r) for r in rows]


def get_project_version_by_id(pv_id: int) -> dict | None:
    with get_conn() as c:
        return row_to_dict(c.execute("SELECT * FROM project_versions WHERE id=?",
                                     (pv_id,)).fetchone())


def get_project_version(project_id: int, version: int) -> dict | None:
    with get_conn() as c:
        return row_to_dict(c.execute(
            "SELECT * FROM project_versions WHERE project_id=? AND version=?",
            (project_id, version)).fetchone())


def latest_project_version(project_id: int) -> dict | None:
    with get_conn() as c:
        return row_to_dict(c.execute("""SELECT * FROM project_versions
                                        WHERE project_id=? ORDER BY version DESC LIMIT 1""",
                                     (project_id,)).fetchone())


def save_project_version(project_id: int, timeline: dict, user_id: int,
                         expected_base_version: int) -> dict:
    """Optimistic save.  Raises ConflictError if someone else committed a
    version on top of the base the client edited from."""
    with get_conn() as c, tx(c):
        cur = latest_project_version(project_id)
        if cur["version"] != expected_base_version:
            raise ConflictError(
                f"project {project_id} is at v{cur['version']}, "
                f"client edited from v{expected_base_version}",
                current_version=cur["version"])
        nv = cur["version"] + 1
        cur_id = c.execute("""INSERT INTO project_versions
                     (project_id,version,timeline_json,edited_by,created_at)
                     VALUES(?,?,?,?,?)""",
                  (project_id, nv, json.dumps(timeline, ensure_ascii=False), user_id, now())).lastrowid
        return row_to_dict(c.execute(
            "SELECT * FROM project_versions WHERE id=?", (cur_id,)).fetchone())


def update_cursor(user_id: int, project_id: int, project_version_id: int) -> None:
    with get_conn() as c, tx(c):
        c.execute("""INSERT INTO home_cursors(user_id,project_id,project_version_id,updated_at)
                     VALUES(?,?,?,?)
                     ON CONFLICT(user_id,project_id) DO UPDATE SET
                        project_version_id=excluded.project_version_id,
                        updated_at=excluded.updated_at""",
                  (user_id, project_id, project_version_id, now()))


# ------------------------------------------------------------- jobs/exports

def enqueue_job(project_id: int, project_version_id: int, user_id: int) -> int:
    with get_conn() as c:
        return c.execute("""INSERT INTO jobs(project_id,project_version_id,status,created_by,created_at)
                            VALUES(?,?,'queued',?,?)""",
                         (project_id, project_version_id, user_id, now())).lastrowid


def claim_next_job() -> dict | None:
    with get_conn() as c, tx(c):
        r = c.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
        if not r:
            return None
        c.execute("UPDATE jobs SET status='running', started_at=? WHERE id=?",
                  (now(), r["id"]))
        return dict(r)


def finish_job(job_id: int, status: str, export_id: int | None = None,
               error: str = "") -> None:
    with get_conn() as c:
        c.execute("UPDATE jobs SET status=?, export_id=?, error=?, finished_at=? WHERE id=?",
                  (status, export_id, error, now(), job_id))


def get_job(job_id: int) -> dict | None:
    with get_conn() as c:
        return row_to_dict(c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())


def create_export(project_id: int, project_version_id: int, channels: list,
                  snapshot: dict, checks: dict, user_id: int) -> int:
    with get_conn() as c:
        return c.execute("""INSERT INTO exports
                (project_id,project_version_id,channels_json,snapshot_json,checks_json,created_by,created_at)
                VALUES(?,?,?,?,?,?,?)""",
                (project_id, project_version_id,
                 json.dumps(channels, ensure_ascii=False),
                 json.dumps(snapshot, ensure_ascii=False),
                 json.dumps(checks, ensure_ascii=False), user_id, now())).lastrowid


def add_export_output(export_id: int, channel: str, kind: str, path: str) -> None:
    with get_conn() as c:
        c.execute("""INSERT OR REPLACE INTO export_outputs(export_id,channel,kind,path)
                     VALUES(?,?,?,?)""", (export_id, channel, kind, path))


def get_export(export_id: int) -> dict | None:
    with get_conn() as c:
        d = row_to_dict(c.execute("SELECT * FROM exports WHERE id=?",
                                  (export_id,)).fetchone())
        if d:
            with get_conn() as c2:
                d["outputs"] = [dict(r) for r in c2.execute(
                    "SELECT * FROM export_outputs WHERE export_id=? ORDER BY channel,kind",
                    (export_id,))]
        return d


def list_exports(project_id: int) -> list[dict]:
    with get_conn() as c:
        rows = c.execute("""SELECT e.* FROM exports e
                            WHERE e.project_id=? ORDER BY e.id DESC""",
                         (project_id,)).fetchall()
        return [row_to_dict(r) for r in rows]


# ---------------------------------------------------------- upload sessions

def create_upload(upload_id: str, filename: str, total_size: int,
                  chunk_size: int, user_id: int) -> None:
    with get_conn() as c:
        c.execute("""INSERT INTO upload_sessions(id,filename,total_size,chunk_size,user_id,created_at)
                     VALUES(?,?,?,?,?,?)""",
                  (upload_id, filename, total_size, chunk_size, user_id, now()))


def get_upload(upload_id: str) -> dict | None:
    with get_conn() as c:
        r = c.execute("SELECT * FROM upload_sessions WHERE id=?", (upload_id,)).fetchone()
        return dict(r) if r else None


def bump_upload(upload_id: str, received: int, sha256: str | None = None,
                status: str | None = None) -> None:
    with get_conn() as c:
        if status == "completed":
            c.execute("UPDATE upload_sessions SET received=?, sha256=COALESCE(?,sha256),"
                      " status='completed', completed_at=? WHERE id=?",
                      (received, sha256, now(), upload_id))
        else:
            c.execute("UPDATE upload_sessions SET received=?, sha256=COALESCE(?,sha256) WHERE id=?",
                      (received, sha256, upload_id))


class ConflictError(Exception):
    """409: two people edited the same timeline item / project."""

    def __init__(self, message: str, *, current_version: int):
        super().__init__(message)
        self.current_version = current_version
