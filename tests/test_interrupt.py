"""Ctrl-C exits without a traceback and dies by SIGINT, so shell loops stop."""
import signal
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
@pytest.mark.parametrize("where", ["import", "main"])
def test_ctrl_c_dies_by_sigint_without_traceback(where):
    # Raise KeyboardInterrupt at a known spot instead of racing a real
    # signal against startup, and without touching any config or qB.
    code = textwrap.dedent(f"""
        import sys; sys.path.insert(0, {str(ROOT)!r})
        from unittest import mock
        import anirss_lib.entry as entry
        def boom(*a, **k):
            raise KeyboardInterrupt
        if {where!r} == "import":
            real = __import__
            def imp(name, *a, **k):
                if name == "anirss_lib.main":
                    raise KeyboardInterrupt
                return real(name, *a, **k)
            with mock.patch("builtins.__import__", imp):
                entry.main()
        else:
            import anirss_lib.main
            with mock.patch("anirss_lib.main._main", boom):
                entry.main()
    """)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == -signal.SIGINT
    assert "Traceback" not in r.stderr
