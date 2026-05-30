import io

from anirss_lib import main
from anirss_lib.types import Item


def _items(n):
    """n synthetic items with downloads = 100, 99, 98, ..."""
    return [Item(title=f"Show ep{i} 1080p", link=f"link-{i}",
                 downloads=100 - i, seeders=10)
            for i in range(n)]


def test_pick_top_n_sorts_by_downloads():
    items = _items(5)
    chosen = main._pick_top_n(items, 3)
    assert len(chosen) == 3
    assert chosen[0].downloads == 100
    assert chosen[1].downloads == 99
    assert chosen[2].downloads == 98


def test_pick_top_n_clamps_to_available():
    items = _items(3)
    chosen = main._pick_top_n(items, 10)
    assert len(chosen) == 3


def test_resolve_password_env(monkeypatch):
    monkeypatch.setenv("ANIRSS_QBT_PASSWORD", "secret")
    assert main._resolve_password(password_stdin=False) == "secret"


def test_resolve_password_stdin(monkeypatch):
    monkeypatch.delenv("ANIRSS_QBT_PASSWORD", raising=False)
    monkeypatch.setattr("sys.stdin", io.StringIO("from-stdin\n"))
    assert main._resolve_password(password_stdin=True) == "from-stdin"


def test_resolve_password_env_wins_over_stdin(monkeypatch):
    monkeypatch.setenv("ANIRSS_QBT_PASSWORD", "env-pass")
    monkeypatch.setattr("sys.stdin", io.StringIO("from-stdin\n"))
    # Env var preferred, stdin is not consumed.
    assert main._resolve_password(password_stdin=True) == "env-pass"


def test_resolve_password_neither_returns_none(monkeypatch):
    monkeypatch.delenv("ANIRSS_QBT_PASSWORD", raising=False)
    assert main._resolve_password(password_stdin=False) is None
