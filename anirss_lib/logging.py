"""File-based logging + die() for fatal exits."""

import datetime
import os
import sys
from typing import NoReturn, TextIO

from anirss_lib.ansi import C_DIM, C_OFF, C_RED


_LOG_FILE: TextIO | None = None


def init_log(log_path: str) -> None:
    global _LOG_FILE
    try:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        _LOG_FILE = open(log_path, "a", buffering=1)  # line-buffered
    except OSError:
        _LOG_FILE = None


def log(level: str, msg: str) -> None:
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
