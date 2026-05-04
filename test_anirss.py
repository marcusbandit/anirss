"""Tests for the pure functions in anirss.

The script ships without a .py extension, so we load it via importlib.
Run with: uvx pytest test_anirss.py
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
from pathlib import Path

_ANIRSS_PATH = Path(__file__).parent / "anirss"
_loader = importlib.machinery.SourceFileLoader("anirss", str(_ANIRSS_PATH))
_spec = importlib.util.spec_from_loader("anirss", _loader)
assert _spec is not None
anirss = importlib.util.module_from_spec(_spec)
sys.modules["anirss"] = anirss
_loader.exec_module(anirss)


# -------- poster_of --------

def test_poster_of_present():
    assert anirss.poster_of("[Erai-raws] Frieren - 01 [1080p].mkv") == "[Erai-raws]"


def test_poster_of_absent():
    assert anirss.poster_of("Frieren - 01 [1080p].mkv") is None


def test_poster_of_only_matches_leading_bracket():
    assert anirss.poster_of("Frieren [Erai-raws] 01.mkv") is None


# -------- show_name --------

def test_show_name_erai_raws_episode():
    title = (
        "[Erai-raws] Class de 2-banme ni Kawaii Onnanoko to Tomodachi ni Natta - 04 "
        "[1080p CR WEB-DL AVC AAC][MultiSub][D70EB2BA]"
    )
    assert anirss.show_name(title) == (
        "Class de 2-banme ni Kawaii Onnanoko to Tomodachi ni Natta"
    )


def test_show_name_simple_episode():
    assert anirss.show_name("[SubsPlease] Frieren - 01 (1080p) [BCDEF].mkv") == "Frieren"


def test_show_name_movie_no_episode_cuts_at_first_bracket():
    assert anirss.show_name("[Erai-raws] Some Movie [1080p][BCDEF].mkv") == "Some Movie"


def test_show_name_keeps_internal_dash_digits():
    # "2-banme" should not be confused with an episode marker (no spaces around dash).
    assert anirss.show_name("[X] 2-banme Show - 01 [meta]") == "2-banme Show"


def test_show_name_does_not_cut_on_resolution_after_dash():
    # " - 1080p" must not match (lookahead requires space/end/[ after digits).
    assert anirss.show_name("[X] Show - 1080p [meta]") == "Show - 1080p"


def test_show_name_handles_episode_with_version():
    assert anirss.show_name("[X] Show - 01v2 [1080p]") == "Show"


def test_show_name_no_poster():
    assert anirss.show_name("Some Show - 01 [1080p].mkv") == "Some Show"


# -------- title_tokens --------

def test_title_tokens_strips_poster_and_extension():
    tokens = anirss.title_tokens("[Erai-raws] Frieren - 01 [1080p].mkv")
    assert "Erai-raws" not in tokens
    assert "mkv" not in tokens
    assert "Frieren" in tokens
    assert "1080p" in tokens


def test_title_tokens_drops_pure_numeric():
    tokens = anirss.title_tokens("Show 01 02 1080p.mkv")
    assert "01" not in tokens
    assert "02" not in tokens
    assert "1080p" in tokens


def test_title_tokens_drops_pure_hex():
    tokens = anirss.title_tokens("Show [ABCDEF12].mkv")
    assert "ABCDEF12" not in tokens


def test_title_tokens_keeps_hex_with_letters_outside_af():
    # Hex regex requires only [0-9A-Fa-f]; presence of g-z keeps the token.
    tokens = anirss.title_tokens("Show ABCDEFG.mkv")
    assert "ABCDEFG" in tokens


def test_title_tokens_drops_short():
    tokens = anirss.title_tokens("a bb ccc.mkv")
    assert "a" not in tokens
    assert "bb" in tokens
    assert "ccc" in tokens


# -------- auto_resolution --------

def _items(*titles):
    return [anirss.Item(title, f"link-{i}") for i, title in enumerate(titles)]


def test_auto_resolution_noop_when_query_already_has_resolution():
    items = _items("Show 1080p", "Show 720p")
    query, kept = anirss.auto_resolution("Show 1080p", items)
    assert query == "Show 1080p"
    assert kept == items


def test_auto_resolution_appends_highest_when_all_have_resolution():
    items = _items("Show 720p", "Show 1080p", "Show 480p")
    query, kept = anirss.auto_resolution("Show", items)
    assert query == "Show 1080p"
    assert len(kept) == 1
    assert kept[0].title == "Show 1080p"


def test_auto_resolution_skips_when_any_title_lacks_resolution():
    items = _items("Show 1080p", "Show without resolution")
    query, kept = anirss.auto_resolution("Show", items)
    assert query == "Show"
    assert kept == items


def test_auto_resolution_keeps_all_at_highest():
    items = _items("Show 1080p A", "Show 1080p B", "Show 720p")
    query, kept = anirss.auto_resolution("Show", items)
    assert query == "Show 1080p"
    titles = [item.title for item in kept]
    assert "Show 1080p A" in titles
    assert "Show 1080p B" in titles
    assert "Show 720p" not in titles


# -------- compute_groups --------

def test_compute_groups_partitions_by_poster():
    items = _items(
        "[A] Show 1080p",
        "[A] Show 720p",
        "[B] Show 1080p",
        "[B] Show 720p",
    )
    groups = anirss.compute_groups(items)
    labels = {g.label for g in groups}
    # Both posters partition the set in half (count 2 of 4).
    assert any("[A]" in label for label in labels)
    assert any("[B]" in label for label in labels)


def test_compute_groups_empty_when_nothing_partitions():
    items = _items("Show 1080p", "Show 1080p")
    # With 2 items, refinable requires 1 < count < 2 — impossible.
    assert anirss.compute_groups(items) == []


def test_compute_groups_sorts_poster_groups_first():
    items = _items(
        "[A] Show HEVC 1080p",
        "[A] Show 1080p",
        "[B] Show HEVC 1080p",
        "[B] Show 1080p",
    )
    groups = anirss.compute_groups(items)
    assert groups, "expected at least one group"
    # Poster groups should appear before non-poster groups.
    poster_idx = next(i for i, g in enumerate(groups) if g.has_poster)
    non_poster_idxs = [i for i, g in enumerate(groups) if not g.has_poster]
    if non_poster_idxs:
        assert poster_idx < min(non_poster_idxs)


# -------- apply_pick --------

def test_apply_pick_filters_by_poster_and_prepends_query():
    items = _items("[A] Show 1080p", "[B] Show 1080p")
    result = anirss.apply_pick(items, "Show", ["[A]"])
    assert result is not None
    selected, query = result
    assert len(selected) == 1
    assert selected[0].title == "[A] Show 1080p"
    assert query == "[A] Show"


def test_apply_pick_filters_by_token_and_appends_query():
    items = _items("Show HEVC 1080p", "Show 1080p")
    result = anirss.apply_pick(items, "Show", ["HEVC"])
    assert result is not None
    selected, query = result
    assert len(selected) == 1
    assert selected[0].title == "Show HEVC 1080p"
    assert query == "Show HEVC"


def test_apply_pick_returns_none_when_filter_yields_empty():
    items = _items("[A] Show 1080p")
    result = anirss.apply_pick(items, "Show", ["[B]"])
    assert result is None


def test_apply_pick_does_not_double_prepend_poster():
    items = _items("[A] Show 1080p")
    result = anirss.apply_pick(items, "[X] Show", ["[A]"])
    assert result is not None
    _, query = result
    # Query already starts with a poster — second one should not prepend.
    assert query == "[X] Show"


def test_apply_pick_combined_tokens():
    items = _items("[A] Show HEVC 1080p", "[A] Show 1080p", "[B] Show HEVC 1080p")
    result = anirss.apply_pick(items, "Show", ["[A]", "HEVC"])
    assert result is not None
    selected, query = result
    assert len(selected) == 1
    assert selected[0].title == "[A] Show HEVC 1080p"
    assert query == "[A] Show HEVC"
