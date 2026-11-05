"""Getting started: setup's config writes and marker, the doctor's checks,
telling qBittorrent from another web server, and the fzf guard."""

import tomllib
import types

import pytest

from anirss_lib import config as config_mod
from anirss_lib import config_edit, doctor
from anirss_lib.cli import setup
from anirss_lib.errors import QbtAuthError

# ---- config_edit -----------------------------------------------------------

def test_set_values_rewrites_keys_in_place_and_keeps_comments(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(config_mod.DEFAULT_CONFIG_TOML)
    config_edit.set_values(path, "qbittorrent", {"url": "http://10.0.0.2:8080",
                                                  "username": "me"})
    text = path.read_text()
    cfg = tomllib.loads(text)
    assert cfg["qbittorrent"]["url"] == "http://10.0.0.2:8080"
    assert cfg["qbittorrent"]["username"] == "me"
    assert cfg["qbittorrent"]["tags"] == ["Anime"]
    assert "# password attempts before giving up" in text
    assert text.count("url = ") == config_mod.DEFAULT_CONFIG_TOML.count("url = ")
    assert (tmp_path / "config.toml.bak").exists()


def test_set_values_adds_a_missing_key_and_a_missing_table(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[downloads]\nmovie_path = "/m"\n\n[logging]\nlog_path = "/l"\n')
    config_edit.set_values(path, "downloads", {"save_base": "/shows"})
    config_edit.set_values(path, "qbittorrent", {"url": "http://h:1"})
    cfg = tomllib.loads(path.read_text())
    assert cfg["downloads"] == {"movie_path": "/m", "save_base": "/shows"}
    assert cfg["qbittorrent"] == {"url": "http://h:1"}
    assert cfg["logging"] == {"log_path": "/l"}


def test_set_values_quotes_what_it_writes(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[downloads]\n")
    config_edit.set_values(path, "downloads", {"save_base": 'C:\\Anime "new"'})
    assert tomllib.loads(path.read_text())["downloads"]["save_base"] == 'C:\\Anime "new"'


# ---- the onboarding marker ---------------------------------------------------

def test_first_run_needs_onboarding_and_a_working_setup_never_does(tmp_path, monkeypatch):
    monkeypatch.setattr(setup, "ONBOARDED", tmp_path / "onboarded")
    assert setup.needs_onboarding({"endpoint": []})
    assert not setup.needs_onboarding({"endpoint": [{"name": "x"}]})
    assert (tmp_path / "onboarded").exists()       # an upgrade marks it quietly
    assert not setup.needs_onboarding({"endpoint": []})


# ---- qBittorrent or something else ---------------------------------------------

class _Resp:
    def __init__(self, body):
        self.body = body

    def read(self, n=-1):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.mark.parametrize("answer,expected", [
    (b"v5.3.0", "qbt"), (b"v4.6.7", "qbt"), (403, "qbt"), (401, "qbt"),
    (404, "other"), (b"<html>hello</html>", "other"), (OSError("refused"), None),
])
def test_qbt_answer_tells_qbittorrent_from_another_server(monkeypatch, answer, expected):
    import urllib.error

    from anirss_lib.qbt import session

    def fake(req, timeout):
        if isinstance(answer, int):
            raise urllib.error.HTTPError(req.full_url, answer, "x", {}, None)
        if isinstance(answer, Exception):
            raise urllib.error.URLError(answer)
        return _Resp(answer)
    monkeypatch.setattr(session.urllib.request, "urlopen", fake)
    assert session.qbt_answer("http://localhost:8080") == expected


# ---- doctor ------------------------------------------------------------------

class FakeAni:
    def __init__(self, tmp_path, *, prefs=None, auth_error=None, endpoints=()):
        self.config = {"downloads": {"save_base": str(tmp_path / "shows"),
                                     "movie_path": str(tmp_path / "movies"),
                                     "hidden_base": str(tmp_path / "hid")}}
        self._client = {"name": "default", "url": "http://localhost:8080",
                        "username": "admin"}
        self.prefs = prefs if prefs is not None else {
            "rss_processing_enabled": True, "rss_auto_downloading_enabled": True}
        self.auth_error = auth_error
        self._endpoints = list(endpoints)
        self.repos_file = tmp_path / "repos.json"

    def clients(self):
        return [self._client]

    def endpoints(self):
        return self._endpoints

    def repos(self, refresh=False):
        return []

    def repo_endpoints(self):
        return []

    def _with_qbt(self, fn, client=None):
        if self.auth_error:
            raise self.auth_error
        q = types.SimpleNamespace(get=lambda path: "v5.3.0",
                                  get_json=lambda path: self.prefs)
        return fn(q)


def _by_id(checks):
    return {c.id.split(":")[0]: c for c in checks}


def test_doctor_on_a_good_setup_is_all_ok(tmp_path, monkeypatch):
    from anirss_lib import endpoints as endpoints_mod
    from anirss_lib.endpoints import Endpoint
    monkeypatch.setattr("anirss_lib.qbt.session.qbt_answer", lambda url, **kw: "qbt")
    monkeypatch.setattr(endpoints_mod, "fetch_items", lambda ep, q, boot=True: [1, 2, 3])
    ani = FakeAni(tmp_path, endpoints=[Endpoint("site", "rss", "https://s.example/?q={query}")])
    checks = doctor.run(ani, fzf=False)
    assert {c.status for c in checks} == {"ok"}
    assert "3 results" in _by_id(checks)["endpoint"].detail


def test_doctor_names_each_problem_with_a_fix(tmp_path, monkeypatch):
    monkeypatch.setattr("anirss_lib.qbt.session.qbt_answer", lambda url, **kw: "qbt")
    ani = FakeAni(tmp_path, prefs={"rss_processing_enabled": False,
                                   "rss_auto_downloading_enabled": False})
    checks = _by_id(doctor.run(ani, fzf=False))
    assert checks["qbt_rss"].status == "warn" and "Enable fetching RSS feeds" in checks["qbt_rss"].fix
    assert checks["endpoints"].status == "fail" and "--add-repo" in checks["endpoints"].fix

    ani = FakeAni(tmp_path, auth_error=QbtAuthError("refused", rejected=True))
    checks = _by_id(doctor.run(ani, fzf=False))
    assert checks["qbt_login"].status == "fail" and "refused" in checks["qbt_login"].fix


def test_doctor_says_when_the_address_is_not_qbittorrent(tmp_path, monkeypatch):
    monkeypatch.setattr("anirss_lib.qbt.session.qbt_answer", lambda url, **kw: "other")
    checks = _by_id(doctor.run(FakeAni(tmp_path), fzf=False))
    assert checks["qbt"].status == "fail" and "not qBittorrent" in checks["qbt"].detail
    assert "qbt_login" not in checks


def test_doctor_reports_a_missing_fzf_with_the_install_command(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    (c,) = doctor.check_fzf()
    assert c.status == "warn" and c.fix


# ---- the fzf guard -----------------------------------------------------------

def test_interactive_search_without_fzf_stops_and_says_how_to_install(monkeypatch, capsys):
    from anirss_lib import main as main_mod
    from anirss_lib.cli.args import ParsedArgs
    from anirss_lib.endpoints import Endpoint, EndpointState
    monkeypatch.setattr(main_mod.shutil, "which", lambda name: None)
    monkeypatch.setattr(main_mod, "install_hint", lambda: "brew install fzf")
    eps = EndpointState([Endpoint("site", "rss", "https://s.example/?q={query}")])
    with pytest.raises(SystemExit):
        main_mod._run_interactive("Frieren", None, ParsedArgs(), {}, eps)
    err = capsys.readouterr().err
    assert "needs fzf" in err and "brew install fzf" in err and "--subscribe" in err


def test_install_hint_names_the_platforms_package_manager(monkeypatch):
    from anirss_lib import compat
    monkeypatch.setattr(compat.sys, "platform", "darwin")
    assert compat.install_hint("fzf") == "brew install fzf"
    monkeypatch.setattr(compat, "IS_WINDOWS", True)
    monkeypatch.setattr(compat.sys, "platform", "win32")
    assert compat.install_hint("fzf") == "winget install fzf"
