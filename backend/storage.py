"""Content-addressed blob storage.

Blobs are keyed by sha256 (``storage/uploads/ab/cdef...``).  This is what
makes "reference master" and "copy into project" cheap and safe:

* **reference** items point at an asset master row + pinned version.  If the
  master is re-uploaded, old version rows still point at the old sha256, so
  historical timelines reproduce exactly; references to withdrawn licenses
  fail the publish-time check instead of silently changing.
* **copy** items record the same sha256 but the timeline is considered to
  own an independent copy: the publish check verifies the blob physically
  exists and never auto-updates even if the master moves on.

Since both modes ultimately resolve a sha256, copies do not duplicate bytes
on disk; the difference is legal/editorial semantics, enforced by the
dependency report.
"""
from __future__ import annotations

import hashlib
import os
import shutil
from typing import BinaryIO

ROOT = os.environ.get(
    "WORKBENCH_STORAGE", os.path.join(os.path.dirname(__file__), "..", "storage"))
UPLOAD_DIR = os.path.join(ROOT, "uploads")
OUTPUT_DIR = os.path.join(ROOT, "output")
CHUNK_DIR = os.path.join(ROOT, "chunks")


def ensure_dirs() -> None:
    for d in (UPLOAD_DIR, OUTPUT_DIR, CHUNK_DIR):
        os.makedirs(d, exist_ok=True)


def blob_path(sha256: str) -> str:
    return os.path.join(UPLOAD_DIR, sha256[:2], sha256[2:])


def blob_exists(sha256: str) -> bool:
    return bool(sha256) and os.path.exists(blob_path(sha256))


def put_blob(stream: BinaryIO) -> tuple[str, int]:
    """Stream a file into the content-addressed store.  Idempotent: the same
    bytes uploaded after a network retry land on the same path once."""
    ensure_dirs()
    h = hashlib.sha256()
    size = 0
    tmp = os.path.join(CHUNK_DIR, f"tmp-{os.getpid()}-{id(stream)}")
    with open(tmp, "wb") as out:
        while True:
            buf = stream.read(1024 * 1024)
            if not buf:
                break
            h.update(buf)
            size += len(buf)
            out.write(buf)
    digest = h.hexdigest()
    dest = blob_path(digest)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if not os.path.exists(dest):
        shutil.move(tmp, dest)
    else:                       # identical re-upload / retry
        os.remove(tmp)
    return digest, size


def put_bytes(data: bytes) -> tuple[str, int]:
    import io
    return put_blob(io.BytesIO(data))


def open_blob(sha256: str) -> BinaryIO:
    return open(blob_path(sha256), "rb")


def output_path(*parts: str) -> str:
    p = os.path.join(OUTPUT_DIR, *parts)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    return p
