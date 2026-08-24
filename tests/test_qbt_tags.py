from anirss_lib import config
from anirss_lib.qbt import actions


class RecordingQbt:
    """Captures the fields sent to /api/v2/torrents/add."""

    def __init__(self):
        self.posts = []
        self.multiparts = []

    def post(self, endpoint, **kwargs):
        self.posts.append((endpoint, kwargs))
        return "Ok."

    def post_multipart(self, endpoint, **kwargs):
        self.multiparts.append((endpoint, kwargs))
        return "Ok."


# ---- tag normalisation ----

def test_clean_tags_trims_dedupes_and_splits_commas():
    assert actions._clean_tags(["  Anime ", "Anime", "a,b", "", "   "]) == [
        "Anime", "a", "b"]


def test_clean_tags_of_nothing_is_empty():
    assert actions._clean_tags(None) == []
    assert actions._clean_tags([]) == []


def test_tags_param_omits_the_field_when_untagged():
    assert actions._tags_param([]) == {}
    assert actions._tags_param(["Anime", "raw"]) == {"tags": "Anime,raw"}


# ---- the add paths ----

def test_do_download_sends_tags():
    qbt = RecordingQbt()
    actions.do_download(qbt, ["magnet:?x"], "Show", "/tmp/save", tags=["Anime"])
    endpoint, fields = qbt.posts[0]
    assert endpoint == "/api/v2/torrents/add"
    assert fields["tags"] == "Anime"
    assert fields["savepath"] == "/tmp/save/Show"


def test_do_download_without_tags_omits_the_field():
    qbt = RecordingQbt()
    actions.do_download(qbt, ["magnet:?x"], "Show", "/tmp/save")
    assert "tags" not in qbt.posts[0][1]


def test_do_movie_sends_tags():
    qbt = RecordingQbt()
    actions.do_movie(qbt, "A Movie", "magnet:?x", "/tmp/movies", tags=["Anime"])
    assert qbt.posts[0][1]["tags"] == "Anime"


def test_do_upload_local_torrent_sends_tags(tmp_path):
    path = tmp_path / "x.torrent"
    path.write_bytes(b"d8:announce0:e")
    qbt = RecordingQbt()
    actions.do_upload_local_torrent(qbt, str(path), "Show", "/tmp/save",
                                    tags=["Anime"])
    endpoint, fields = qbt.multiparts[0]
    assert endpoint == "/api/v2/torrents/add"
    assert fields["tags"] == "Anime"


# ---- config ----

def test_default_config_tags_everything_anime():
    assert config.DEFAULT_CONFIG["qbittorrent"]["tags"] == ["Anime"]


def test_user_config_can_override_tags(tmp_path, monkeypatch):
    p = tmp_path / "config.toml"
    p.write_text('[qbittorrent]\ntags = ["Weeb", "anirss"]\n')
    monkeypatch.setattr(config, "CONFIG_PATH", p)
    assert config.load_config()["qbittorrent"]["tags"] == ["Weeb", "anirss"]


def test_user_config_can_disable_tagging(tmp_path, monkeypatch):
    p = tmp_path / "config.toml"
    p.write_text("[qbittorrent]\ntags = []\n")
    monkeypatch.setattr(config, "CONFIG_PATH", p)
    assert config.load_config()["qbittorrent"]["tags"] == []
