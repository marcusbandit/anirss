import io
import os
from unittest.mock import MagicMock

from anirss_lib import terminal


def test_get_size_reads_from_dev_tty(monkeypatch):
    """When /dev/tty is accessible, get_size uses it (not stdout)."""
    fake_tty = MagicMock()
    fake_tty.__enter__.return_value = fake_tty
    fake_tty.fileno.return_value = 42

    def fake_open(path, *a, **kw):
        if path == "/dev/tty":
            return fake_tty
        raise FileNotFoundError(path)

    monkeypatch.setattr("builtins.open", fake_open)
    real_get = os.get_terminal_size
    monkeypatch.setattr(
        os, "get_terminal_size",
        lambda fd=None: os.terminal_size((80, 30)) if fd == 42 else real_get(fd),
    )

    size = terminal.get_size()
    assert size == os.terminal_size((80, 30))


def test_get_size_falls_back_when_no_tty(monkeypatch):
    def fake_open(*a, **kw):
        raise OSError("no tty")
    monkeypatch.setattr("builtins.open", fake_open)
    size = terminal.get_size()
    assert size.columns >= 40
    assert size.lines >= 1


class _FakeStdout(io.StringIO):
    """StringIO that also satisfies isatty() — pretends to be a real terminal."""
    def __init__(self, isatty: bool = True):
        super().__init__()
        self._isatty = isatty
    def isatty(self) -> bool:  # type: ignore[override]
        return self._isatty


def test_alt_screen_writes_enter_and_leave_when_tty(monkeypatch):
    """Entering and leaving the context emits the DEC 1049 escapes."""
    fake = _FakeStdout(isatty=True)
    monkeypatch.setattr("sys.stdout", fake)
    with terminal.alt_screen():
        mid = fake.getvalue()
        assert terminal.ALT_SCREEN_ENTER in mid
        assert terminal.ALT_SCREEN_LEAVE not in mid
    final = fake.getvalue()
    assert final.startswith(terminal.ALT_SCREEN_ENTER)
    assert final.endswith(terminal.ALT_SCREEN_LEAVE)


def test_alt_screen_restores_on_exception(monkeypatch):
    """A crash inside the block still restores the original screen."""
    fake = _FakeStdout(isatty=True)
    monkeypatch.setattr("sys.stdout", fake)
    try:
        with terminal.alt_screen():
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert fake.getvalue().endswith(terminal.ALT_SCREEN_LEAVE)


def test_alt_screen_is_noop_when_not_tty(monkeypatch):
    """Piped output mustn't emit terminal escapes — they would corrupt the pipe."""
    fake = _FakeStdout(isatty=False)
    monkeypatch.setattr("sys.stdout", fake)
    with terminal.alt_screen():
        pass
    assert fake.getvalue() == ""


def test_clear_screen_writes_when_tty(monkeypatch):
    fake = _FakeStdout(isatty=True)
    monkeypatch.setattr("sys.stdout", fake)
    terminal.clear_screen()
    assert fake.getvalue() == terminal.CLEAR_AND_HOME


def test_clear_screen_noop_when_not_tty(monkeypatch):
    fake = _FakeStdout(isatty=False)
    monkeypatch.setattr("sys.stdout", fake)
    terminal.clear_screen()
    assert fake.getvalue() == ""
