"""FastAPI app factory + local-only server entrypoint.

Binds 127.0.0.1 only. Mostly read-only; the two categorization POST routes in api.py
are the sole write path, and they go through the classify engine.
"""

from __future__ import annotations

import socket
import threading
import webbrowser
from pathlib import Path

import typer
import uvicorn
from fastapi import FastAPI
from starlette.staticfiles import StaticFiles

from bankapp.config import Config
from bankapp.web import api

_STATIC_DIR = Path(__file__).parent / "static"


def _port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    """True if a listener already holds host:port. Pre-flight so we can print a
    friendly message: uvicorn catches its own bind OSError and sys.exit(3)s, so a
    try/except around uvicorn.run() never sees it."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        probe.bind((host, port))
        return False
    except OSError:
        return True
    finally:
        probe.close()


def create_app(cfg: Config) -> FastAPI:
    app = FastAPI(title="BankApp")
    app.state.cfg = cfg
    app.include_router(api.router)
    # Static mount goes last: it's mounted at "/" with html=True (serves index.html
    # for directory requests), so routes registered after it would be unreachable.
    app.mount("/", StaticFiles(directory=_STATIC_DIR, html=True), name="static")
    return app


def serve(cfg: Config, port: int = 8377, open_browser: bool = True) -> None:
    if _port_in_use(port):
        typer.echo(f"Could not bind port {port} (already in use?). Try: finance serve --port <other>")
        raise typer.Exit(1)
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    uvicorn.run(create_app(cfg), host="127.0.0.1", port=port, log_level="info")
