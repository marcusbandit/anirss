"""Op-flag `-T <path>` end-to-end behavior in _handle_op_flag."""

import pytest

from anirss_lib import main
from anirss_lib.cli.args import ParsedArgs


def _cfg_stub():
    """Minimal cfg dict — _handle_op_flag for -T doesn't touch qBittorrent."""
    return {
        "qbittorrent": {"url": "http://x", "username": "x", "login_retries": 1},
        "downloads":   {"save_base": "/tmp", "movie_path": "/tmp",
                        "hidden_base": "/tmp/hidden"},
        "search":      {"nyaa_url": "https://nyaa.si/", "category": "1_0", "filter": "2"},
        "logging":     {"log_path": "/tmp/x.log"},
        "display":     {"show_leechers": False, "force_show_seeders": False},
    }


def test_T_sets_local_torrent_path_when_file_exists(tmp_path, monkeypatch):
    """`-T <existing.torrent>` populates parsed.local_torrent_path and returns
    (False, None) so the caller continues into the upload runner.
    """
    f = tmp_path / "foo.torrent"
    f.write_bytes(b"d8:announceXe")
    parsed = ParsedArgs()
    handled, force_url = main._handle_op_flag(
        ["-T", str(f)], _cfg_stub(), parsed,
    )
    assert handled is False
    assert force_url is None
    assert parsed.local_torrent_path == str(f)


def test_T_errors_when_file_missing(tmp_path, monkeypatch):
    parsed = ParsedArgs()
    def fake_die(msg):
        raise SystemExit(1)
    monkeypatch.setattr(main, "die", fake_die)
    with pytest.raises(SystemExit):
        main._handle_op_flag(
            ["-T", str(tmp_path / "nope.torrent")], _cfg_stub(), parsed,
        )


def test_T_errors_when_path_has_wrong_suffix(tmp_path, monkeypatch):
    """`-T file.txt` should refuse — only .torrent files are accepted."""
    other = tmp_path / "not-a-torrent.txt"
    other.write_text("hello")
    parsed = ParsedArgs()
    def fake_die(msg):
        raise SystemExit(1)
    monkeypatch.setattr(main, "die", fake_die)
    with pytest.raises(SystemExit):
        main._handle_op_flag(["-T", str(other)], _cfg_stub(), parsed)


def test_T_errors_when_no_path_given(monkeypatch):
    parsed = ParsedArgs()
    def fake_die(msg):
        raise SystemExit(1)
    monkeypatch.setattr(main, "die", fake_die)
    with pytest.raises(SystemExit):
        main._handle_op_flag(["-T"], _cfg_stub(), parsed)


def test_T_rejects_combination_with_action_flags(tmp_path, monkeypatch):
    """-T + --download N is nonsensical (no nyaa items to count from)."""
    f = tmp_path / "foo.torrent"
    f.write_bytes(b"x")
    parsed = ParsedArgs(download_n=3)
    def fake_die(msg):
        raise SystemExit(1)
    monkeypatch.setattr(main, "die", fake_die)
    with pytest.raises(SystemExit):
        main._handle_op_flag(["-T", str(f)], _cfg_stub(), parsed)


def test_Th_sets_hidden_flag(tmp_path):
    """`-Th <path>` flips parsed.hidden so _save_base_for picks hidden_base."""
    f = tmp_path / "foo.torrent"
    f.write_bytes(b"x")
    parsed = ParsedArgs()
    main._handle_op_flag(["-Th", str(f)], _cfg_stub(), parsed)
    assert parsed.local_torrent_path == str(f)
    assert parsed.hidden is True


def test_Sh_sets_hidden_flag():
    """`-Sh <url>` flips parsed.hidden and returns the URL as force_url."""
    parsed = ParsedArgs()
    handled, force_url = main._handle_op_flag(
        ["-Sh", "https://nyaa.si/?page=rss&q=Frieren"], _cfg_stub(), parsed,
    )
    assert handled is False
    assert force_url == "https://nyaa.si/?page=rss&q=Frieren"
    assert parsed.hidden is True


def test_save_base_for_picks_hidden_when_flag_set():
    cfg = _cfg_stub()
    cfg["downloads"]["hidden_base"] = "/srv/media/Hentai"
    parsed = ParsedArgs(hidden=True)
    assert main._save_base_for(parsed, cfg) == "/srv/media/Hentai"


def test_save_base_for_picks_regular_when_flag_clear():
    cfg = _cfg_stub()
    cfg["downloads"]["save_base"] = "/srv/media/Anime"
    parsed = ParsedArgs(hidden=False)
    assert main._save_base_for(parsed, cfg) == "/srv/media/Anime"
