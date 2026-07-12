import json

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


def test_unique_rule_name_survives_die_style_exit():
    class Dies:
        def get_json(self, path):
            raise SystemExit(1)
    assert actions._unique_rule_name(Dies(), "Show", "http://f", "x") == "Show"


def test_do_subscribe_sets_must_not_contain_in_rule():
    qbt = RecordingQbt()
    actions.do_subscribe(qbt, "http://feed", "Show", "/tmp/save",
                         must_not_contain="HEVC|dual audio")
    _, kwargs = next(p for p in qbt.posts if p[0] == "/api/v2/rss/setRule")
    rule = json.loads(kwargs["ruleDef"])
    assert rule["mustNotContain"] == "HEVC|dual audio"


def test_do_subscribe_default_must_not_contain_is_empty():
    qbt = RecordingQbt()
    actions.do_subscribe(qbt, "http://feed", "Show", "/tmp/save")
    _, kwargs = next(p for p in qbt.posts if p[0] == "/api/v2/rss/setRule")
    rule = json.loads(kwargs["ruleDef"])
    assert rule["mustNotContain"] == ""
