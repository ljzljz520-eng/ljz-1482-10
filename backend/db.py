"""SQLite persistence layer for the homestay video editing workbench.

Everything is append-only / versioned where business rules require it
(asset versions, room rule versions, project versions, exports are never
mutated after creation).  Physical blobs live under storage/ and are
content-addressed by sha256, so "copy into project" and "reference master"
share bytes on disk while differing in licensing semantics.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any

DB_PATH = os.environ.get(
    "WORKBENCH_DB", os.path.join(os.path.dirname(__file__), "..", "data", "workbench.db")
)

_lock = threading.Lock()


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY,
    username    TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS properties (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    address    TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS room_types (
    id          INTEGER PRIMARY KEY,
    property_id INTEGER NOT NULL REFERENCES properties(id),
    name        TEXT NOT NULL,
    created_at  REAL NOT NULL
);

-- room_type rule revisions (check-in / check-out / pets / smoking ...).
CREATE TABLE IF NOT EXISTS room_rule_versions (
    id           INTEGER PRIMARY KEY,
    room_type_id INTEGER NOT NULL REFERENCES room_types(id),
    version      INTEGER NOT NULL,
    rules_json   TEXT NOT NULL,           -- {check_in, check_out, pets, smoking, note}
    effective_from TEXT NOT NULL,         -- YYYY-MM-DD inclusive
    effective_to   TEXT,                  -- NULL = open-ended
    changed_by   INTEGER REFERENCES users(id),
    created_at   REAL NOT NULL,
    UNIQUE(room_type_id, version)
);

-- facilities / window views, bound to a concrete room type with validity.
CREATE TABLE IF NOT EXISTS room_features (
    id           INTEGER PRIMARY KEY,
    room_type_id INTEGER NOT NULL REFERENCES room_types(id),
    kind         TEXT NOT NULL CHECK(kind IN ('facility','window_view')),
    label        TEXT NOT NULL,
    valid_from   TEXT NOT NULL,
    valid_to     TEXT,                    -- NULL = still valid
    source_note  TEXT NOT NULL DEFAULT '',
    created_at   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS property_members (
    id          INTEGER PRIMARY KEY,
    property_id INTEGER NOT NULL REFERENCES properties(id),
    user_id     INTEGER NOT NULL REFERENCES users(id),
    role        TEXT NOT NULL CHECK(role IN ('owner','editor','viewer')),
    UNIQUE(property_id, user_id)
);

-- master media assets.
CREATE TABLE IF NOT EXISTS assets (
    id           INTEGER PRIMARY KEY,
    property_id  INTEGER NOT NULL REFERENCES properties(id),
    kind         TEXT NOT NULL CHECK(kind IN ('video','image','audio')),
    title        TEXT NOT NULL,
    scope        TEXT NOT NULL CHECK(scope IN ('shared','private')),
    -- for scope='private', the room type this asset belongs to (NULL for shared)
    room_type_id INTEGER REFERENCES room_types(id),
    space_label  TEXT NOT NULL DEFAULT '',         -- e.g. 大厅 / 大床房A
    current_version INTEGER NOT NULL DEFAULT 1,
    thumbnail_only INTEGER NOT NULL DEFAULT 0,     -- 1 = proxy only, no full-res source
    created_by   INTEGER REFERENCES users(id),
    created_at   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS asset_versions (
    id            INTEGER PRIMARY KEY,
    asset_id      INTEGER NOT NULL REFERENCES assets(id),
    version       INTEGER NOT NULL,
    sha256        TEXT NOT NULL,
    size_bytes    INTEGER NOT NULL,
    source_uri    TEXT NOT NULL,                  -- original photo/video source
    source_status TEXT NOT NULL DEFAULT 'ok'
                  CHECK(source_status IN ('ok','unreachable')),
    width         INTEGER,
    height        INTEGER,
    duration      REAL,
    created_at    REAL NOT NULL,
    UNIQUE(asset_id, version)
);

-- license grant for an asset version; withdrawal is recorded in place.
CREATE TABLE IF NOT EXISTS asset_licenses (
    id              INTEGER PRIMARY KEY,
    asset_version_id INTEGER NOT NULL REFERENCES asset_versions(id),
    holder          TEXT NOT NULL DEFAULT '',
    valid_from      TEXT NOT NULL,
    valid_to        TEXT,                         -- NULL = perpetual
    withdrawn       INTEGER NOT NULL DEFAULT 0,
    withdrawn_at    REAL,
    withdrawn_reason TEXT NOT NULL DEFAULT '',
    note            TEXT NOT NULL DEFAULT '',
    created_at      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    id          INTEGER PRIMARY KEY,
    property_id INTEGER NOT NULL REFERENCES properties(id),
    title       TEXT NOT NULL,
    created_by  INTEGER REFERENCES users(id),
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS project_versions (
    id           INTEGER PRIMARY KEY,
    project_id   INTEGER NOT NULL REFERENCES projects(id),
    version      INTEGER NOT NULL,
    timeline_json TEXT NOT NULL,                 -- frozen timeline document
    edited_by    INTEGER REFERENCES users(id),
    created_at   REAL NOT NULL,
    UNIQUE(project_id, version)
);

-- cursor the homepage holds; points at an exact project version.
CREATE TABLE IF NOT EXISTS home_cursors (
    id          INTEGER PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id),
    project_id  INTEGER NOT NULL REFERENCES projects(id),
    project_version_id INTEGER NOT NULL REFERENCES project_versions(id),
    updated_at  REAL NOT NULL,
    UNIQUE(user_id, project_id)
);

CREATE TABLE IF NOT EXISTS jobs (
    id          INTEGER PRIMARY KEY,
    project_id  INTEGER NOT NULL REFERENCES projects(id),
    project_version_id INTEGER NOT NULL REFERENCES project_versions(id),
    kind        TEXT NOT NULL DEFAULT 'render',
    status      TEXT NOT NULL CHECK(status IN ('queued','running','done','failed')),
    export_id   INTEGER REFERENCES exports(id),
    error       TEXT NOT NULL DEFAULT '',
    created_by  INTEGER REFERENCES users(id),
    created_at  REAL NOT NULL,
    started_at  REAL,
    finished_at REAL
);

CREATE TABLE IF NOT EXISTS exports (
    id           INTEGER PRIMARY KEY,
    project_id   INTEGER NOT NULL REFERENCES projects(id),
    project_version_id INTEGER NOT NULL REFERENCES project_versions(id),
    channels_json TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,                -- frozen truth at production time
    checks_json   TEXT NOT NULL,                -- dependency / safe-area report
    created_by   INTEGER REFERENCES users(id),
    created_at   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS export_outputs (
    id         INTEGER PRIMARY KEY,
    export_id  INTEGER NOT NULL REFERENCES exports(id),
    channel    TEXT NOT NULL,
    kind       TEXT NOT NULL,                 -- subtitles|shot_list|contact_sheet|preview
    path       TEXT NOT NULL,
    UNIQUE(export_id, channel, kind)
);

-- resumable upload sessions (chunked, retry-safe).
CREATE TABLE IF NOT EXISTS upload_sessions (
    id           TEXT PRIMARY KEY,             -- uuid
    filename     TEXT NOT NULL,
    total_size   INTEGER NOT NULL,
    chunk_size   INTEGER NOT NULL,
    received     INTEGER NOT NULL DEFAULT 0,
    sha256       TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'open'
                 CHECK(status IN ('open','completed','aborted')),
    user_id      INTEGER REFERENCES users(id),
    created_at   REAL NOT NULL,
    completed_at REAL
);
"""


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db() -> None:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with _lock, get_conn() as conn:
        conn.executescript(SCHEMA)


@contextmanager
def tx(conn: sqlite3.Connection):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def now() -> float:
    return time.time()


# ---- tiny row helpers ----------------------------------------------------

def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    d = dict(row)
    for key in ("rules_json", "timeline_json", "channels_json",
                "snapshot_json", "checks_json"):
        if key in d and isinstance(d[key], str):
            d[key] = json.loads(d[key])
    return d


def parse_date(s: str) -> tuple[int, int, int]:
    y, m, dd = s.split("-")
    return int(y), int(m), int(dd)


def date_le(a: str, b: str) -> bool:
    return parse_date(a) <= parse_date(b)


def active_on(valid_from: str, valid_to: str | None, day: str) -> bool:
    """Inclusive-range membership test for YYYY-MM-DD dates."""
    if day < valid_from:
        return False
    if valid_to and day > valid_to:
        return False
    return True
