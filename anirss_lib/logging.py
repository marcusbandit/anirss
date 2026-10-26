"""File-based logging + die() for fatal exits."""

import datetime
import logging as _stdlog
import os
import sys
from typing import NoReturn, TextIO

from anirss_lib.ansi import C_DIM, C_OFF, C_RED


_LOG_FILE: TextIO | None = None


def init_log(log_path: str) -> None:
    global _LOG_FILE
    try:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        _LOG_FILE = open(log_path, "a", buffering=1, encoding="utf-8")  # line-buffered
    except OSError:
        _LOG_FILE = None


# Library embedders get every log line through the stdlib logger "anirss"
# (silent unless they configure logging); the CLI also writes anirss.log.
_STD = _stdlog.getLogger("anirss")
_STD.addHandler(_stdlog.NullHandler())
_LEVELS = {"INFO": _stdlog.INFO, "WARN": _stdlog.WARNING, "ERROR": _stdlog.ERROR}


def log(level: str, msg: str) -> None:
    _STD.log(_LEVELS.get(level, _stdlog.INFO), msg)
    if _LOG_FILE is None:
        return
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        _LOG_FILE.write(f"{ts} [{level:5}] {msg}\n")
    except OSError:
        pass


def die(msg: str) -> NoReturn:
    log("ERROR", msg)
    print(f"{C_RED}error:{C_OFF} {msg}", file=sys.stderr)
    if _LOG_FILE is not None:
        print(f"{C_DIM}log: {_LOG_FILE.name}{C_OFF}", file=sys.stderr)
    sys.exit(1)
