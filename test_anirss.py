"""Tests for the pure functions in anirss_lib.

Run with: uvx pytest test_anirss.py
"""

from __future__ import annotations

import http.cookiejar
import urllib.request

import pytest

from anirss_lib import bestfit
from anirss_lib import config as _cfg
from anirss_lib.cli import args as _cli_args
from anirss_lib.cli.urls import UrlKind, classify_url, extract_nyaa_query
from anirss_lib.qbt import feeds as _feeds
from anirss_lib.qbt import session as _session
from anirss_lib.qbt.actions import _human_bytes, _norm_path
from anirss_lib.qbt.session import _effective_cookie_host, _make_sid_cookie
from anirss_lib import refine as _refine
from anirss_lib.ansi import ansi_strip
from anirss_lib.refine import (
    add_exclude_to_query, auto_resolution, build_refined_query, compute_groups,
)
from anirss_lib.titles import poster_of, show_name, title_tokens
from anirss_lib.types import Item


def is_nyaa_url(s):
    return classify_url(s) == UrlKind.NYAA_RSS


parse_op_flag = _cli_args.parse_op_flag


# -------- poster_of --------

def test_poster_of_present():
    assert poster_of("[Erai-raws] Frieren - 01 [1080p].mkv") == "[Erai-raws]"


def test_poster_of_absent():
    assert poster_of("Frieren - 01 [1080p].mkv") is None


def test_poster_of_only_matches_leading_bracket():
    assert poster_of("Frieren [Erai-raws] 01.mkv") is None


# -------- show_name --------

def test_show_name_erai_raws_episode():
    title = (
        "[Erai-raws] Class de 2-banme ni Kawaii Onnanoko to Tomodachi ni Natta - 04 "
        "[1080p CR WEB-DL AVC AAC][MultiSub][D70EB2BA]"
    )
    assert show_name(title) == (
        "Class de 2-banme ni Kawaii Onnanoko to Tomodachi ni Natta"
    )


def test_show_name_simple_episode():
    assert show_name("[SubsPlease] Frieren - 01 (1080p) [BCDEF].mkv") == "Frieren"


def test_show_name_movie_no_episode_cuts_at_first_bracket():
    assert show_name("[Erai-raws] Some Movie [1080p][BCDEF].mkv") == "Some Movie"


def test_show_name_keeps_internal_dash_digits():
    assert show_name("[X] 2-banme Show - 01 [meta]") == "2-banme Show"


def test_show_name_does_not_cut_on_resolution_after_dash():
    # " - 1080p" must not match (lookahead requires space/end/[ after digits).
    assert show_name("[X] Show - 1080p [meta]") == "Show - 1080p"


def test_show_name_handles_episode_with_version():
    assert show_name("[X] Show - 01v2 [1080p]") == "Show"


def test_show_name_no_poster():
    assert show_name("Some Show - 01 [1080p].mkv") == "Some Show"


def test_show_name_cuts_at_season_episode_marker():
    title = ("[ToonsHub] Sparks of Tomorrow S01E02 1080p NF WEB-DL MULTi "
             "AAC2.0 H.264 (Nijusseiki Denki Mokuroku)")
    assert show_name(title) == "Sparks of Tomorrow"


# -------- title_tokens --------

def test_title_tokens_strips_poster_and_extension():
    tokens = title_tokens("[Erai-raws] Frieren - 01 [1080p].mkv")
    assert "Erai-raws" not in tokens
    assert "mkv" not in tokens
    assert "Frieren" in tokens
    assert "1080p" in tokens


def test_title_tokens_drops_pure_numeric():
    tokens = title_tokens("Show 01 02 1080p.mkv")
    assert "01" not in tokens
    assert "02" not in tokens
    assert "1080p" in tokens


def test_title_tokens_drops_pure_hex():
    tokens = title_tokens("Show [ABCDEF12].mkv")
    assert "ABCDEF12" not in tokens


def test_title_tokens_keeps_hex_with_letters_outside_af():
    # Hex regex requires only [0-9A-Fa-f]; presence of g-z keeps the token.
    tokens = title_tokens("Show ABCDEFG.mkv")
    assert "ABCDEFG" in tokens


def test_title_tokens_normalizes_season_episode_to_season():
    # SxxEyy pins a single episode; only the season half is a real facet.
    tokens = title_tokens("[ToonsHub] Sparks of Tomorrow S01E02 1080p WEB-DL.mkv")
    assert "S01E02" not in tokens
    assert "S01" in tokens


def test_title_tokens_drops_short():
    tokens = title_tokens("a bb ccc.mkv")
    assert "a" not in tokens
    assert "bb" in tokens
    assert "ccc" in tokens


# -------- auto_resolution --------

def _items(*titles):
    return [Item(title, f"link-{i}") for i, title in enumerate(titles)]


def test_auto_resolution_noop_when_query_already_has_resolution():
    items = _items("Show 1080p", "Show 720p")
    query, kept = auto_resolution("Show 1080p", items)
    assert query == "Show 1080p"
    assert kept == items


def test_auto_resolution_appends_highest_when_all_have_resolution():
    items = _items("Show 720p", "Show 1080p", "Show 480p")
    query, kept = auto_resolution("Show", items)
    assert query == "Show 1080p"
    assert len(kept) == 1
    assert kept[0].title == "Show 1080p"


def test_auto_resolution_skips_when_any_title_lacks_resolution():
    items = _items("Show 1080p", "Show without resolution")
    query, kept = auto_resolution("Show", items)
    assert query == "Show"
    assert kept == items


def test_auto_resolution_keeps_all_at_highest():
    items = _items("Show 1080p A", "Show 1080p B", "Show 720p")
    query, kept = auto_resolution("Show", items)
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
    groups = compute_groups(items)
    labels = {g.label for g in groups}
    assert any("[A]" in label for label in labels)
    assert any("[B]" in label for label in labels)


def test_compute_groups_empty_when_nothing_partitions():
    items = _items("Show 1080p", "Show 1080p")
    # With 2 items, refinable requires 1 < count < 2 — impossible.
    assert compute_groups(items) == []


def test_compute_groups_sorts_poster_groups_first():
    items = _items(
        "[A] Show HEVC 1080p",
        "[A] Show 1080p",
        "[B] Show HEVC 1080p",
        "[B] Show 1080p",
    )
    groups = compute_groups(items)
    assert groups, "expected at least one group"
    poster_idx = next(i for i, g in enumerate(groups) if g.has_poster)
    non_poster_idxs = [i for i, g in enumerate(groups) if not g.has_poster]
    if non_poster_idxs:
        assert poster_idx < min(non_poster_idxs)


# -------- build_refined_query --------

def test_build_refined_query_prepends_poster():
    items = _items("[A] Show 1080p", "[B] Show 1080p")
    assert build_refined_query("Show", ["[A]"], items) == "[A] Show"


def test_build_refined_query_appends_token_when_query_is_just_the_name():
    items = _items("Show HEVC 1080p", "Show 1080p")
    assert build_refined_query("Show", ["HEVC"], items) == "Show HEVC"


def test_build_refined_query_does_not_double_prepend_poster():
    items = _items("[A] Show 1080p")
    # Query already starts with a poster — the second one should not prepend.
    assert build_refined_query("[X] Show", ["[A]"], items) == "[X] Show"


def test_build_refined_query_combined_tokens():
    items = _items("[A] Show HEVC 1080p", "[A] Show 1080p", "[B] Show HEVC 1080p")
    assert build_refined_query("Show", ["[A]", "HEVC"], items) == "[A] Show HEVC"


def test_build_refined_query_inserts_token_in_title_order():
    # In the titles 1080p precedes multisub, so adding 1080p to a query that
    # already carries multisub must slot it *before* multisub, not append it.
    items = _items(
        "[A] Show 1080p multisub",
        "[B] Show 1080p multisub",
        "[C] Show 1080p multisub",
    )
    assert build_refined_query("Show multisub", ["1080p"], items) == "Show 1080p multisub"


def test_build_refined_query_keeps_exclusions_at_the_tail():
    items = _items("Show HEVC 1080p", "Show HEVC 1080p")
    # A positive token slots in front of an existing `-exclusion`.
    assert build_refined_query('Show -bad', ["HEVC"], items) == 'Show HEVC -bad'


# -------- bestfit (Try Best Fit) --------

_BESTFIT_CFG = {
    "preferred_groups": ["Erai-raws", "SubsPlease", "ASW"],
    "source_order": ["WEB-DL", "WEB", "BluRay", "WEBRip", "HDTV"],
    "preferred_resolution": "highest",
}


def test_source_of_detects_web_dl_over_bare_web():
    assert bestfit.source_of("[X] Show 1080p WEB-DL") == ("WEB-DL", "WEB-DL")
    assert bestfit.source_of("[X] Show 1080p WEBRip")[0] == "WEBRip"
    assert bestfit.source_of("[X] Show 1080p BluRay")[0] == "BluRay"
    assert bestfit.source_of("[X] Show 1080p") is None


def test_source_of_returns_literal_match_for_pinning():
    # The literal form (with whatever separator) is what gets pinned back.
    assert bestfit.source_of("[X] Show 1080p WEBDL")[1] == "WEBDL"


def test_resolution_and_subs_parsing():
    assert bestfit.resolution_of("Show 1080p") == 1080
    assert bestfit.resolution_of("Show 720p 1080p") == 1080  # highest wins
    assert bestfit.resolution_of("Show no res") == 0
    assert bestfit.has_subs("[X] Show 1080p WEB-DL MultiSub") is True
    assert bestfit.has_subs("[X] Show 1080p WEB-DL") is False
    # Dual-Audio is an audio variant, not a subtitle signal.
    assert bestfit.has_subs("[X] Show 1080p WEB-DL Dual-Audio") is False


def test_audio_variant_parsing():
    assert bestfit.audio_of("[X] Show S01E01 1080p WEB-DL DUAL AAC2.0")[0] == "Dual"
    assert bestfit.audio_of("[X] Show S01E01 1080p WEB-DL MULTi AAC2.0")[0] == "Multi"
    assert bestfit.audio_of("[X] Show - 01 (Multi-Audio)")[0] == "Multi"
    assert bestfit.audio_of("[X] Show 1080p WEB-DL MultiSub") is None
    assert bestfit.audio_of("[X] Show 1080p Multi Subs") is None


def test_best_item_prefers_dual_audio_over_multi():
    items = _items(
        "[ToonsHub] Show S01E01 1080p NF WEB-DL MULTi AAC2.0 H.264",
        "[ToonsHub] Show S01E01 1080p NF WEB-DL DUAL AAC2.0 H.264",
    )
    best = bestfit.best_item(items, _BESTFIT_CFG)
    assert "DUAL" in best.title


def test_best_item_prefers_multisub_over_audio_variant():
    # Subtitles outrank the audio variant: multi-sub beats dual-audio.
    items = _items(
        "[Erai-raws] Show 1080p WEB-DL DUAL",
        "[Erai-raws] Show 1080p WEB-DL MultiSub",
    )
    best = bestfit.best_item(items, _BESTFIT_CFG)
    assert "MultiSub" in best.title


def test_best_item_prefers_web_dl_over_webrip():
    items = _items("[Erai-raws] Show 1080p WEBRip", "[Erai-raws] Show 1080p WEB-DL")
    best = bestfit.best_item(items, _BESTFIT_CFG)
    assert best.title == "[Erai-raws] Show 1080p WEB-DL"


def test_best_item_prefers_trusted_group():
    items = _items("[Nobody] Show 1080p WEB-DL", "[Erai-raws] Show 720p WEBRip")
    # Trusted group gates first, even at lower quality.
    best = bestfit.best_item(items, _BESTFIT_CFG)
    assert best.title == "[Erai-raws] Show 720p WEBRip"


def test_best_item_prefers_highest_resolution_within_same_source():
    items = _items("[Erai-raws] Show 720p WEB-DL", "[Erai-raws] Show 1080p WEB-DL")
    best = bestfit.best_item(items, _BESTFIT_CFG)
    assert best.title == "[Erai-raws] Show 1080p WEB-DL"


def test_best_item_matches_the_users_example():
    items = _items(
        "[SubsPlease] Himekishi wa Barbaroi no Yome - 03 (1080p) [WEBRip][AB12].mkv",
        "[Erai-raws] Himekishi wa Barbaroi no Yome - 03 [1080p][WEB-DL][MultiSub].mkv",
        "[ASW] Himekishi wa Barbaroi no Yome - 03 [720p][WEB-DL].mkv",
        "[Nobody] Himekishi wa Barbaroi no Yome - 03 [2160p][WEBRip].mkv",
    )
    best = bestfit.best_item(items, _BESTFIT_CFG)
    assert best.title.startswith("[Erai-raws]")
    assert bestfit.best_fit_query(best) == \
        "[Erai-raws] Himekishi wa Barbaroi no Yome 1080p WEB-DL"


def test_best_fit_query_uses_real_title_not_truncated_search():
    # The result carries the real title; best fit rebuilds the query from it,
    # replacing whatever truncated terms the user actually searched with.
    item = Item(
        "[Erai-raws] Heroine? Seijo? Iie, All Works Maid desu (Hokori)! "
        "- 01 [1080p][WEB-DL][MultiSub]", "l")
    assert bestfit.clean_show_name(item.title) == \
        "Heroine Seijo Iie All Works Maid desu (Hokori)"
    assert bestfit.best_fit_query(item) == \
        "[Erai-raws] Heroine Seijo Iie All Works Maid desu (Hokori) 1080p WEB-DL"


def test_best_fit_query_keeps_season_drops_episode():
    item = Item("[ToonsHub] Sparks of Tomorrow S01E02 1080p NF WEB-DL DUAL "
                "AAC2.0 H.264 (Nijusseiki Denki Mokuroku)", "l")
    assert bestfit.best_fit_query(item) == \
        "[ToonsHub] Sparks of Tomorrow S01 1080p WEB-DL DUAL"


def test_best_fit_query_for_returns_refetch_query():
    items = _items("[Erai-raws] Show - 01 [1080p][WEB-DL]")
    assert _refine._best_fit_query_for("show", items, _BESTFIT_CFG) == \
        "[Erai-raws] Show 1080p WEB-DL"


def test_best_fit_query_for_empty_when_rerun_changes_nothing():
    items = _items("[Erai-raws] Show - 01 [1080p][WEB-DL]")
    assert _refine._best_fit_query_for(
        "[erai-raws] show 1080p web-dl", items, _BESTFIT_CFG) == ""


def test_pick_group_action_order(monkeypatch):
    captured = {}

    def fake_fzf(options, header, **kwargs):
        captured["options"] = [ansi_strip(opt) for opt in options]
        return ("", None, True, "")

    monkeypatch.setattr(_refine, "fzf_pick_with_query", fake_fzf)
    items = _items("[X] Show 01", "[X] Show 02")
    _refine.pick_group([], items)
    assert captured["options"] == [
        _refine.BEST_FIT, _refine.DONE, "[≡ Show All 2 Titles]",
        _refine.EXCLUDE,
    ]


def test_pick_group_hides_best_fit_when_noop(monkeypatch):
    captured = {}

    def fake_fzf(options, header, **kwargs):
        captured["options"] = [ansi_strip(opt) for opt in options]
        return ("", None, True, "")

    monkeypatch.setattr(_refine, "fzf_pick_with_query", fake_fzf)
    items = _items("[X] Show 01", "[X] Show 02")
    _refine.pick_group([], items, show_best_fit=False)
    assert _refine.BEST_FIT not in captured["options"]
    assert captured["options"][0] == _refine.DONE


def test_best_fit_query_pins_audio_variant_to_dedupe_episodes():
    # Without the audio token the refetch returns a DUAL and a MULTi copy of
    # every episode; pinning it keeps best fit at one release per episode.
    item = Item("[ToonsHub] Sparks of Tomorrow S01E01 1080p NF WEB-DL MULTi "
                "AAC2.0 H.264 (Nijusseiki Denki Mokuroku)", "l")
    assert bestfit.best_fit_query(item) == \
        "[ToonsHub] Sparks of Tomorrow S01 1080p WEB-DL MULTi"


def test_preferred_resolution_target_avoids_4k():
    cfg = {**_BESTFIT_CFG, "preferred_resolution": "1080"}
    items = _items("[Erai-raws] Show 2160p WEB-DL", "[Erai-raws] Show 1080p WEB-DL")
    best = bestfit.best_item(items, cfg)
    assert best.title == "[Erai-raws] Show 1080p WEB-DL"


# -------- saved password persistence --------

def _qbt_cfg(**over):
    cfg = {"url": "http://x", "username": "admin",
           "login_retries": 3, "save_password": True}
    cfg.update(over)
    return cfg


def _patch_pass_path(monkeypatch, tmp_path):
    monkeypatch.setattr(_session, "PASS_PATH", tmp_path / "qbt.pass")
    monkeypatch.setattr(_session, "STATE_DIR", tmp_path)


def test_password_save_load_roundtrip(tmp_path, monkeypatch):
    _patch_pass_path(monkeypatch, tmp_path)
    assert _session._load_password() is None
    assert _session._save_password("hunter2") is True
    assert _session._load_password() == "hunter2"


def test_password_file_is_mode_600(tmp_path, monkeypatch):
    import os
    import stat
    _patch_pass_path(monkeypatch, tmp_path)
    _session._save_password("secret")
    mode = stat.S_IMODE(os.stat(tmp_path / "qbt.pass").st_mode)
    assert mode == 0o600


def test_password_load_tolerates_trailing_newline(tmp_path, monkeypatch):
    _patch_pass_path(monkeypatch, tmp_path)
    (tmp_path / "qbt.pass").write_text("secret\n")
    assert _session._load_password() == "secret"


def test_drop_password_removes_file(tmp_path, monkeypatch):
    _patch_pass_path(monkeypatch, tmp_path)
    (tmp_path / "qbt.pass").write_text("x")
    _session._drop_password()
    assert not (tmp_path / "qbt.pass").exists()


def test_login_uses_saved_password_without_prompting(tmp_path, monkeypatch):
    _patch_pass_path(monkeypatch, tmp_path)
    (tmp_path / "qbt.pass").write_text("saved-pw")
    monkeypatch.setattr(_session, "_try_qbt_sid", lambda url: None)
    seen = {}

    def fake_login(base, user, pw):
        seen["pw"] = pw
        return _session.QbtSession(object(), base), None

    monkeypatch.setattr(_session, "qbt_login", fake_login)

    def boom(*a, **k):
        raise AssertionError("must not prompt when the saved password works")

    monkeypatch.setattr(_session.getpass, "getpass", boom)
    sess = _session.login_with_retry(_qbt_cfg())
    assert isinstance(sess, _session.QbtSession)
    assert seen["pw"] == "saved-pw"


def test_login_drops_rejected_saved_password_and_reprompts(tmp_path, monkeypatch):
    _patch_pass_path(monkeypatch, tmp_path)
    pass_path = tmp_path / "qbt.pass"
    pass_path.write_text("old-pw")
    monkeypatch.setattr(_session, "_try_qbt_sid", lambda url: None)
    calls = []

    def fake_login(base, user, pw):
        calls.append(pw)
        if pw == "old-pw":
            return None, "Fails."
        return _session.QbtSession(object(), base), None

    monkeypatch.setattr(_session, "qbt_login", fake_login)
    monkeypatch.setattr(_session.getpass, "getpass", lambda prompt="": "new-pw")
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")  # decline saving
    sess = _session.login_with_retry(_qbt_cfg())
    assert isinstance(sess, _session.QbtSession)
    assert calls == ["old-pw", "new-pw"]
    assert not pass_path.exists()  # the rejected password was dropped


def test_login_offers_and_saves_new_password(tmp_path, monkeypatch):
    _patch_pass_path(monkeypatch, tmp_path)
    pass_path = tmp_path / "qbt.pass"
    monkeypatch.setattr(_session, "_try_qbt_sid", lambda url: None)
    monkeypatch.setattr(
        _session, "qbt_login",
        lambda base, user, pw: (_session.QbtSession(object(), base), None))
    monkeypatch.setattr(_session.getpass, "getpass", lambda prompt="": "typed-pw")
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")  # accept saving
    sess = _session.login_with_retry(_qbt_cfg())
    assert isinstance(sess, _session.QbtSession)
    assert pass_path.read_text() == "typed-pw"


def test_login_respects_save_password_false(tmp_path, monkeypatch):
    _patch_pass_path(monkeypatch, tmp_path)
    pass_path = tmp_path / "qbt.pass"
    pass_path.write_text("saved-pw")
    monkeypatch.setattr(_session, "_try_qbt_sid", lambda url: None)
    calls = []

    def fake_login(base, user, pw):
        calls.append(pw)
        return _session.QbtSession(object(), base), None

    monkeypatch.setattr(_session, "qbt_login", fake_login)
    monkeypatch.setattr(_session.getpass, "getpass", lambda prompt="": "typed-pw")

    def no_offer(prompt=""):
        raise AssertionError("must not offer to save when save_password is false")

    monkeypatch.setattr("builtins.input", no_offer)
    sess = _session.login_with_retry(_qbt_cfg(save_password=False))
    assert isinstance(sess, _session.QbtSession)
    assert calls == ["typed-pw"]          # saved password ignored, prompt used
    assert pass_path.read_text() == "saved-pw"  # left untouched


# -------- add_exclude_to_query --------

def test_add_exclude_to_query_single_word():
    assert add_exclude_to_query("Show", "2nd") == "Show -2nd"


def test_add_exclude_to_query_quotes_term_with_spaces():
    assert add_exclude_to_query("Show", "2nd Season") == 'Show -"2nd Season"'


def test_add_exclude_to_query_strips_leading_dashes():
    assert add_exclude_to_query("Show", "-A") == "Show -A"
    assert add_exclude_to_query("Show", "--A") == "Show -A"


def test_add_exclude_to_query_empty_term_is_noop():
    assert add_exclude_to_query("Show", "") == "Show"


def test_add_exclude_to_query_dash_only_term_is_noop():
    assert add_exclude_to_query("Show", "--") == "Show"


def test_add_exclude_to_query_whitespace_only_is_noop():
    assert add_exclude_to_query("Show", "   ") == "Show"


# -------- parse_op_flag --------

def test_parse_op_flag_bare():
    assert parse_op_flag("-Q") == ("Q", set())
    assert parse_op_flag("-S") == ("S", set())
    assert parse_op_flag("-R") == ("R", set())


def test_parse_op_flag_with_modifiers():
    assert parse_op_flag("-Qj") == ("Q", {"j"})
    assert parse_op_flag("-Sy") == ("S", {"y"})
    assert parse_op_flag("-Rs") == ("R", {"s"})
    assert parse_op_flag("-Rn") == ("R", {"n"})
    assert parse_op_flag("-Rns") == ("R", {"n", "s"})


def test_parse_op_flag_ignores_long_and_unknown():
    assert parse_op_flag("--version") is None
    assert parse_op_flag("-h") is None
    assert parse_op_flag("frieren") is None
    assert parse_op_flag("") is None
    assert parse_op_flag("-") is None


def test_parse_op_flag_rejects_bad_modifier(monkeypatch):
    # `die` calls sys.exit(1); patch it to raise SystemExit so we can assert.
    # parse_op_flag lives in anirss_lib.cli.args and calls die() imported from
    # anirss_lib.logging — patch the local binding.
    captured = {}
    def fake_die(msg):
        captured["msg"] = msg
        raise SystemExit(1)
    monkeypatch.setattr(_cli_args, "die", fake_die)

    with pytest.raises(SystemExit):
        parse_op_flag("-Rz")
    assert "z" in captured["msg"]


# -------- nyaa URL detection + query extraction --------

def test_is_nyaa_url_positive():
    assert is_nyaa_url("https://nyaa.si/")
    assert is_nyaa_url("https://nyaa.si/?page=rss&q=Frieren")
    assert is_nyaa_url("http://nyaa.si/")
    assert is_nyaa_url("https://sukebei.nyaa.si/")  # subdomain


def test_is_nyaa_url_negative():
    assert not is_nyaa_url("https://example.com/")
    assert not is_nyaa_url("magnet:?xt=urn:btih:abc")
    assert not is_nyaa_url("not a url")
    assert not is_nyaa_url("")


def test_extract_nyaa_query_basic():
    url = "https://nyaa.si/?page=rss&q=Frieren&c=1_0&f=0"
    assert extract_nyaa_query(url) == "Frieren"


def test_extract_nyaa_query_url_encoded_spaces():
    url = "https://nyaa.si/?page=rss&q=Sousou+no+Frieren"
    assert extract_nyaa_query(url) == "Sousou no Frieren"
    url2 = "https://nyaa.si/?page=rss&q=Sousou%20no%20Frieren"
    assert extract_nyaa_query(url2) == "Sousou no Frieren"


def test_extract_nyaa_query_missing():
    assert extract_nyaa_query("https://nyaa.si/") is None
    assert extract_nyaa_query("https://nyaa.si/?page=rss") is None
    assert extract_nyaa_query("https://nyaa.si/?page=rss&q=") is None


# -------- _norm_path --------

def test_norm_path_strips_trailing_slash():
    assert _norm_path("/home/user/Anime/Frieren/") == "/home/user/Anime/Frieren"
    assert _norm_path("/home/user/Anime/Frieren") == "/home/user/Anime/Frieren"


def test_norm_path_collapses_redundant_slashes():
    assert _norm_path("/home//user///Anime/") == "/home/user/Anime"


def test_norm_path_expands_user(monkeypatch):
    monkeypatch.setenv("HOME", "/home/x")
    assert _norm_path("~/Anime/Frieren/") == "/home/x/Anime/Frieren"


# -------- _human_bytes --------

def test_human_bytes_units():
    assert _human_bytes(0) == "0 B"
    assert _human_bytes(1023) == "1023 B"
    assert _human_bytes(1024) == "1.0 KiB"
    assert _human_bytes(1024 * 1024) == "1.0 MiB"
    assert _human_bytes(int(2.5 * 1024 * 1024 * 1024)) == "2.5 GiB"


# -------- feed cache I/O --------
#
# The feed-cache module reads `anirss_lib.config.{FEEDS_CACHE_PATH,STATE_DIR}`
# at call time (via `_cfg.X` attribute access), so we monkeypatch the config
# module — that's what the cache code actually consults.

def test_feed_cache_round_trip(tmp_path, monkeypatch):
    cache_path = tmp_path / "feeds.txt"
    monkeypatch.setattr(_cfg, "FEEDS_CACHE_PATH", cache_path)
    monkeypatch.setattr(_cfg, "STATE_DIR", tmp_path)

    _feeds.write_feed_cache(["b feed", "A feed", "c feed"])
    # Sorted case-insensitively.
    assert _feeds.read_feed_cache() == ["A feed", "b feed", "c feed"]


def test_feed_cache_age_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(_cfg, "FEEDS_CACHE_PATH", tmp_path / "missing.txt")
    assert _feeds.feed_cache_age_seconds() is None
    assert _feeds.is_feed_cache_stale()


def test_feed_cache_stale_threshold(tmp_path, monkeypatch):
    cache_path = tmp_path / "feeds.txt"
    monkeypatch.setattr(_cfg, "FEEDS_CACHE_PATH", cache_path)
    monkeypatch.setattr(_cfg, "STATE_DIR", tmp_path)
    _feeds.write_feed_cache(["x"])
    assert not _feeds.is_feed_cache_stale(threshold_seconds=3600)
    # Threshold smaller than the smallest meaningful age -> always stale.
    assert _feeds.is_feed_cache_stale(threshold_seconds=-1)


# -------- _make_sid_cookie --------
# The `name` argument was added when qBittorrent 5.x compat shipped
# (v<5 = "SID", v5+ = "QBT_SID_<port>"). These tests cover both shapes.

def test_sid_cookie_attaches_to_outgoing_request():
    """Regression: the synthetic SID cookie must actually be sent by cookielib.

    The earlier `domain_specified=False, discard=True` combination silently
    failed to match outgoing requests, so the validation hit (/api/v2/app/version)
    went unauthenticated, qB returned 403, and the SID was dropped as 'stale'.
    """
    jar = http.cookiejar.CookieJar()
    jar.set_cookie(_make_sid_cookie("localhost", "SID", "deadbeef123", https=False))

    req = urllib.request.Request("http://localhost:8080/api/v2/app/version")
    jar.add_cookie_header(req)
    assert req.get_header("Cookie") == "SID=deadbeef123"


def test_sid_cookie_v5_name_attaches():
    """qBittorrent v5+ uses QBT_SID_<port>. The cookie must still attach."""
    jar = http.cookiejar.CookieJar()
    jar.set_cookie(_make_sid_cookie("localhost", "QBT_SID_8080", "abc123", https=False))

    req = urllib.request.Request("http://localhost:8080/api/v2/app/version")
    jar.add_cookie_header(req)
    assert req.get_header("Cookie") == "QBT_SID_8080=abc123"


def test_sid_cookie_does_not_leak_to_other_hosts():
    jar = http.cookiejar.CookieJar()
    jar.set_cookie(_make_sid_cookie("localhost", "SID", "deadbeef123", https=False))

    other = urllib.request.Request("http://example.com/api/v2/app/version")
    jar.add_cookie_header(other)
    assert other.get_header("Cookie") is None


def test_effective_cookie_host_munges_only_dotless_hostnames():
    # Bare hostname → suffixed with .local (cookielib's effective request host).
    assert _effective_cookie_host("localhost") == "localhost.local"
    assert _effective_cookie_host("qbt") == "qbt.local"
    # FQDN, IPv4, or pre-suffixed: pass through unchanged.
    assert _effective_cookie_host("qbt.example.com") == "qbt.example.com"
    assert _effective_cookie_host("192.168.1.5") == "192.168.1.5"
    assert _effective_cookie_host("nas.local") == "nas.local"
    assert _effective_cookie_host("") == ""


def test_sid_cookie_https_only_when_https_set():
    # secure=True should keep the cookie off http requests.
    jar = http.cookiejar.CookieJar()
    jar.set_cookie(_make_sid_cookie("localhost", "SID", "abc", https=True))

    http_req = urllib.request.Request("http://localhost:8080/x")
    jar.add_cookie_header(http_req)
    assert http_req.get_header("Cookie") is None  # secure cookie not sent over http

    https_req = urllib.request.Request("https://localhost:8080/x")
    jar.add_cookie_header(https_req)
    assert https_req.get_header("Cookie") == "SID=abc"
