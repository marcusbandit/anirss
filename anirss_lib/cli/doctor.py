"""`anirss --doctor`: prints doctor.run's checks, one line each, with the fix
under anything that is not ok. Exits 1 when something failed."""

import os
import sys

from anirss_lib.ansi import C_BLD, C_CYN, C_DIM, C_GRN, C_OFF, C_RED, C_YEL
from anirss_lib.errors import ConfigError

_MARK = {"ok": f"{C_GRN}✓{C_OFF}", "warn": f"{C_YEL}!{C_OFF}",
         "fail": f"{C_RED}✗{C_OFF}", "skip": f"{C_DIM}·{C_OFF}"}


def _short(text: str) -> str:
    home = os.path.expanduser("~")
    return text.replace(home + os.sep, "~" + os.sep) if home not in ("", os.sep) else text


def print_checks(checks) -> None:
    width = min(18, max((len(c.title) for c in checks), default=0))
    for c in checks:
        print(f"  {_MARK[c.status]} {C_BLD}{c.title:<{width}}{C_OFF}  {_short(c.detail)}")
        if c.fix and c.status in ("warn", "fail"):
            print(f"    {' ' * width}  {C_DIM}→ {c.fix}{C_OFF}")


def cmd_doctor(args: list[str]) -> None:
    from anirss_lib.api import Anirss
    from anirss_lib.config import CONFIG_PATH
    search = "--no-search" not in args
    print(f"{C_CYN}==>{C_OFF} checking the setup"
          f"{'' if search else ' (no test searches)'}")
    try:
        ani = Anirss(config_path=CONFIG_PATH)
    except ConfigError as e:
        print(f"  {_MARK['fail']} {C_BLD}config{C_OFF}  {e}")
        print(f"    {C_DIM}→ fix {CONFIG_PATH}, or move it away and run anirss --setup{C_OFF}")
        sys.exit(1)
    from anirss_lib.doctor import OK, Check
    checks = ani.doctor(search=search, fzf=True)
    print_checks([Check("config", "config", OK, str(CONFIG_PATH)), *checks])
    fails = sum(c.status == "fail" for c in checks)
    warns = sum(c.status == "warn" for c in checks)
    print()
    if not fails and not warns:
        print(f"{C_GRN}✓ all good.{C_OFF}")
    else:
        parts = [f"{n} {word}{'' if n == 1 else 's'}" for n, word in
                 ((fails, "problem"), (warns, "warning")) if n]
        print(f"{' and '.join(parts)}.")
    if fails:
        sys.exit(1)
