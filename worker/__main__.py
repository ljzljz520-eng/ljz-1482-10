"""Worker entry point: python -m worker  (queue drain loop).

Set WORKBENCH_RUN_ONCE=1 to drain one job and exit (used by tests/cron).
"""
from backend.db import init_db
from worker.render import run_loop

if __name__ == "__main__":
    init_db()
    run_loop(once=__import__("os").environ.get("WORKBENCH_RUN_ONCE") == "1")
