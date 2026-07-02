"""FastAPI application wiring."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader

from ..config import get_config
from ..ssh_wrapper import OldServer, NewServer
from ..state_db import get_state_db
from ..motor_a import MotorA
from ..motor_b import MotorB
from ..polylang import PolylangEngine

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Template setup — templates/ relative to project root
# ---------------------------------------------------------------------------

_HERE = Path(__file__).parent
_ROOT = _HERE.parent.parent
# cache_size=0 disables the LRU template cache — avoids a Python 3.14
# incompatibility where weakref.ref(FileSystemLoader) is not hashable.
_jinja_env = Environment(
    loader=FileSystemLoader(str(_ROOT / "templates")),
    autoescape=True,
    cache_size=0,
)
templates = Jinja2Templates(env=_jinja_env)

# ---------------------------------------------------------------------------
# App factory and shared state
# ---------------------------------------------------------------------------

app = FastAPI(title="WP Synchro", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(_ROOT / "static")), name="static")

_executor = ThreadPoolExecutor(max_workers=4)


def submit_background(fn) -> None:
    """Submit a function to run in background without blocking the HTTP response."""
    _executor.submit(fn)

# Global engine cache (one per config profile)
_engines: Dict[str, Dict] = {}


def _get_engines() -> Dict:
    cfg = get_config()
    key = cfg.profile
    if key not in _engines:
        old = OldServer(cfg.old_server)
        new_ = NewServer(cfg.new_server)
        db = get_state_db(cfg.state_dir)
        pll = PolylangEngine(old, new_)
        _engines[key] = {
            "cfg": cfg,
            "old": old,
            "new": new_,
            "db": db,
            "motor_a": MotorA(cfg, old, new_, db),
            "motor_b": MotorB(cfg, old, new_, db, pll),
            "polylang": pll,
        }
    return _engines[key]


def _run_sync(fn, *args, **kwargs):
    """Run a blocking function in the thread pool."""
    loop = asyncio.get_event_loop()
    return loop.run_in_executor(_executor, lambda: fn(*args, **kwargs))


# ---------------------------------------------------------------------------
# Jinja2 globals / filters
# ---------------------------------------------------------------------------

def _fmt_ts(ts: Optional[float]) -> str:
    if not ts:
        return "—"
    import datetime
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


_jinja_env.filters["fmt_ts"] = _fmt_ts


# ---------------------------------------------------------------------------
# Routes — import here to register them
# ---------------------------------------------------------------------------

from .routes import dashboard, files, content, logs, database  # noqa: E402, F401

app.include_router(dashboard.router)
app.include_router(files.router)
app.include_router(content.router)
app.include_router(logs.router)
app.include_router(database.router)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "dashboard.html", {
        "cfg": get_config(),
        "active": "dashboard",
    })
