from anirss_lib.qbt import actions


class FakeQbt:
    def __init__(self, rules):
        self._rules = rules

    def get_json(self, path):
        assert path == "/api/v2/rss/rules"
        return self._rules


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
