import os
import subprocess
import sys

from anirss_lib import compat, ondemand
from anirss_lib.qbt import actions


def test_lock_file_round_trip(tmp_path):
    with open(tmp_path / "x.lock", "w") as fh:
        compat.lock_file(fh)
        compat.unlock_file(fh)
        compat.lock_file(fh)
        compat.unlock_file(fh)


def test_pid_alive_sees_self_and_a_finished_child():
    assert compat.pid_alive(os.getpid())
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    assert not compat.pid_alive(child.pid)


def test_ondemand_uses_the_helper():
    assert ondemand._pid_alive(os.getpid())


def test_child_flags_is_a_dict():
    assert isinstance(compat.child_flags(), dict)
    assert isinstance(compat.detached_flags(), dict)


def test_norm_path_strips_trailing_separators(tmp_path):
    assert actions._norm_path(str(tmp_path) + "/") == str(tmp_path)
    assert actions._norm_path("/") == ""


def test_norm_path_strips_backslash_only_on_windows(monkeypatch):
    # On Windows the same call must drop a trailing backslash too.
    if os.name == "nt":
        assert actions._norm_path("C:\\anime\\") == "C:\\anime"
        assert actions._norm_path("C:/anime/") == "C:\\anime"
