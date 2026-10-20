"""Writing [[endpoint]] blocks into config.toml: the rest of the file stays
as the user wrote it, and a bad spec never reaches the disk."""

import tomllib

import pytest

from anirss_lib import config
from anirss_lib.endpoint_config import add_endpoint, check_spec, remove_endpoint
from anirss_lib.errors import NotFoundError, ValidationError

NYAA = {"name": "main", "kind": "nyaa", "url": "https://one.example/"}
RSS = {"name": "fallback", "kind": "rss", "url": "https://two.example/rss?q={query}"}

HAND_WRITTEN = """\
# my anirss config
[qbittorrent]
url = "http://localhost:8080"  # local qB

# first endpoint, the default
[[endpoint]]
name = "main"
kind = "nyaa"
url = "https://one.example/"

# a note about the jackett one below
[[endpoint]]
name = "jackett"
kind = "rss"
url = "http://127.0.0.1:9117/x?apikey=KEY&q={query}"
start = "podman pod start j"
ready = "http://127.0.0.1:9117/caps"

[bestfit]
preferred_groups = ["GroupA"]
"""


def names(path):
    return [e["name"] for e in tomllib.loads(path.read_text()).get("endpoint", [])]


@pytest.fixture
def cfg(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(HAND_WRITTEN)
    return p


def test_add_goes_after_the_last_endpoint_and_keeps_the_rest(cfg):
    assert add_endpoint(cfg, check_spec(RSS)) is False
    text = cfg.read_text()
    assert names(cfg) == ["main", "jackett", "fallback"]
    for kept in ("# my anirss config", "# local qB", "# a note about the jackett one below",
                 'start = "podman pod start j"', 'preferred_groups = ["GroupA"]'):
        assert kept in text
    assert text.index("fallback") < text.index("[bestfit]")


def test_add_with_a_name_in_use_replaces_it_in_place(cfg):
    new = dict(NYAA, url="https://three.example/")
    assert add_endpoint(cfg, check_spec(new)) is True
    loaded = tomllib.loads(cfg.read_text())["endpoint"]
    assert [e["name"] for e in loaded] == ["main", "jackett"]
    assert loaded[0]["url"] == "https://three.example/"
    assert "# first endpoint, the default" in cfg.read_text()


def test_remove_keeps_comments_around_the_block(cfg):
    remove_endpoint(cfg, "main")
    text = cfg.read_text()
    assert names(cfg) == ["jackett"]
    assert "# first endpoint, the default" in text
    assert "# a note about the jackett one below" in text
    assert 'url = "http://localhost:8080"' in text


def test_remove_unknown_is_not_found_and_leaves_the_file(cfg):
    with pytest.raises(NotFoundError):
        remove_endpoint(cfg, "nope")
    assert cfg.read_text() == HAND_WRITTEN


def test_a_write_keeps_the_previous_file_as_bak(cfg):
    add_endpoint(cfg, check_spec(RSS))
    assert cfg.with_name("config.toml.bak").read_text() == HAND_WRITTEN


def test_add_to_a_fresh_default_config(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(config.DEFAULT_CONFIG_TOML)
    add_endpoint(p, check_spec(NYAA))
    add_endpoint(p, check_spec(RSS))
    assert config.load_config(p, bootstrap=False, strict=True)["endpoint"] == [
        {"name": "main", "kind": "nyaa", "url": "https://one.example/"},
        {"name": "fallback", "kind": "rss", "url": "https://two.example/rss?q={query}"},
    ]


def test_add_to_a_missing_file_creates_it(tmp_path):
    p = tmp_path / "new" / "config.toml"
    add_endpoint(p, check_spec(NYAA))
    assert names(p) == ["main"]


def test_strings_are_escaped(tmp_path):
    p = tmp_path / "config.toml"
    url = 'https://x.example/rss?q={query}&t="a\\b"'
    add_endpoint(p, check_spec({"name": "x", "kind": "rss", "url": url}))
    assert tomllib.loads(p.read_text())["endpoint"][0]["url"] == url


@pytest.mark.parametrize("spec", [
    dict(NYAA, start="rm -rf ~"),                          # commands stay hand-written
    dict(NYAA, url="ftp://one.example/"),
    dict(NYAA, url=""),
    dict(NYAA, name="a b"),
    dict(NYAA, kind="torznab"),
    dict(RSS, url="https://two.example/rss"),               # no {query}
    dict(NYAA, url=5),
    "not an object",
])
def test_check_spec_refuses(spec):
    with pytest.raises(ValidationError):
        check_spec(spec)


def test_check_spec_drops_nyaa_keys_from_rss():
    assert check_spec(dict(RSS, category="1_0", filter="2")) == RSS


LEGACY = '[search]\nnyaa_url = "https://legacy.example/"\ncategory = "1_2"\n'
LEGACY_EP = {"name": "nyaa", "kind": "nyaa", "url": "https://legacy.example/",
             "category": "1_2", "filter": "0"}


def test_a_legacy_endpoint_is_written_out_before_an_add(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(LEGACY)
    add_endpoint(p, check_spec(RSS), implicit=[LEGACY_EP])
    assert names(p) == ["nyaa", "fallback"]
    assert config.load_config(p, bootstrap=False)["search"]["nyaa_url"] == ""
    assert "# moved to [[endpoint]]: nyaa_url" in p.read_text()


def test_removing_a_legacy_endpoint_removes_it_for_good(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(LEGACY)
    remove_endpoint(p, "nyaa", implicit=[LEGACY_EP])
    assert config.load_config(p, bootstrap=False)["endpoint"] == []


def test_replacing_a_hand_written_on_demand_endpoint_is_refused(cfg):
    with pytest.raises(ValidationError, match="start"):
        add_endpoint(cfg, check_spec({"name": "jackett", "kind": "nyaa",
                                      "url": "https://x.example/"}))
    assert cfg.read_text() == HAND_WRITTEN


def test_a_symlinked_config_stays_a_symlink(tmp_path):
    real = tmp_path / "dotfiles" / "config.toml"
    real.parent.mkdir()
    real.write_text(HAND_WRITTEN)
    link = tmp_path / "config.toml"
    link.symlink_to(real)
    add_endpoint(link, check_spec(RSS))
    assert link.is_symlink()
    assert "fallback" in real.read_text()


def test_non_ascii_round_trips(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text("# café\n", encoding="utf-8")
    url = "https://x.example/rss?q={query}&lang=日本"
    add_endpoint(p, check_spec({"name": "x", "kind": "rss", "url": url}))
    assert tomllib.loads(p.read_text(encoding="utf-8"))["endpoint"][0]["url"] == url


@pytest.mark.parametrize("bad", ["\x7f", "\x01", "\ud800"])
def test_control_characters_are_refused(bad):
    with pytest.raises(ValidationError):
        check_spec(dict(RSS, url=RSS["url"] + bad))
