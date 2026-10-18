"""login_with_password against a WebUI that skips the login for this client."""

import http.server
import threading

import pytest

from anirss_lib.qbt import session


def _serve(status):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(status)
            self.end_headers()
            self.wfile.write(b"v5.2.4" if status == 200 else b"Forbidden")

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _cfg(server):
    return {"url": f"http://127.0.0.1:{server.server_address[1]}", "username": "anibeam",
            "save_password": False, "login_retries": 1}


@pytest.fixture(autouse=True)
def no_cached_sid(monkeypatch):
    monkeypatch.setattr(session, "_load_sid", lambda client="": None)


def test_a_webui_that_needs_no_login_is_used_without_a_password():
    server = _serve(200)
    try:
        sess = session.login_with_password(_cfg(server), "", autostart=False,
                                           notify=None, persist=False)
        assert sess.is_alive()
    finally:
        server.shutdown()


def test_a_webui_that_wants_a_login_still_asks_for_a_password():
    server = _serve(403)
    try:
        with pytest.raises(session.QbtAuthError, match="login required"):
            session.login_with_password(_cfg(server), "", autostart=False,
                                        notify=None, persist=False)
    finally:
        server.shutdown()
