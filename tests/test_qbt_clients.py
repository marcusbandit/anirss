"""Several qBittorrent clients: [[qbittorrent.client]] in the config, `-c` on
the CLI, and `client` through the library and the HTTP API."""

import http.client
import json
import os
import threading

import pytest

from anirss_lib import config as config_mod
from anirss_lib import server
from anirss_lib.api import Anirss
from anirss_lib.cli.args import take_client_flag
from anirss_lib.errors import ConfigError, NotFoundError, QbtAuthError, ValidationError
from anirss_lib.qbt import session as session_mod
from anirss_lib.qbt.clients import DEFAULT_CLIENT, local_files, qbt_client, qbt_clients

SEEDBOX = "http://192.0.2.10:8080"
LOCAL = "http://localhost:8080"
MAGNET = "magnet:?xt=urn:btih:" + "ab" * 20 + "&dn=x"
TWO = {"qbittorrent": {"username": "me", "tags": ["Anime"], "client": [
    {"name": "seedbox", "url": SEEDBOX},
    {"name": "local", "url": LOCAL, "username": "admin", "tags": ["Local"]},
]}}


@pytest.fixture(autouse=True)
def isolate_state(tmp_path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setattr(session_mod, "STATE_DIR", state)
    monkeypatch.setattr(session_mod, "SID_PATH", state / "qbt.sid")
    monkeypatch.setattr(session_mod, "PASS_PATH", state / "qbt.pass")
    monkeypatch.delenv("ANIRSS_QBT_PASSWORD", raising=False)
    monkeypatch.delenv("ANIRSS_QBT_URL", raising=False)
    monkeypatch.delenv("ANIRSS_QBT_USERNAME", raising=False)
    return state


# ---- config -------------------------------------------------------------

def test_a_config_without_blocks_has_one_default_client():
    clients = qbt_clients(config_mod.build_config({"qbittorrent": {"url": SEEDBOX}}))
    assert [c["name"] for c in clients] == [DEFAULT_CLIENT]
    assert clients[0]["url"] == SEEDBOX and clients[0]["username"] == "admin"


def test_a_block_takes_what_it_leaves_out_from_qbittorrent():
    seedbox, local = qbt_clients(config_mod.build_config(TWO))
    assert (seedbox["name"], seedbox["url"], seedbox["username"]) == ("seedbox", SEEDBOX, "me")
    assert seedbox["tags"] == ["Anime"] and seedbox["login_retries"] == 3
    assert (local["url"], local["username"], local["tags"]) == (LOCAL, "admin", ["Local"])
    assert "client" not in seedbox


def test_the_first_block_is_the_default_and_a_name_picks_another():
    cfg = config_mod.build_config(TWO)
    assert qbt_client(cfg)["name"] == "seedbox"
    assert qbt_client(cfg, "local")["url"] == LOCAL
    with pytest.raises(NotFoundError, match="configured: seedbox, local"):
        qbt_client(cfg, "nas")


def test_a_resolved_client_resolves_to_itself():
    # `anirss -c local --serve` hands the server one resolved client.
    cfg = config_mod.build_config(TWO)
    cfg["qbittorrent"] = qbt_client(cfg, "local")
    assert [(c["name"], c["url"]) for c in qbt_clients(cfg)] == [("local", LOCAL)]


@pytest.mark.parametrize("blocks, message", [
    ([{"url": LOCAL}], "missing `name`"),
    ([{"name": "a", "url": LOCAL}, {"name": "a", "url": SEEDBOX}], "duplicate name"),
    ([{"name": "my box", "url": LOCAL}], "invalid name"),
    ([{"name": "../up", "url": LOCAL}], "invalid name"),
    ([{"name": "local"}], "missing `url`"),
    ([{"name": "local", "url": LOCAL, "tags": "Anime"}], "must be a list"),
    ([{"name": "env-localhost_8080", "url": LOCAL}], "kept for ANIRSS_QBT_URL"),
])
def test_an_invalid_block_is_a_config_error(blocks, message):
    cfg = config_mod.build_config({"qbittorrent": {"client": blocks}})
    with pytest.raises(ConfigError, match=message):
        qbt_clients(cfg)


def test_a_name_without_blocks_is_checked_too():
    cfg = config_mod.build_config({"qbittorrent": {"name": "../x"}})
    with pytest.raises(ConfigError, match="invalid name"):
        qbt_clients(cfg)


def test_an_invalid_block_fails_the_library_at_construction():
    with pytest.raises(ConfigError, match="missing `url`"):
        Anirss({"qbittorrent": {"client": [{"name": "local"}]}})


def test_the_environment_url_replaces_the_configured_clients(tmp_path, monkeypatch):
    # An app that names its own qBittorrent (AniBeam) gets that one and no other.
    path = tmp_path / "config.toml"
    path.write_text('[qbittorrent]\nurl = "http://localhost:8080"\n'
                    '[[qbittorrent.client]]\nname = "seedbox"\nurl = "http://192.0.2.10:8080"\n')
    assert qbt_client(config_mod.load_config(path, bootstrap=False))["name"] == "seedbox"
    monkeypatch.setenv("ANIRSS_QBT_URL", "http://127.0.0.1:47321")
    clients = qbt_clients(config_mod.load_config(path, bootstrap=False))
    assert [(c["name"], c["url"]) for c in clients] == [("env-127.0.0.1_47321", "http://127.0.0.1:47321")]


def test_a_server_only_the_environment_names_keeps_its_own_session(tmp_path, monkeypatch, isolate_state):
    # qbt.sid and qbt.pass belong to the server the config names; an app that
    # points anirss elsewhere must not send that server's cookie or password.
    path = tmp_path / "config.toml"
    path.write_text('[qbittorrent]\nurl = "http://192.0.2.10:8080"\n')
    monkeypatch.setenv("ANIRSS_QBT_URL", "http://me:pw@localhost:8080/")
    local = qbt_client(config_mod.load_config(path, bootstrap=False))
    assert local["name"] == "env-localhost_8080"
    assert session_mod._client_file(session_mod.SID_PATH, local["name"]) == isolate_state / "qbt-env-localhost_8080.sid"
    # The same server the config names keeps the files it always had
    monkeypatch.setenv("ANIRSS_QBT_URL", "http://192.0.2.10:8080/")
    assert qbt_client(config_mod.load_config(path, bootstrap=False))["name"] == DEFAULT_CLIENT


def test_each_environment_address_gets_a_name_of_its_own():
    # The host alone would give these three one session file between them.
    urls = ["https://localhost:8080", "http://localhost:8080/a", "http://localhost:8080/b"]
    assert len({config_mod._env_client_name(u) for u in urls}) == 3
    assert config_mod._env_client_name("http://localhost:8080/") == "env-localhost_8080"


def test_the_default_config_still_parses_with_the_client_example():
    assert "client" not in config_mod.DEFAULT_CONFIG["qbittorrent"]
    assert "[[qbittorrent.client]]" in config_mod.DEFAULT_CONFIG_TOML


# ---- session files ------------------------------------------------------

def test_the_default_client_keeps_the_old_files_and_another_gets_its_own(isolate_state):
    assert session_mod._client_file(session_mod.SID_PATH, "") == isolate_state / "qbt.sid"
    assert session_mod._client_file(session_mod.SID_PATH, DEFAULT_CLIENT) == isolate_state / "qbt.sid"
    assert session_mod._client_file(session_mod.PASS_PATH, "seedbox") == isolate_state / "qbt-seedbox.pass"


def test_saved_passwords_and_sessions_are_kept_per_client(isolate_state):
    assert session_mod._save_password("old-one")
    assert session_mod._save_password("box-pw", "seedbox")
    assert session_mod._load_password() == "old-one"
    assert session_mod._load_password("seedbox") == "box-pw"
    assert session_mod._load_password("local") is None
    (isolate_state / "qbt-local.sid").write_text("QBT_SID_8080\tabc")
    assert session_mod._load_sid("local") == ("QBT_SID_8080", "abc")
    assert session_mod._load_sid() is None
    session_mod._drop_password("seedbox")
    assert session_mod._load_password("seedbox") is None
    assert session_mod._load_password() == "old-one"


def test_an_interactive_login_names_the_client_it_asks_for(monkeypatch):
    prompts = []
    monkeypatch.setattr(session_mod, "_ensure_qbt_reachable", lambda *a, **k: None)
    monkeypatch.setattr(session_mod, "qbt_login",
                        lambda base, user, pw, **kw: (session_mod.QbtSession(object(), base), None))
    monkeypatch.setattr(session_mod.getpass, "getpass",
                        lambda prompt="": prompts.append(prompt) or "pw")
    cfg = qbt_client(config_mod.build_config({**TWO, "qbittorrent": {
        **TWO["qbittorrent"], "save_password": False}}), "local")
    session_mod.login_with_retry(cfg)
    assert prompts == ["qBittorrent password for local (1/3): "]


# ---- CLI ----------------------------------------------------------------

@pytest.mark.parametrize("argv, rest, client", [
    ([], [], None),
    (["-c", "local", "-Q"], ["-Q"], "local"),
    (["-Q", "--client", "seedbox"], ["-Q"], "seedbox"),
    (["--client=local", "Frieren", "1080p"], ["Frieren", "1080p"], "local"),
    (["-e", "nyaa", "Frieren"], ["-e", "nyaa", "Frieren"], None),
    # A flag's value and anything after `--` are never read as -c
    (["--name", "-c", "-S", "magnet:x"], ["--name", "-c", "-S", "magnet:x"], None),
    (["-c", "local", "--", "-c", "x"], ["--", "-c", "x"], "local"),
])
def test_the_client_flag_comes_off_wherever_it_sits(argv, rest, client):
    assert take_client_flag(argv) == (rest, client)


def test_a_run_uses_one_client_and_the_server_keeps_them_all():
    from anirss_lib.main import pin_client

    def names(cfg):
        return [c["name"] for c in qbt_clients(cfg)]

    cfg = config_mod.build_config(TWO)
    pin_client(cfg, ["-Q"], None)
    assert names(cfg) == ["seedbox"]
    cfg = config_mod.build_config(TWO)
    pin_client(cfg, ["-Q"], "local")
    assert names(cfg) == ["local"] and cfg["qbittorrent"]["url"] == LOCAL
    cfg = config_mod.build_config(TWO)
    pin_client(cfg, ["--serve", "--port", "0"], None)
    assert names(cfg) == ["seedbox", "local"]
    cfg = config_mod.build_config(TWO)
    pin_client(cfg, ["--serve"], "local")
    assert names(cfg) == ["local"]


def test_a_run_pinned_to_another_client_drops_the_environment_password(monkeypatch):
    # ANIRSS_QBT_PASSWORD is the default client's; the CLI reads it straight
    # from the environment, so a run on another client must not see it.
    from anirss_lib.main import pin_client

    monkeypatch.setenv("ANIRSS_QBT_PASSWORD", "seedbox-pw")
    pin_client(config_mod.build_config(TWO), ["-Q"], "seedbox")
    assert os.environ.get("ANIRSS_QBT_PASSWORD") == "seedbox-pw"
    pin_client(config_mod.build_config(TWO), ["-Q"], "local")
    assert "ANIRSS_QBT_PASSWORD" not in os.environ
    monkeypatch.setenv("ANIRSS_QBT_PASSWORD", "seedbox-pw")
    pin_client(config_mod.build_config(TWO), ["--serve"], "local")
    assert "ANIRSS_QBT_PASSWORD" not in os.environ


def test_the_client_flag_needs_a_name():
    with pytest.raises(SystemExit):
        take_client_flag(["-Q", "-c"])


# ---- library ------------------------------------------------------------

class FakeQbt:
    """Records which calls reached it; answers like an empty qBittorrent."""

    def __init__(self):
        self.gets, self.posts = [], []

    def get_json(self, endpoint, **params):
        self.gets.append(endpoint)
        return {}

    def post(self, endpoint, **fields):
        self.posts.append((endpoint, fields))
        return "Ok."


class Logins:
    """login_with_password stand-in: one FakeQbt per client url, and a log of
    which client logged in with which password."""

    def __init__(self, fail=()):
        self.qbts = {SEEDBOX: FakeQbt(), LOCAL: FakeQbt()}
        self.calls = []
        self.fail = set(fail)

    def __call__(self, cfg, password, **kw):
        self.calls.append((cfg["name"], password))
        if cfg["name"] in self.fail:
            raise QbtAuthError("rejected", rejected=True)
        return self.qbts[cfg["url"]]


@pytest.fixture
def two(monkeypatch, tmp_path):
    def _make(**kw):
        logins = Logins(**kw)
        monkeypatch.setattr("anirss_lib.api.login_with_password", logins)
        # qbt_status probes; no test may reach the real seedbox address.
        monkeypatch.setattr("anirss_lib.api._qbt_reachable", lambda *a, **kw: True)
        ani = Anirss({**TWO, "downloads": {"save_base": str(tmp_path / "anime")}})
        return ani, logins
    return _make


def test_a_call_reaches_the_client_it_names(two):
    ani, logins = two()
    ani.subscriptions(client="local")
    assert logins.qbts[LOCAL].gets and not logins.qbts[SEEDBOX].gets
    ani.subscriptions()
    assert logins.qbts[SEEDBOX].gets
    with pytest.raises(NotFoundError, match="unknown qBittorrent client 'nas'"):
        ani.subscriptions(client="nas")


def test_each_client_logs_in_once_and_keeps_its_session(two):
    ani, logins = two()
    for _ in range(2):
        ani.subscriptions(client="local")
        ani.subscriptions(client="seedbox")
    assert [name for name, _pw in logins.calls] == ["local", "seedbox"]


def test_a_slow_client_does_not_hold_up_another(two, monkeypatch):
    ani, logins = two()
    entered, release = threading.Event(), threading.Event()
    login = logins.__call__

    def slow_seedbox(cfg, password, **kw):
        if cfg["name"] == "seedbox":
            entered.set()
            release.wait(5)
        return login(cfg, password, **kw)

    monkeypatch.setattr("anirss_lib.api.login_with_password", slow_seedbox)
    waiting = threading.Thread(target=ani.subscriptions, kwargs={"client": "seedbox"})
    waiting.start()
    try:
        assert entered.wait(5)
        done = threading.Thread(target=ani.subscriptions, kwargs={"client": "local"})
        done.start()
        done.join(2)
        assert not done.is_alive() and logins.qbts[LOCAL].gets
    finally:
        release.set()
        waiting.join(5)


def test_the_status_masks_credentials_in_a_url(monkeypatch):
    monkeypatch.setattr("anirss_lib.api._qbt_reachable", lambda *a, **kw: False)
    ani = Anirss({"qbittorrent": {"client": [
        {"name": "box", "url": "http://me:secret@192.0.2.10:8080"}]}})
    assert ani.qbt_status()["url"] == "http://***@192.0.2.10:8080"


def test_a_download_carries_its_clients_tags(two):
    ani, logins = two()
    assert ani.download([MAGNET], name="x", client="local").tags == ["Local"]
    assert ani.download([MAGNET], name="x").tags == ["Anime"]
    assert [e for e, _f in logins.qbts[LOCAL].posts] == ["/api/v2/torrents/add"]


def test_the_environment_password_is_the_default_clients_only(two, monkeypatch):
    monkeypatch.setenv("ANIRSS_QBT_PASSWORD", "seedbox-pw")
    ani, logins = two()
    ani.subscriptions(client="local")
    ani.subscriptions()
    assert logins.calls == [("local", ""), ("seedbox", "seedbox-pw")]
    ani.set_qbt_password("local-pw", client="local")
    ani.subscriptions(client="local")
    assert logins.calls[-1] == ("local", "local-pw")


def test_a_rejected_password_on_one_client_leaves_the_other_alone(two, monkeypatch):
    monkeypatch.setattr(session_mod, "_try_qbt_sid", lambda *a, **k: None)
    ani, _ = two(fail={"seedbox"})
    with pytest.raises(QbtAuthError):
        ani.subscriptions()
    ani.subscriptions(client="local")
    assert ani.qbt_status("seedbox")["login_failed"] is True
    local = ani.qbt_status("local")
    assert (local["client"], local["authenticated"], local["login_failed"]) == ("local", True, False)


# ---- HTTP API -----------------------------------------------------------

@pytest.fixture
def api(two):
    ani, logins = two()
    srv = server.make_server(ani, "127.0.0.1", 0, "tok")
    thread = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01},
                              daemon=True)
    thread.start()

    def call(method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
        try:
            headers = {"Authorization": "Bearer tok"}
            data = None
            if body is not None:
                data = json.dumps(body).encode()
                headers["Content-Type"] = "application/json"
            conn.request(method, server.API + path, body=data, headers=headers)
            resp = conn.getresponse()
            return resp.status, json.loads(resp.read() or b"null")
        finally:
            conn.close()

    yield call, logins
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=2)


def test_the_clients_route_lists_them_in_order(api):
    call, _ = api
    status, body = call("GET", "/clients")
    assert status == 200
    assert body == {"clients": [{"name": "seedbox", "url": SEEDBOX},
                                {"name": "local", "url": LOCAL}]}


def test_the_clients_route_masks_credentials_in_a_url(monkeypatch):
    ani = Anirss({"qbittorrent": {"client": [
        {"name": "box", "url": "http://me:secret@192.0.2.10:8080"}]}})
    from anirss_lib.api import client_info
    assert client_info(ani.client()) == {"name": "box", "url": "http://***@192.0.2.10:8080"}


def test_routes_take_a_client(api):
    call, logins = api
    status, body = call("GET", "/qbt/status?client=local")
    assert status == 200 and body["client"] == "local" and body["url"] == LOCAL
    assert call("GET", "/subscriptions?client=local")[0] == 200
    assert logins.qbts[LOCAL].gets and not logins.qbts[SEEDBOX].gets
    status, body = call("POST", "/downloads", {"links": [MAGNET], "name": "x", "client": "local"})
    assert status == 201 and body["tags"] == ["Local"]
    assert call("GET", "/torrents?client=seedbox")[0] == 200
    assert logins.qbts[SEEDBOX].gets


def test_an_unknown_client_is_not_found(api):
    call, _ = api
    status, body = call("GET", "/subscriptions?client=nas")
    assert status == 404 and body["error"]["type"] == "not_found"
    status, _ = call("POST", "/movie", {"link": MAGNET, "client": 5})
    assert status == 400


# ---- each client's own download roots -----------------------------------

def test_a_client_on_this_machine_has_its_files_here_and_another_does_not():
    seedbox, local = qbt_clients(config_mod.build_config(TWO))
    assert not local_files(seedbox) and local_files(local)
    nas = qbt_client(config_mod.build_config({"qbittorrent": {"client": [
        {"name": "nas", "url": "http://192.0.2.5:8080", "local_files": True}]}}))
    assert local_files(nas)


def test_a_block_sets_its_own_roots():
    cfg = config_mod.build_config({"qbittorrent": {"client": [
        {"name": "seedbox", "url": SEEDBOX, "save_base": "/srv/anime"},
        {"name": "local", "url": LOCAL, "save_base": "~/Anime"}]}})
    seedbox, local = qbt_clients(cfg)
    assert seedbox["save_base"] == "/srv/anime"
    assert local["save_base"] == os.path.expanduser("~/Anime")
    assert "save_base" not in qbt_client(config_mod.build_config(TWO))


@pytest.mark.parametrize("block, message", [
    # Nothing on the seedbox would expand this machine's home
    ({"name": "seedbox", "url": SEEDBOX, "save_base": "~/Anime"}, "not expanded there"),
    ({"name": "seedbox", "url": SEEDBOX, "movie_path": ""}, "must be a path"),
    ({"name": "seedbox", "url": SEEDBOX, "local_files": "yes"}, "true or false"),
])
def test_a_bad_root_is_a_config_error(block, message):
    with pytest.raises(ConfigError, match=message):
        qbt_clients(config_mod.build_config({"qbittorrent": {"client": [block]}}))


class RuleQbt(FakeQbt):
    """A qBittorrent with one subscription, `Frieren`, and its torrent."""

    def __init__(self, save_path):
        super().__init__()
        self.save_path = save_path

    def get_json(self, endpoint, **params):
        self.gets.append(endpoint)
        if endpoint == "/api/v2/rss/rules":
            return {"Frieren": {"affectedFeeds": [], "savePath": self.save_path}}
        if endpoint == "/api/v2/torrents/info":
            return [{"hash": "ab" * 20, "save_path": self.save_path, "size": 5000}]
        return {}


@pytest.fixture
def rooted(monkeypatch, tmp_path):
    """Two clients, the seedbox with roots of its own on its own machine,
    and this machine's [downloads] holding a folder of the same name."""
    local_base = tmp_path / "anime"
    (local_base / "Frieren").mkdir(parents=True)
    (local_base / "Frieren" / "ep01.mkv").write_bytes(b"x" * 10)
    logins = Logins()
    logins.qbts[SEEDBOX] = RuleQbt("/srv/anime/Frieren")
    logins.qbts[LOCAL] = RuleQbt(str(local_base / "Frieren"))
    monkeypatch.setattr("anirss_lib.api.login_with_password", logins)
    ani = Anirss({"downloads": {"save_base": str(local_base)}, "qbittorrent": {"client": [
        {"name": "seedbox", "url": SEEDBOX, "save_base": "/srv/anime"},
        {"name": "local", "url": LOCAL}]}})
    return ani, logins, local_base


def test_a_download_lands_under_its_clients_own_root(rooted):
    ani, _logins, local_base = rooted
    assert ani.download([MAGNET], name="Frieren").save_path == "/srv/anime/Frieren"
    assert ani.download([MAGNET], name="Frieren", client="local").save_path == \
        str(local_base / "Frieren")


def test_another_machines_files_are_never_deleted_here(rooted):
    ani, logins, local_base = rooted
    # Without its torrents only qBittorrent could reach them: refused, and
    # this machine's folder of the same name is left alone.
    with pytest.raises(ValidationError, match="not on this machine"):
        ani.remove(["Frieren"], drop_files=True)
    assert (local_base / "Frieren" / "ep01.mkv").exists()
    # With them, qBittorrent deletes the files on its own disk; the size is
    # the torrents', and the folder here is still untouched.
    plan, = ani.remove(["Frieren"], drop_torrents=True, drop_files=True)
    assert plan.size_bytes == 5000
    delete = [f for e, f in logins.qbts[SEEDBOX].posts if e == "/api/v2/torrents/delete"]
    assert delete == [{"hashes": "ab" * 20, "deleteFiles": "true"}]
    assert (local_base / "Frieren" / "ep01.mkv").exists()


def test_files_on_this_machine_are_still_deleted_here(rooted):
    ani, _logins, local_base = rooted
    plan, = ani.remove(["Frieren"], drop_files=True, client="local")
    assert plan.size_bytes == 10
    assert not (local_base / "Frieren").exists()


def test_the_cli_takes_its_clients_root_before_an_endpoints():
    from anirss_lib.cli.args import ParsedArgs
    from anirss_lib.endpoints import Endpoint
    from anirss_lib.main import _save_base_for, pin_client

    cfg = config_mod.build_config({"qbittorrent": {"client": [
        {"name": "seedbox", "url": SEEDBOX, "save_base": "/srv/anime"},
        {"name": "local", "url": LOCAL}]}})
    ep = Endpoint(name="nyaa", kind="nyaa", url="https://nyaa.si", save_base="/ep/anime")
    pin_client(cfg, ["-Q"], "seedbox")
    assert _save_base_for(ParsedArgs(), cfg, ep) == "/srv/anime"
    cfg = config_mod.build_config({"qbittorrent": {"client": [
        {"name": "seedbox", "url": SEEDBOX, "save_base": "/srv/anime"},
        {"name": "local", "url": LOCAL}]}})
    pin_client(cfg, ["-Q"], "local")
    assert _save_base_for(ParsedArgs(), cfg, ep) == "/ep/anime"
