"""Regression tests for the backend hardening pass (review findings)."""

import pytest

from anirss_lib import api
from anirss_lib.api import Anirss, redact_url
from anirss_lib.errors import QbtAuthError, QbtError, ValidationError
from anirss_lib.qbt import session as session_mod
from anirss_lib.qbt.session import _build_multipart_body


def test_multipart_filename_cannot_inject_fields():
    body = _build_multipart_body("B", {"savepath": "/ok"}, "torrents",
                                 'x"\r\n--B\r\nContent-Disposition: form-data; name="savepath"',
                                 b"d4:infod1:xi1eee", "application/x-bittorrent")
    assert body.count(b'name="savepath"') == 1


def test_upload_rejects_hostile_file_name():
    ani = Anirss({})
    with pytest.raises(ValidationError):
        ani.upload_torrent(b"x", file_name='a"\r\nb.torrent')


def test_redact_url_strips_userinfo_and_extra_keys():
    out = redact_url("https://user:hunter2@feeds.example:8443/rss?q={query}&password=p&rsskey=k")
    assert "hunter2" not in out and "user" not in out
    assert out.startswith("https://***@feeds.example:8443/rss?")
    assert "password=***" in out and "rsskey=***" in out and "q={query}" in out


def test_unreachable_login_is_qbt_error_not_latched(monkeypatch):
    monkeypatch.setattr(session_mod, "_ensure_qbt_reachable", lambda *a, **k: None)
    monkeypatch.setattr(session_mod, "_try_qbt_sid", lambda *a, **k: None)
    monkeypatch.setattr(session_mod, "qbt_login",
                        lambda *a, **k: (None, "can't reach qBittorrent at x: boom"))
    ani = Anirss({}, qbt_password="pw")
    with pytest.raises(QbtError) as ei:
        ani.subscriptions()
    assert not isinstance(ei.value, QbtAuthError)
    assert ani._qbt_state["default"].failed_at is None


def test_rejected_login_retries_after_cooldown(monkeypatch):
    calls = []

    def fake_login(*a, **k):
        calls.append(1)
        raise QbtAuthError("bad", rejected=True)

    monkeypatch.setattr(api, "login_with_password", fake_login)
    monkeypatch.setattr(session_mod, "_try_qbt_sid", lambda *a, **k: None)
    clock = [1000.0]
    monkeypatch.setattr(api.time, "monotonic", lambda: clock[0])
    ani = Anirss({}, qbt_password="pw")
    for _ in range(3):
        with pytest.raises(QbtAuthError):
            ani.subscriptions()
    assert len(calls) == 1          # cool-down: no hammering qB's IP ban
    clock[0] += api.LOGIN_COOLDOWN_S + 1
    with pytest.raises(QbtAuthError):
        ani.subscriptions()
    assert len(calls) == 2


def test_remove_dedupes_names(monkeypatch):
    seen = {}

    def fake_plan(q, names, **k):
        seen["names"] = names
        return []
    monkeypatch.setattr(api.actions, "plan_remove", fake_plan)
    ani = Anirss({})
    monkeypatch.setattr(ani, "_with_qbt", lambda fn, client=None: fn(object()))
    ani.remove(["A", "B", "A"], dry_run=True)
    assert seen["names"] == ["A", "B"]
