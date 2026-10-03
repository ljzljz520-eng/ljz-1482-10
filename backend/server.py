"""Dev server: serves the API, the static web UI and worker deliverables.

    python -m backend.server [port]

Set WORKBENCH_INLINE=0 to leave render jobs queued for the worker process
(`python -m worker`).
"""
from __future__ import annotations

import mimetypes
import os
import sys
from wsgiref.simple_server import make_server

from .api import app
from .db import init_db
from .storage import ensure_dirs, OUTPUT_DIR

WEB_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "web"))
_OUT = os.path.abspath(OUTPUT_DIR)


def static_app(environ, start_response):
    path = environ["PATH_INFO"]

    if path.startswith("/api") or path == "/health":
        return app(environ, start_response)

    # worker deliverables: read-only files under storage/output
    if path.startswith("/storage/output/"):
        rel = path[len("/storage/output/"):]
        full = os.path.normpath(os.path.join(_OUT, rel))
        if not full.startswith(_OUT) or not os.path.isfile(full):
            start_response("404 Not Found", [("Content-Type", "text/plain")])
            return [b"not found"]
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        with open(full, "rb") as f:
            body = f.read()
        start_response("200 OK", [("Content-Type", ctype),
                                  ("Content-Length", str(len(body)))])
        return [body]

    rel = path.lstrip("/") or "index.html"
    full = os.path.normpath(os.path.join(WEB_ROOT, rel))
    if not full.startswith(WEB_ROOT) or not os.path.isfile(full):
        full = os.path.join(WEB_ROOT, "index.html")  # SPA fallback
    ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
    with open(full, "rb") as f:
        body = f.read()
    start_response("200 OK", [("Content-Type", ctype),
                              ("Content-Length", str(len(body)))])
    return [body]


def main() -> None:
    init_db()
    ensure_dirs()
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    httpd = make_server("0.0.0.0", port, static_app)
    print(f"民宿房源视频编辑台 listening on http://127.0.0.1:{port}")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
