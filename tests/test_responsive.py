import os

import pytest

from anirss_lib import responsive, terminal


@pytest.fixture(autouse=True)
def _reset_display(monkeypatch):
    """Each test gets a known starting display config."""
    monkeypatch.setattr(responsive, "_DISPLAY", None)
    yield


def _fake_size(cols: int, lines: int = 24):
    return lambda: os.terminal_size((cols, lines))


def test_show_seeders_hidden_below_threshold(monkeypatch):
    monkeypatch.setattr(terminal, "get_size", _fake_size(responsive.SEEDERS_HIDE_BELOW - 1))
    responsive.set_display({"show_leechers": False, "force_show_seeders": False})
    assert responsive.show("seeders") is False


def test_show_seeders_visible_at_threshold(monkeypatch):
    monkeypatch.setattr(terminal, "get_size", _fake_size(responsive.SEEDERS_HIDE_BELOW))
    responsive.set_display({"show_leechers": False, "force_show_seeders": False})
    assert responsive.show("seeders") is True


def test_show_seeders_visible_when_forced_even_on_narrow(monkeypatch):
    """force_show_seeders overrides the auto-hide on tiny terminals."""
    monkeypatch.setattr(terminal, "get_size", _fake_size(40))
    responsive.set_display({"show_leechers": False, "force_show_seeders": True})
    assert responsive.show("seeders") is True


def test_show_seeders_default_when_no_set_display(monkeypatch):
    """If set_display was never called, fall back to width-based decision."""
    monkeypatch.setattr(terminal, "get_size", _fake_size(40))
    assert responsive.show("seeders") is False
    monkeypatch.setattr(terminal, "get_size", _fake_size(200))
    assert responsive.show("seeders") is True


def test_show_leechers_follows_config(monkeypatch):
    monkeypatch.setattr(terminal, "get_size", _fake_size(200))
    responsive.set_display({"show_leechers": True, "force_show_seeders": False})
    assert responsive.show("leechers") is True
    responsive.set_display({"show_leechers": False, "force_show_seeders": False})
    assert responsive.show("leechers") is False


def test_show_leechers_default_off_when_no_set_display():
    assert responsive.show("leechers") is False


def test_show_unknown_part_defaults_to_visible(monkeypatch):
    """The `_` case in the match block keeps undeclared parts visible."""
    monkeypatch.setattr(terminal, "get_size", _fake_size(40))
    assert responsive.show("widget-that-doesnt-exist") is True


def test_show_size_and_category_always_visible_by_default(monkeypatch):
    """Default thresholds for size/category are 0 — always shown."""
    monkeypatch.setattr(terminal, "get_size", _fake_size(20))
    responsive.set_display({"show_leechers": False, "force_show_seeders": False})
    assert responsive.show("size") is True
    assert responsive.show("category") is True
    assert responsive.show("downloads") is True
    assert responsive.show("title-box") is True


def test_show_size_can_be_threshold_adjusted(monkeypatch):
    """If the user raises SIZE_HIDE_BELOW, size hides below that width.

    Mirrors the user's workflow for adding a new rule: tweak the
    threshold constant in responsive.py, no other code needed.
    """
    monkeypatch.setattr(responsive, "SIZE_HIDE_BELOW", 50)
    monkeypatch.setattr(terminal, "get_size", _fake_size(40))
    assert responsive.show("size") is False
    monkeypatch.setattr(terminal, "get_size", _fake_size(60))
    assert responsive.show("size") is True


def test_split_view_returns_percentage_for_picker(monkeypatch):
    """Picker height is a fzf-style spec string so fzf re-derives it on SIGWINCH."""
    monkeypatch.setattr(terminal, "get_size", _fake_size(120, 53))
    cap, picker = responsive.split_view()
    assert picker == "50%"
    assert isinstance(cap, int)


def test_split_view_cap_matches_half_minus_overhead(monkeypatch):
    """On a 53-line terminal, cap = (53-4)//2 - TITLE_BOX_OVERHEAD = 21."""
    from anirss_lib.format import TITLE_BOX_OVERHEAD
    monkeypatch.setattr(terminal, "get_size", _fake_size(120, 53))
    cap, _ = responsive.split_view()
    expected = max(4, (53 - 4) // 2 - TITLE_BOX_OVERHEAD)
    assert cap == expected


def test_split_view_clamps_tiny_terminal(monkeypatch):
    """Even on a 12-line terminal we return at least MIN_TITLE_ROWS."""
    from anirss_lib.format import MIN_TITLE_ROWS
    monkeypatch.setattr(terminal, "get_size", _fake_size(80, 12))
    cap, _ = responsive.split_view()
    assert cap >= MIN_TITLE_ROWS


def test_format_stats_omits_size_when_threshold_raised(monkeypatch):
    """If the user adds a hide-size rule, format_stats stops emitting it."""
    from anirss_lib.ansi import ansi_strip
    from anirss_lib.format import format_stats
    from anirss_lib.types import Item
    monkeypatch.setattr(responsive, "SIZE_HIDE_BELOW", 100)
    monkeypatch.setattr(terminal, "get_size", _fake_size(60))
    responsive.set_display({"show_leechers": False, "force_show_seeders": True})
    item = Item(
        title="x", link="x", seeders=10, leechers=0,
        downloads=500, size="1.4 GiB", category="Anime",
    )
    plain = ansi_strip(format_stats(item))
    assert "1.4 GiB" not in plain
    assert "500 dl" in plain


def test_format_stats_omits_seeders_on_narrow_terminal(monkeypatch):
    """End-to-end: format_stats should drop the seeders segment under 70 cols."""
    from anirss_lib.ansi import ansi_strip
    from anirss_lib.format import format_stats
    from anirss_lib.types import Item

    item = Item(
        title="[Group] Show - 01 [1080p].mkv",
        link="magnet:?x", seeders=1234, leechers=10,
        downloads=5000, size="1.4 GiB", category="Anime",
    )
    monkeypatch.setattr(terminal, "get_size", _fake_size(40))
    responsive.set_display({"show_leechers": False, "force_show_seeders": False})
    plain = ansi_strip(format_stats(item))
    assert "1234s" not in plain
    assert "5000 dl" in plain
    assert "1.4 GiB" in plain


def test_format_stats_includes_seeders_when_forced(monkeypatch):
    """force_show_seeders keeps the column visible even at 40 columns."""
    from anirss_lib.ansi import ansi_strip
    from anirss_lib.format import format_stats
    from anirss_lib.types import Item

    item = Item(
        title="x", link="x", seeders=1234, leechers=10,
        downloads=5000, size="1.4 GiB", category="Anime",
    )
    monkeypatch.setattr(terminal, "get_size", _fake_size(40))
    responsive.set_display({"show_leechers": False, "force_show_seeders": True})
    plain = ansi_strip(format_stats(item))
    assert "1234s" in plain
