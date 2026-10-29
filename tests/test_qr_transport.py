"""QR lifecycle regression tests use synthetic sessions, never account secrets."""
import json
import threading
from unittest.mock import Mock
import requests
from test_local_auth import local_http
import common
import server
import store
import xiaomi_cloud as xm
REAL_CLIENT = xm.Client


def response(data):
    return Mock(text=json.dumps(data))


def test_expired_ticket_is_closed_without_network(monkeypatch):
    session = Mock()
    monkeypatch.setattr(xm, "QR_SESSIONS", {"fake-ticket": {"session": session, "created": 1}})
    monkeypatch.setattr(xm.time, "time", lambda: 1000)
    assert xm.qr_poll("fake-ticket") == (None, "expired")
    session.get.assert_not_called()
    session.close.assert_called_once()


def test_same_ticket_has_one_active_poll_and_one_callback(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    session = Mock()
    jar = requests.cookies.RequestsCookieJar()
    jar.set("serviceToken", "synthetic-token", domain="i.mi.com", path="/")
    jar.set("userId", "synthetic-uid", domain="i.mi.com", path="/")
    session.cookies = jar
    def get(url, **kwargs):
        if url == "fake-ticket":
            entered.set()
            assert release.wait(3)
            return response({"code": 0, "location": "https://i.mi.com/fake-callback"})
        return Mock()
    session.get.side_effect = get
    monkeypatch.setattr(xm, "QR_SESSIONS", {"fake-ticket": {"session": session}})
    results = []
    worker = threading.Thread(target=lambda: results.append(xm.qr_poll("fake-ticket")))
    worker.start()
    try:
        assert entered.wait(3)
        assert xm.qr_poll("fake-ticket") == (None, "waiting")
    finally:
        release.set()
        worker.join(3)
    assert results == [(("synthetic-token", "synthetic-uid"), "ok")]
    assert xm.qr_poll("fake-ticket") == (None, "expired")
    assert session.get.call_count == 2
    session.close.assert_called_once()


def test_network_timeout_keeps_ticket_for_retry(monkeypatch):
    session = Mock()
    session.get.side_effect = requests.Timeout("private text")
    monkeypatch.setattr(xm, "QR_SESSIONS", {"fake-ticket": {"session": session}})
    assert xm.qr_poll("fake-ticket") == (None, "waiting")
    assert xm.QR_SESSIONS["fake-ticket"]["busy"] is False
    session.get.side_effect = None
    session.get.return_value = response({})
    assert xm.qr_poll("fake-ticket") == (None, "waiting")


def test_old_request_cannot_replace_new_authorization(local_http):
    b = local_http
    store.set_meta(b.cfg, "xiaomi_token", "new-synthetic-token")
    client = Mock()
    server._persist_xiaomi_session(b.cfg, client, "old-synthetic-token")
    client.current_credentials.assert_not_called()
    assert store.get_meta(b.cfg, "xiaomi_token") == "new-synthetic-token"


def test_rotated_session_survives_new_client(local_http, monkeypatch):
    b = local_http
    real_client = REAL_CLIENT
    monkeypatch.setattr(xm, "Client", real_client)
    store.set_meta(b.cfg, "xiaomi_token", "old-synthetic-token")
    store.set_meta(b.cfg, "xiaomi_uid", "synthetic-uid")
    client = real_client("old-synthetic-token", "synthetic-uid")
    client.session.cookies.set("serviceToken", "rotated-synthetic-token", domain=".i.mi.com", path="/")
    client.session.cookies.set("findExtra", "synthetic-extra", domain="i.mi.com", path="/")
    server._persist_xiaomi_session(b.cfg, client, "old-synthetic-token")
    restored = server._xiaomi_client(b.cfg)
    assert restored.current_credentials() == ("rotated-synthetic-token", "synthetic-uid")
    assert restored.session.cookies.get("findExtra",domain="i.mi.com",path="/") == "synthetic-extra"
    restored.session.close()
    client.session.close()


def test_qr_api_does_not_expose_cookie_snapshot(local_http, monkeypatch):
    b = local_http
    b.setup()
    cookie_rows = [{"name": "findExtra", "value": "synthetic-private-extra"}]
    monkeypatch.setattr(xm, "qr_poll", Mock(return_value=(("synthetic-token", "synthetic-uid", cookie_rows), "ok")))
    accept = Mock(return_value=[])
    monkeypatch.setattr(server, "_accept_xiaomi", accept)
    code, _, text = b.request("/api/xiaomi/qr/poll", "POST", {"ticket":"fake-ticket"})
    assert code == 200 and json.loads(text)["ok"] is True
    assert "synthetic-token" not in text and "synthetic-private-extra" not in text
    accept.assert_called_once_with(b.cfg,"synthetic-token","synthetic-uid",cookies=cookie_rows)


def test_deleted_token_not_resurrected_by_next_request(local_http, monkeypatch):
    b = local_http
    monkeypatch.setattr(xm, "Client", REAL_CLIENT)
    store.set_meta(b.cfg, "xiaomi_token", "old-synthetic-token")
    store.set_meta(b.cfg, "xiaomi_uid", "synthetic-uid")
    client = REAL_CLIENT("old-synthetic-token", "synthetic-uid")
    client.session.cookies.clear(".i.mi.com", "/", "serviceToken")
    server._persist_xiaomi_session(b.cfg, client, "old-synthetic-token")
    restored = server._xiaomi_client(b.cfg)
    assert not any(c.name == "serviceToken" for c in restored.session.cookies)
    client.session.close()
    restored.session.close()
