from anirss_lib import config, types


def test_default_config_has_required_sections():
    cfg = config.DEFAULT_CONFIG
    assert "qbittorrent" in cfg
    assert "downloads" in cfg
    assert "search" in cfg
    assert "logging" in cfg
    assert "display" in cfg


def test_item_namedtuple_default_values():
    it = types.Item("title", "link")
    assert it.seeders == 0
    assert it.leechers == 0
    assert it.downloads == 0


def test_pick_constants():
    assert types.PICK_DONE.kind == "done"
    assert types.PICK_BACK.kind == "back"
    assert types.PICK_EXCLUDE.kind == "exclude"
    assert types.PICK_SHOW_ALL.kind == "show_all"
