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


def test_op_flags_run_without_any_endpoint(monkeypatch, tmp_path):
    """-Q, -Sy and -R only talk to qBittorrent: a fresh config with no
    endpoint must not stop them."""
    from anirss_lib import config as config_mod
    from anirss_lib import main as main_mod
    cfg = config_mod.build_config({"logging": {"log_path": str(tmp_path / "log")}})
    assert cfg["endpoint"] == []
    monkeypatch.setattr(main_mod, "load_config", lambda *a, **k: cfg)
    monkeypatch.setattr(main_mod, "setup_readline", lambda: None)
    seen = []
    monkeypatch.setattr(main_mod, "_handle_op_flag",
                        lambda argv, c, parsed: (seen.append(argv) or (True, None)))
    monkeypatch.setattr("sys.argv", ["anirss", "-Q"])
    main_mod.main()
    assert seen == [["-Q"]]
