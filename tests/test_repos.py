"""Endpoint repos: the signature check, the index, trust and refresh, and
installing endpoints into config.toml. Every repo here is built and signed by
the test itself against example hosts; nothing touches the network."""

import base64
import json
import os
import stat
import threading

import pytest

from anirss_lib import config as config_mod
from anirss_lib import ed25519, repos, server
from anirss_lib.api import Anirss
from anirss_lib.errors import (
    FetchError,
    NotFoundError,
    RepoError,
    ValidationError,
)

SEED = bytes(range(32))
OTHER_SEED = bytes(range(1, 33))


def key_text(seed: bytes = SEED) -> str:
    return "ed25519:" + base64.b64encode(ed25519.public_key(seed)).decode()


def repo_file(endpoints, *, seed=SEED, serial=1, name="Test repo", nsfw=False,
              key=None) -> bytes:
    index = {"name": name, "key": key or key_text(seed), "serial": serial,
             "nsfw": nsfw, "endpoints": endpoints}
    payload = json.dumps(index).encode()
    return json.dumps({
        "format": repos.FORMAT, "version": 1,
        "payload": base64.b64encode(payload).decode(),
        "signature": base64.b64encode(ed25519.sign(seed, payload)).decode(),
    }).encode()


PLAIN = {"id": "plain", "name": "Plain", "version": 1, "kind": "rss",
         "url": "https://plain.example/rss?q={query}"}
NYAA = {"id": "site", "name": "Site", "version": 1, "kind": "nyaa",
        "url": "https://site.example/", "category": "1_2", "filter": "2"}
TOKENED = {"id": "tokened", "name": "Tokened", "version": 1, "kind": "rss",
           "url": "https://tok.example/feed?s={query}&token={token}",
           "auth": [{"key": "token", "label": "RSS token",
                     "help": "Find it under Account.",
                     "help_url": "https://tok.example/account"}]}
JACKETT = {"id": "own", "name": "Own (Jackett)", "version": 1, "kind": "torznab",
           "url": "http://{host}/api/v2.0/indexers/x/results/torznab/api"
                  "?apikey={apikey}&t=search&q={query}",
           "auth": [{"key": "host", "label": "Jackett address", "secret": False},
                    {"key": "apikey", "label": "API key"}]}
TOSHO = {"id": "jsonish", "name": "Tosho kind", "version": 1, "kind": "tosho",
         "url": "https://tosho.example/feed/json?q={query}"}


# ---- ed25519 ---------------------------------------------------------------

RFC_VECTORS = [
    ("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
     "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a", "",
     ("e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555f"
      "b8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b")),
    ("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
     "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c", "72",
     ("92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da08"
      "5ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00")),
]


@pytest.mark.parametrize("seed,pub,msg,sig", RFC_VECTORS)
def test_rfc8032_vectors_sign_and_verify(seed, pub, msg, sig):
    seed, pub, msg, sig = map(bytes.fromhex, (seed, pub, msg, sig))
    assert ed25519.public_key(seed) == pub
    assert ed25519.sign(seed, msg) == sig
    assert ed25519.verify(pub, msg, sig)
    assert not ed25519.verify(pub, msg + b"!", sig)


def test_a_signature_with_s_past_the_group_order_or_a_small_order_key_fails():
    _seed, pub, msg, sig = map(bytes.fromhex, RFC_VECTORS[0])
    s = int.from_bytes(sig[32:], "little") + ed25519._L
    assert not ed25519.verify(pub, msg, sig[:32] + s.to_bytes(32, "little"))
    identity = (1).to_bytes(32, "little")          # y = 1: the neutral point
    assert not ed25519.verify(identity, msg, sig)
    assert not ed25519.verify(pub[:31], msg, sig)


# ---- the file --------------------------------------------------------------

def test_a_signed_repo_reads_with_its_definitions():
    idx = repos.parse(repo_file([PLAIN, NYAA, TOKENED]))
    assert idx.name == "Test repo" and idx.serial == 1
    assert [d.id for d in idx.endpoints] == ["plain", "site", "tokened"]
    assert idx.endpoints[2].auth[0].label == "RSS token"
    assert idx.skipped == []


def test_the_fingerprint_is_eight_groups_of_the_key_hash():
    fp = repos.fingerprint(key_text())
    groups = fp.split(" ")
    assert len(groups) == 8 and all(len(g) == 4 and g == g.upper() for g in groups)
    assert fp != repos.fingerprint(key_text(OTHER_SEED))


def test_a_changed_payload_fails_the_signature():
    env = json.loads(repo_file([PLAIN]))
    payload = json.loads(base64.b64decode(env["payload"]))
    payload["endpoints"][0]["url"] = "https://evil.example/?q={query}"
    env["payload"] = base64.b64encode(json.dumps(payload).encode()).decode()
    with pytest.raises(RepoError) as e:
        repos.parse(json.dumps(env).encode())
    assert e.value.code == "signature"


def test_an_index_signed_by_another_key_than_it_names_fails():
    data = repo_file([PLAIN], seed=OTHER_SEED, key=key_text(SEED))
    with pytest.raises(RepoError) as e:
        repos.parse(data)
    assert e.value.code == "signature"


@pytest.mark.parametrize("data,code", [
    (b"<html>not json</html>", "format"),
    (json.dumps({"format": "something-else", "version": 1}).encode(), "format"),
    (json.dumps({"format": repos.FORMAT, "version": 2}).encode(), "version"),
    (b" " * (repos.MAX_BYTES + 1), "format"),
])
def test_something_else_is_not_a_repo(data, code):
    with pytest.raises(RepoError) as e:
        repos.parse(data)
    assert e.value.code == code


def test_a_bad_definition_is_left_out_and_the_rest_stays():
    bad = [
        {**PLAIN, "id": "http-site", "url": "http://plain.example/?q={query}"},
        {**PLAIN, "id": "stray", "url": "https://x.example/?q={query}&k={key}"},
        {**NYAA, "id": "nyaa-query", "url": "https://n.example/?q={query}"},
        {**PLAIN, "id": "no-query", "url": "https://x.example/rss"},
        {**PLAIN, "id": "Upper"},
        {**PLAIN, "id": "weird", "kind": "gopher"},
        {**TOKENED, "id": "host-ph", "url": "https://{token}.example/?q={query}"},
        {**PLAIN, "id": "no-version", "version": "1"},
        {**PLAIN},                       # second "plain"
    ]
    idx = repos.parse(repo_file([PLAIN, *bad]))
    assert [d.id for d in idx.endpoints] == ["plain"]
    assert len(idx.skipped) == len(bad)


def test_a_torznab_definition_may_name_the_users_own_host_over_http():
    idx = repos.parse(repo_file([JACKETT]))
    assert [d.id for d in idx.endpoints] == ["own"]
    secret_host = {**JACKETT, "auth": [{"key": "host", "label": "h", "secret": True},
                                       {"key": "apikey", "label": "k"}]}
    assert repos.check(repos.definition_from(secret_host))


def test_kinds_anirss_cannot_search_are_listed_not_installable():
    idx = repos.parse(repo_file([TOSHO]))
    d = idx.endpoints[0]
    assert not d.searchable and "tosho" in repos.not_searchable_reason(d)


# ---- filling and reading back --------------------------------------------

def test_filling_encodes_values_and_keeps_the_query():
    d = repos.definition_from(TOKENED)
    url = repos.fill(d, {"token": "a b&c"})
    assert url == "https://tok.example/feed?s={query}&token=a%20b%26c"
    assert repos.extract(d, url) == {"token": "a b&c"}
    with pytest.raises(ValidationError):
        repos.fill(d, {})


def test_the_host_field_takes_a_host_or_host_and_port_only():
    d = repos.definition_from(JACKETT)
    url = repos.fill(d, {"host": "127.0.0.1:9117", "apikey": "k"})
    assert url.startswith("http://127.0.0.1:9117/api/")
    for bad in ("127.0.0.1:9117/x", "user@host", "a b", "[::1]:1"):
        with pytest.raises(ValidationError):
            repos.fill(d, {"host": bad, "apikey": "k"})


def test_credentials_follow_an_update_only_to_the_same_host_and_fields():
    old = repos.definition_from(TOKENED)
    same = repos.definition_from({**TOKENED, "version": 2, "name": "Renamed"})
    moved = repos.definition_from({**TOKENED, "version": 2,
                                   "url": "https://other.example/?s={query}&token={token}"})
    more = repos.definition_from({**TOKENED, "version": 2,
                                  "url": TOKENED["url"] + "&u={user}",
                                  "auth": [*TOKENED["auth"], {"key": "user", "label": "u"}]})
    assert repos.keeps_credentials(old, same)
    assert not repos.keeps_credentials(old, moved)
    assert not repos.keeps_credentials(old, more)


# ---- trust and refresh -----------------------------------------------------

def test_trust_pins_the_key_and_refuses_another_or_an_older_index():
    state = repos.State()
    repos.accept(state, "https://r.example/i.json", repos.parse(repo_file([PLAIN], serial=5)))
    repo = state.repos[0]
    assert repo.key == key_text() and repo.serial == 5
    with pytest.raises(RepoError) as e:
        repos.accept(state, repo.url, repos.parse(repo_file([PLAIN], seed=OTHER_SEED)))
    assert e.value.code == "key_changed"
    with pytest.raises(RepoError) as e:
        repos.accept(state, repo.url, repos.parse(repo_file([PLAIN], serial=4)))
    assert e.value.code == "stale"
    repos.accept(state, repo.url, repos.parse(repo_file([PLAIN, NYAA], serial=5)))
    assert [d.id for d in state.repos[0].definitions()] == ["plain", "site"]


def test_a_failed_refresh_keeps_the_last_good_index_and_says_why():
    state = repos.State()
    repos.accept(state, "https://r.example/i.json", repos.parse(repo_file([PLAIN])))

    def down(url):
        raise FetchError("repo unreachable: timed out")
    repos.refresh(state, fetcher=down)
    assert state.repos[0].error == "repo unreachable: timed out"
    assert [d.id for d in state.repos[0].definitions()] == ["plain"]
    repos.refresh(state, fetcher=lambda url: repo_file([PLAIN, NYAA], serial=2))
    assert state.repos[0].error is None and state.repos[0].serial == 2


def test_a_local_path_is_a_repo_address_too(tmp_path):
    f = tmp_path / "index.json"
    f.write_bytes(repo_file([PLAIN]))
    assert repos.parse(repos.fetch(str(f))).name == "Test repo"
    assert repos.parse(repos.fetch(f.as_uri())).name == "Test repo"
    with pytest.raises(FetchError):
        repos.fetch(str(tmp_path / "missing.json"))
    with pytest.raises(FetchError):
        repos.fetch("ftp://r.example/index.json")


# ---- the facade: installing into config.toml --------------------------------

@pytest.fixture
def ani(tmp_path, monkeypatch):
    files = {}

    def fake_fetch(url, **kw):
        return files[url]
    monkeypatch.setattr(repos, "fetch", fake_fetch)
    path = tmp_path / "config.toml"
    path.write_text(config_mod.DEFAULT_CONFIG_TOML, encoding="utf-8")
    a = Anirss(config_path=path, repos_file=tmp_path / "repos.json")
    a.files = files
    a.url = "https://r.example/index.json"
    files[a.url] = repo_file([PLAIN, NYAA, TOKENED, JACKETT, TOSHO])
    return a


def trust(a):
    idx = a.preview_repo(a.url)
    return a.trust_repo(a.url, idx.key)


def test_trusting_needs_the_key_that_was_shown(ani):
    idx = ani.preview_repo(ani.url)
    with pytest.raises(RepoError):
        ani.trust_repo(ani.url, key_text(OTHER_SEED))
    assert ani.repos() == []
    repo = ani.trust_repo(ani.url, idx.key)
    assert [r.name for r in ani.repos()] == ["Test repo"] and repo.fingerprint == idx.fingerprint


def test_install_writes_the_filled_endpoint_and_remembers_where_from(ani):
    trust(ani)
    ep = ani.install_endpoint("tokened", {"token": "s3cret"})
    assert ep.name == "tokened" and ep.kind == "rss"
    assert ep.url == "https://tok.example/feed?s={query}&token=s3cret"
    again = Anirss(config_path=ani.config_file, repos_file=ani.repos_file)
    assert [e.name for e in again.endpoints()] == ["tokened"]
    inst = again.installed_endpoints()["tokened"]
    assert (inst.repo, inst.id, inst.version) == (ani.url, "tokened", 1)
    assert "s3cret" not in ani.repos_file.read_text()
    if os.name == "posix":
        assert stat.S_IMODE(ani.config_file.stat().st_mode) == 0o600
    offer = next(o for o in ani.repo_endpoints() if o.definition.id == "tokened")
    assert offer.installed_as == "tokened" and not offer.update


def test_install_maps_kinds_and_keeps_nyaa_settings(ani):
    trust(ani)
    site = ani.install_endpoint("site")
    assert (site.kind, site.url, site.category, site.filter) == \
        ("nyaa", "https://site.example/", "1_2", "2")
    own = ani.install_endpoint("own", {"host": "127.0.0.1:9117", "apikey": "k"})
    assert own.kind == "rss" and own.url.startswith("http://127.0.0.1:9117/")
    with pytest.raises(ValidationError):
        ani.install_endpoint("jsonish")
    with pytest.raises(ValidationError):
        ani.install_endpoint("tokened")          # no token given
    with pytest.raises(NotFoundError):
        ani.install_endpoint("nope")


def test_install_never_takes_over_an_endpoint_the_user_wrote(ani):
    trust(ani)
    ani.add_endpoint({"name": "plain", "kind": "rss",
                      "url": "https://mine.example/?q={query}"})
    with pytest.raises(ValidationError):
        ani.install_endpoint("plain")
    ep = ani.install_endpoint("plain", name="plain-repo")
    assert ep.name == "plain-repo"
    assert ani.endpoint("plain").url == "https://mine.example/?q={query}"


def test_an_update_keeps_credentials_only_when_it_may(ani):
    trust(ani)
    ani.install_endpoint("tokened", {"token": "s3cret"})
    ani.files[ani.url] = repo_file([{**TOKENED, "version": 2,
                                     "url": TOKENED["url"] + "&res=1080"}], serial=2)
    ani.repos(refresh=True)
    assert [o.definition.id for o in ani.repo_endpoints() if o.update] == ["tokened"]
    assert ani.update_needs("tokened") == []
    ep = ani.update_endpoint("tokened")
    assert ep.url == "https://tok.example/feed?s={query}&token=s3cret&res=1080"
    assert ani.installed_endpoints()["tokened"].version == 2

    ani.files[ani.url] = repo_file([{**TOKENED, "version": 3,
                                     "url": "https://moved.example/?s={query}&token={token}"}],
                                   serial=3)
    ani.repos(refresh=True)
    assert [f.key for f in ani.update_needs("tokened")] == ["token"]
    with pytest.raises(ValidationError):
        ani.update_endpoint("tokened")
    ep = ani.update_endpoint("tokened", {"token": "new"})
    assert ep.url == "https://moved.example/?s={query}&token=new"


def test_removing_an_installed_endpoint_forgets_it_and_a_repo_leaves_its_endpoints(ani):
    trust(ani)
    ani.install_endpoint("plain")
    ani.install_endpoint("site")
    ani.remove_endpoint("plain")
    assert list(ani.installed_endpoints()) == ["site"]
    ani.remove_repo("Test repo")
    assert ani.repos() == [] and [e.name for e in ani.endpoints()] == ["site"]
    assert [i.name for i in repos.orphaned(repos.load_state(ani.repos_file))] == ["site"]


# ---- over HTTP -------------------------------------------------------------

def test_the_repo_routes_preview_trust_list_and_install(ani):
    from tests.test_server import TOKEN, Client
    srv = server.make_server(ani, "127.0.0.1", 0, TOKEN)
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01},
                         daemon=True)
    t.start()
    try:
        c = Client(srv.server_address[1])
        status, body, _ = c.post("/repos", {"url": ani.url})
        assert status == 200 and body["repo"]["name"] == "Test repo"
        assert ani.repos() == []
        status, body2, _ = c.post("/repos/trust", {"url": ani.url,
                                                   "key": key_text(OTHER_SEED)})
        assert status == 422 and body2["error"]["type"] == "repo_error"
        assert body2["error"]["code"] == "key_changed"
        status, _, _ = c.post("/repos/trust", {"url": ani.url, "key": body["repo"]["key"]})
        assert status == 201
        status, body, _ = c.get("/repos/endpoints")
        assert {e["id"]: e["searchable"] for e in body["endpoints"]}["jsonish"] is False
        status, body, _ = c.post("/repos/install", {"id": "tokened",
                                                    "credentials": {"token": "s3cret"}})
        assert status == 201 and body["endpoint"]["url"].endswith("token=***")
        status, body, _ = c.get("/repos")
        assert body["repos"][0]["fingerprint"] == repos.fingerprint(key_text())
        assert "payload" not in body["repos"][0]
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=2)


# ---- review follow-ups -----------------------------------------------------

PATHTOKEN = {"id": "pathtok", "name": "Path token", "version": 1, "kind": "rss",
             "url": "https://p.example/{passkey}/rss?q={query}",
             "auth": [{"key": "passkey", "label": "Passkey"}]}
DASHED = {"id": "dashed", "name": "Dashed", "version": 1, "kind": "rss",
          "url": "https://d.example/?q={query}&k={api-key}",
          "auth": [{"key": "api-key", "label": "API key"}]}


def test_a_secret_in_the_path_is_masked_wherever_it_is_shown(ani):
    ani.files[ani.url] = repo_file([PATHTOKEN])
    trust(ani)
    ep = ani.install_endpoint("pathtok", {"passkey": "p4ss"})
    assert "p4ss" in ep.url
    assert ani.endpoint_view(ep)["url"] == "https://p.example/***/rss?q={query}"


def test_a_fetch_logs_its_url_without_the_secret(monkeypatch):
    from anirss_lib import nyaa
    logged = []
    monkeypatch.setattr(nyaa, "log", lambda level, msg: logged.append(msg))

    def refuse(*a, **kw):
        raise OSError("no network in tests")
    monkeypatch.setattr(nyaa.urllib.request, "urlopen", refuse)
    with pytest.raises(FetchError):
        nyaa.fetch_bytes("https://t.example/feed?q=x&apikey=SECRET")
    assert logged and all("SECRET" not in m for m in logged)


def test_values_read_back_for_keys_with_a_dash():
    d = repos.definition_from(DASHED)
    url = repos.fill(d, {"api-key": "k1"})
    assert repos.extract(d, url) == {"api-key": "k1"}


def test_replacing_an_installed_endpoint_by_hand_forgets_where_it_came_from(ani):
    trust(ani)
    ani.install_endpoint("plain")
    ani.add_endpoint({"name": "plain", "kind": "rss", "url": "https://mine.example/?q={query}"})
    assert "plain" not in ani.installed_endpoints()


def test_a_nyaa_address_with_its_own_parameters_keeps_them():
    from anirss_lib import endpoints
    ep = endpoints.Endpoint("n", "nyaa", "https://n.example/?token=abc")
    assert endpoints.search_url(ep, "x").startswith("https://n.example/?token=abc&page=rss&q=x")


def test_search_words_are_never_found_in_the_host():
    from anirss_lib.endpoint_config import put_query
    assert put_query("https://subs.example/rss/?s=sub", "sub") == \
        "https://subs.example/rss/?s={query}"
    with pytest.raises(ValidationError):
        put_query("https://frieren.example/", "frieren")


def test_the_api_never_reads_a_local_repo_file(ani, tmp_path):
    f = tmp_path / "local.json"
    ani.files[str(f)] = repo_file([PLAIN])
    assert ani.preview_repo(str(f)).name == "Test repo"     # the CLI may
    with pytest.raises(ValidationError):
        ani.preview_repo(str(f), allow_local=False)
    with pytest.raises(ValidationError):
        ani.trust_repo(f.as_uri(), key_text(), allow_local=False)


def test_a_file_url_reads_on_this_platform(tmp_path):
    f = tmp_path / "with space.json"
    f.write_bytes(repo_file([PLAIN]))
    assert repos.parse(repos.fetch(f.as_uri())).name == "Test repo"
