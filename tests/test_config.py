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
    # Unknown stats are None, never 0 (0 means "really zero seeders").
    assert it.seeders is None
    assert it.leechers is None
    assert it.downloads is None


def test_pick_constants():
    assert types.PICK_DONE.kind == "done"
    assert types.PICK_BACK.kind == "back"
    assert types.PICK_EXCLUDE.kind == "exclude"
    assert types.PICK_SHOW_ALL.kind == "show_all"


def test_default_config_has_no_endpoints():
    assert config.build_config()["endpoint"] == []
    assert config.DEFAULT_CONFIG["bestfit"]["preferred_groups"] == []


def test_default_config_names_no_release_site_or_group():
    text = config.DEFAULT_CONFIG_TOML.lower()
    for name in ("nyaa.si", "anirena", "1337x", "erai", "subsplease", "vcb-studio"):
        assert name not in text, name


def test_load_config_synthesizes_endpoint_from_legacy_search(tmp_path, monkeypatch):
    p = tmp_path / "config.toml"
    p.write_text(
        '[search]\nnyaa_url = "https://mirror.example/"\n'
        'category = "1_2"\nfilter = "2"\n'
    )
    monkeypatch.setattr(config, "CONFIG_PATH", p)
    cfg = config.load_config()
    assert cfg["endpoint"] == [{
        "name": "nyaa", "kind": "nyaa", "url": "https://mirror.example/",
        "category": "1_2", "filter": "2",
    }]


def test_load_config_user_endpoints_win(tmp_path, monkeypatch):
    p = tmp_path / "config.toml"
    p.write_text(
        '[[endpoint]]\nname = "anirena"\nkind = "rss"\n'
        'url = "https://www.anirena.com/rss?q={query}&adult=1"\n'
    )
    monkeypatch.setattr(config, "CONFIG_PATH", p)
    cfg = config.load_config()
    assert [e["name"] for e in cfg["endpoint"]] == ["anirena"]


def test_fresh_bootstrap_has_no_endpoints(tmp_path, monkeypatch):
    path = tmp_path / "nope" / "config.toml"
    monkeypatch.setattr(config, "CONFIG_PATH", path)
    cfg = config.load_config()
    assert path.exists()
    assert cfg["endpoint"] == []
    assert config.load_config(path)["endpoint"] == []


def test_load_config_takes_the_qbittorrent_address_from_the_environment(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text('[qbittorrent]\nurl = "http://localhost:8080"\nusername = "admin"\n')
    monkeypatch.setenv("ANIRSS_QBT_URL", "http://127.0.0.1:47321")
    monkeypatch.setenv("ANIRSS_QBT_USERNAME", "anibeam")
    cfg = config.load_config(path, bootstrap=False)
    assert cfg["qbittorrent"]["url"] == "http://127.0.0.1:47321"
    assert cfg["qbittorrent"]["username"] == "anibeam"
    monkeypatch.setenv("ANIRSS_QBT_URL", " ")
    monkeypatch.delenv("ANIRSS_QBT_USERNAME")
    cfg = config.load_config(path, bootstrap=False)
    assert cfg["qbittorrent"]["url"] == "http://localhost:8080"
    assert cfg["qbittorrent"]["username"] == "admin"
