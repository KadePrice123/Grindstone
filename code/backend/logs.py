"""Backend logging. One rotating file under data/logs plus stderr.

Exists because two production incidents were diagnosed blind: the renderer
shows 'backend error' and nothing anywhere records why. Every unhandled route
exception, recorder failure, and refresh failure lands here with a traceback.
"""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from .db import data_dir

LOG = logging.getLogger("grindstone")


def setup() -> str:
    log_dir = data_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / "backend.log"
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S"
    )
    fh = RotatingFileHandler(path, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    LOG.setLevel(logging.INFO)
    LOG.addHandler(fh)
    LOG.addHandler(sh)
    return str(path)
