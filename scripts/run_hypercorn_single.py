#!/usr/bin/env python3
"""Run Hypercorn in a single foreground asyncio process.

The CLI form can spawn a worker supervisor that occasionally exits code=0 while
child processes are recycled.  This wrapper keeps systemd's child process stable
and avoids visual/cache resets caused by the outer runner restarting Hypercorn.
"""
from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path

APP_DIR = Path(os.getenv("APP_DIR", Path(__file__).resolve().parents[1])).resolve()
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))
os.chdir(str(APP_DIR))

from hypercorn.asyncio import serve
from hypercorn.config import Config

try:
    from main import app
except ModuleNotFoundError as exc:
    raise SystemExit(f"[run_hypercorn_single] unable to import main:app app_dir={APP_DIR} cwd={os.getcwd()} sys_path0={sys.path[0]} err={exc}")


async def main() -> None:
    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, shutdown.set)
        except NotImplementedError:
            pass

    cfg = Config()
    cfg.bind = [os.getenv("BROADCAST_BIND", "127.0.0.1:8000")]
    cfg.workers = 1
    cfg.loglevel = os.getenv("HYPERCORN_LOG_LEVEL", "info")
    cfg.graceful_timeout = float(os.getenv("HYPERCORN_GRACEFUL_TIMEOUT", "18"))
    cfg.keep_alive_timeout = float(os.getenv("HYPERCORN_KEEP_ALIVE_TIMEOUT", "20"))
    cfg.accesslog = os.getenv("HYPERCORN_ACCESSLOG", "-") if os.getenv("HYPERCORN_ACCESSLOG") else None

    await serve(app, cfg, shutdown_trigger=shutdown.wait)


if __name__ == "__main__":
    asyncio.run(main())
