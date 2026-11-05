import json
import os

from anirss_lib.errors import QbtError
from anirss_lib.qbt import actions


class FakeQbt:
    def __init__(self, rules):
        self._rules = rules

    def get_json(self, path):
        assert path == "/api/v2/rss/rules"
        return self._rules


class RecordingQbt:
    """Records every post() call so do_subscribe's rule payload can be inspected."""

    def __init__(self):
        self.posts = []

    def get_json(self, path):
        return {}

    def post(self, endpoint, **kwargs):
        self.posts.append((endpoint, kwargs))
        return "Ok."


def test_unique_rule_name_no_collision():
    qbt = FakeQbt({})
    assert actions._unique_rule_name(qbt, "Show", "http://f", "anirena") == "Show"


def test_unique_rule_name_same_feed_overwrites():
    qbt = FakeQbt({"Show": {"affectedFeeds": ["http://f"]}})
    assert actions._unique_rule_name(qbt, "Show", "http://f", "anirena") == "Show"


def test_unique_rule_name_other_feed_suffixes():
    qbt = FakeQbt({"Show": {"affectedFeeds": ["http://nyaa-feed"]}})
    assert (actions._unique_rule_name(qbt, "Show", "http://anirena-feed", "anirena")
            == "Show @anirena")


def test_unique_rule_name_survives_api_errors():
    class Boom:
        def get_json(self, path):
            raise RuntimeError("down")
    assert actions._unique_rule_name(Boom(), "Show", "http://f", "x") == "Show"


def test_unique_rule_name_survives_qbt_error():
    class Dies:
        def get_json(self, path):
            raise QbtError("HTTP 500")
    assert actions._unique_rule_name(Dies(), "Show", "http://f", "x") == "Show"


def test_do_subscribe_sets_must_not_contain_in_rule():
    qbt = RecordingQbt()
    actions.do_subscribe(qbt, "http://feed", "Show", "/tmp/save",
                         must_not_contain="HEVC|dual audio")
    _, kwargs = next(p for p in qbt.posts if p[0] == "/api/v2/rss/setRule")
    rule = json.loads(kwargs["ruleDef"])
    assert rule["mustNotContain"] == "HEVC|dual audio"


def test_do_subscribe_sets_must_contain_in_rule():
    qbt = RecordingQbt()
    actions.do_subscribe(qbt, "http://feed", "Show", "/tmp/save",
                         must_contain="Show 1080p WEB-DL AVC")
    _, kwargs = next(p for p in qbt.posts if p[0] == "/api/v2/rss/setRule")
    rule = json.loads(kwargs["ruleDef"])
    assert rule["mustContain"] == "Show 1080p WEB-DL AVC"


def test_do_subscribe_default_must_not_contain_is_empty():
    qbt = RecordingQbt()
    actions.do_subscribe(qbt, "http://feed", "Show", "/tmp/save")
    _, kwargs = next(p for p in qbt.posts if p[0] == "/api/v2/rss/setRule")
    rule = json.loads(kwargs["ruleDef"])
    assert rule["mustNotContain"] == ""


def _rule_from(qbt):
    """Pull the ruleDef payload out of the recorded setRule post."""
    for endpoint, kwargs in qbt.posts:
        if endpoint == "/api/v2/rss/setRule":
            return json.loads(kwargs["ruleDef"])
    raise AssertionError("no setRule call recorded")


def test_do_subscribe_puts_tags_in_torrent_params():
    qbt = RecordingQbt()
    actions.do_subscribe(qbt, "http://feed", "Show", "/tmp/save",
                         tags=["Anime", "anirss"])
    params = _rule_from(qbt)["torrentParams"]
    assert params["tags"] == ["Anime", "anirss"]
    # torrentParams supersedes the flat savePath, so it has to carry the path
    # and pin manual torrent management or qB would apply its global TMM.
    assert params["save_path"] == os.path.join("/tmp/save", "Show")
    assert params["use_auto_tmm"] is False


def test_do_subscribe_without_tags_keeps_legacy_rule_shape():
    qbt = RecordingQbt()
    actions.do_subscribe(qbt, "http://feed", "Show", "/tmp/save")
    rule = _rule_from(qbt)
    assert "torrentParams" not in rule
    assert rule["savePath"] == os.path.join("/tmp/save", "Show")


class PrefsQbt(RecordingQbt):
    """A qBittorrent whose preferences say what a fresh install says."""

    def __init__(self, prefs):
        super().__init__()
        self.prefs = prefs

    def get_json(self, path):
        return self.prefs if path == "/api/v2/app/preferences" else {}


def test_subscribing_turns_on_rss_fetching_and_auto_download_when_off():
    qbt = PrefsQbt({"rss_processing_enabled": False,
                    "rss_auto_downloading_enabled": False})
    res = actions.subscribe_rule(qbt, "https://f.example/rss", "Show", "/dl")
    assert res.turned_on == ["rss_processing_enabled", "rss_auto_downloading_enabled"]
    (endpoint, fields), *_ = qbt.posts
    assert endpoint == "/api/v2/app/setPreferences"
    assert json.loads(fields["json"]) == {"rss_processing_enabled": True,
                                          "rss_auto_downloading_enabled": True}


def test_subscribing_leaves_rss_preferences_alone_when_on_or_unknown():
    for prefs in ({"rss_processing_enabled": True,
                   "rss_auto_downloading_enabled": True}, {}, None):
        qbt = PrefsQbt(prefs)
        res = actions.subscribe_rule(qbt, "https://f.example/rss", "Show", "/dl")
        assert res.turned_on == []
        assert all(e != "/api/v2/app/setPreferences" for e, _ in qbt.posts)


def test_a_subscription_may_leave_rss_off_and_says_it_is_off():
    qbt = PrefsQbt({"rss_processing_enabled": True,
                    "rss_auto_downloading_enabled": False})
    res = actions.subscribe_rule(qbt, "https://f.example/rss", "Show", "/dl",
                                 enable_rss=False)
    assert res.turned_on == [] and res.rss_off == ["rss_auto_downloading_enabled"]
    assert all(e != "/api/v2/app/setPreferences" for e, _ in qbt.posts)


def test_unreadable_rss_settings_are_unknown_not_on():
    assert actions.rss_prefs_off(PrefsQbt(None)) is None
    assert actions.rss_prefs_off(PrefsQbt({"locale": "en"})) is None
    assert actions.rss_prefs_off(PrefsQbt({"rss_processing_enabled": True,
                                           "rss_auto_downloading_enabled": True})) == []
