"""Isolated HTTP tests: synthetic configuration, temporary SQLite, no workers."""
import copy
import http.client
import json
import re
import secrets
import sys
import threading
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
import common
import local_auth
import server
import store


@pytest.fixture
def local_http(tmp_path, monkeypatch):
    monkeypatch.delenv("LOCATOR_PASSWORD", raising=False)
    monkeypatch.setattr(common, "CONFIG_PATH", str(tmp_path / "config.json"))
    monkeypatch.setattr(common, "lan_ip", lambda: "127.0.0.1")
    monkeypatch.setattr(server, "_public_url", lambda: "")
    for module, name in [(server.xiaomi_cloud, "Client"), (server, "run_auto_reports"),
                         (server, "sync_all"), (server.mailer, "push_schedule")]:
        monkeypatch.setattr(module, name, Mock(side_effect=AssertionError("external work forbidden")))
    cfg = copy.deepcopy(common.DEFAULTS)
    cfg.update(data_dir=str(tmp_path), access_token=secrets.token_hex(16))
    store.init(cfg)
    handler = type("IsolatedHandler", (server.Handler,), {"cfg": cfg})
    httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    httpd.local_auth = local_auth.LocalAuth(cfg, common.save_config)
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()

    class Browser:
        cookie = ""
        csrf = ""
        origin = "http://127.0.0.1:%s" % httpd.server_port

        def request(self, path, method="GET", body=None, headers=None):
            hdr = {"Cookie": self.cookie}
            if method == "POST":
                hdr.update({"Origin": self.origin, "Content-Type": "application/json", "X-CSRF-Token": self.csrf})
            for key, value in (headers or {}).items():
                if value is None:
                    hdr.pop(key, None)
                else:
                    hdr[key] = value
            connection = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=5)
            connection.request(method, path, body=json.dumps(body) if body is not None else None, headers=hdr)
            response = connection.getresponse()
            data = response.read().decode("utf-8")
            result = (response.status, dict(response.getheaders()), data)
            if response.getheader("Set-Cookie"):
                self.cookie = response.getheader("Set-Cookie").split(";", 1)[0]
            connection.close()
            return result

        def page(self):
            code, headers, text = self.request("/login")
            assert code == 200
            self.csrf = re.search(r'name="csrf-token" content="([^"]+)"', text).group(1)
            return headers, text

        def setup(self):
            self.page()
            password = secrets.token_urlsafe(24)
            code, _, text = self.request("/api/auth/setup", "POST", {"password": password})
            assert code == 200, text
            self.csrf = json.loads(text)["csrf"]
            return password

    browser = Browser()
    browser.cfg, browser.httpd = cfg, httpd
    try:
        yield browser
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=3)


def test_anonymous_routes_never_expose_token_or_data(local_http):
    b = local_http
    for path in ["/api/state", "/api/points", "/api/settings", "/api/reports", "/api/report/export", "/api/xiaomi/qr"]:
        code, _, text = b.request(path)
        assert code == 401
        assert b.cfg["access_token"] not in text
    assert b.request("/")[0] == 303
    assert b.request("/api/device/update", "POST", {})[0] == 401
    b.page()  # Pre-login session is not an authenticated session.
    assert b.request("/api/state")[0] == 401


@pytest.mark.parametrize(
    "token, uid, expected",
    [
        (None, None, False),
        ("synthetic-settings-token", None, False),
        (None, "synthetic-settings-uid", False),
        ("synthetic-settings-token", "", False),
        ("", "synthetic-settings-uid", False),
        ("synthetic-settings-token", "synthetic-settings-uid", True),
    ],
    ids=["not-saved", "token-only", "uid-only", "empty-uid", "empty-token", "complete"],
)
def test_settings_xiaomi_credentials_saved(local_http, token, uid, expected):
    b = local_http
    b.setup()
    conn = store.connect(b.cfg)
    try:
        conn.execute("DELETE FROM meta WHERE k IN (?, ?)", ("xiaomi_token", "xiaomi_uid"))
        conn.commit()
    finally:
        conn.close()
    for key, value in [("xiaomi_token", token), ("xiaomi_uid", uid)]:
        if value is not None:
            store.set_meta(b.cfg, key, value)
        assert store.get_meta(b.cfg, key) == value

    code, headers, text = b.request("/api/settings")
    assert code == 200
    payload = json.loads(text)
    assert payload["ok"] is True
    xiaomi = payload["settings"]["xiaomi"]
    assert type(xiaomi["credentials_saved"]) is bool
    assert xiaomi["credentials_saved"] is expected
    assert set(xiaomi) == {"credentials_saved"}
    response = text + json.dumps(headers)
    for secret in ("synthetic-settings-token", "synthetic-settings-uid"):
        assert secret not in response

def test_setup_persists_only_hash_and_protects_state(local_http):
    b = local_http
    password = b.setup()
    stored = Path(common.CONFIG_PATH).read_text(encoding="utf-8")
    assert password not in stored
    assert local_auth.verify_password(password, json.loads(stored)["auth"]["password_hash"])
    assert json.loads(b.request("/api/state")[2])["token"] == b.cfg["access_token"]
    code, _, page = b.request("/")
    assert code == 200 and b.csrf in page and "__CSRF_TOKEN__" not in page
    fresh = local_auth.LocalAuth(json.loads(stored), lambda cfg: None)
    assert fresh.login(password, "127.0.0.1")[1] == 200


@pytest.mark.parametrize("headers", [{"Origin": None}, {"Origin": "null"}, {"Origin": "http://evil.invalid"},
    {"Origin": "https://127.0.0.1"}, {"X-CSRF-Token": None}, {"X-CSRF-Token": "wrong"}, {"Sec-Fetch-Site": "cross-site"}])
def test_setup_rejects_origin_and_csrf(local_http, headers):
    b = local_http
    b.page()
    assert b.request("/api/auth/setup", "POST", {"password": secrets.token_urlsafe(24)}, headers)[0] == 403
    assert not b.httpd.local_auth.configured


def test_remote_initialization_refused(local_http, monkeypatch):
    b = local_http
    b.page()
    monkeypatch.setattr(server.Handler, "_local_setup_ok", lambda self: False)
    assert b.request("/login")[0] == 403
    assert b.request("/api/auth/setup", "POST", {"password": secrets.token_urlsafe(24)})[0] == 403
    assert not b.httpd.local_auth.configured


def test_setup_rejects_non_loopback_host(local_http):
    b = local_http
    assert b.request("/login", headers={"Host": "remote.invalid"})[0] == 403


def test_login_logout_rotation_and_session_flags(local_http):
    b = local_http
    password = b.setup()
    headers, _ = b.page()
    assert "HttpOnly" in headers["Set-Cookie"] and "SameSite=Strict" in headers["Set-Cookie"]
    old_cookie = b.cookie
    new_password = secrets.token_urlsafe(24)
    assert b.request("/api/auth/password", "POST", {"current_password": "wrong", "password": new_password})[0] == 401
    code, _, text = b.request("/api/auth/password", "POST", {"current_password": password, "password": new_password})
    assert code == 200
    b.csrf = json.loads(text)["csrf"]
    assert b.request("/api/state", headers={"Cookie": old_cookie})[0] == 401
    assert b.request("/api/auth/logout", "POST", {})[0] == 200
    assert b.request("/api/state")[0] == 401
    b.page()
    assert b.request("/api/auth/login", "POST", {"password": password})[0] == 401
    code, _, text = b.request("/api/auth/login", "POST", {"password": new_password})
    assert code == 200
    b.csrf = json.loads(text)["csrf"]
    assert b.request("/api/state")[0] == 200


def test_management_writes_require_csrf_and_origin(local_http):
    b = local_http
    b.setup()
    store.upsert_device(b.cfg, "test-device", mode="cloud")
    for headers in [{"Origin": None}, {"X-CSRF-Token": None}, {"Origin": "http://evil.invalid"}]:
        assert b.request("/api/device/update", "POST", {"device_id": "test-device", "enabled": False}, headers)[0] == 403
    assert store.devices(b.cfg)[0]["enabled"] == 1
    assert b.request("/api/device/update", "POST", {"device_id": "test-device", "enabled": False})[0] == 200
    assert store.devices(b.cfg)[0]["enabled"] == 0
    assert b.request("/api/xiaomi/qr/poll")[0] == 405


def test_phone_apis_keep_independent_token_authorization(local_http):
    b = local_http
    assert b.request("/api/phone/ping")[0] == 403
    assert b.request("/api/phone/config", headers={"X-Token": "wrong"})[0] == 403
    code, _, text = b.request("/api/phone/config", headers={"X-Token": b.cfg["access_token"]})
    assert code == 200 and b.cfg["access_token"] not in text
    assert b.request("/api/phone/heartbeat", "POST", {"device_id": "test-phone"}, {"X-Token": b.cfg["access_token"], "Origin": None})[0] == 200
    assert b.request("/api/state", headers={"X-Token": b.cfg["access_token"]})[0] == 401


def test_qr_poll_reads_post_body_only(local_http, monkeypatch):
    b = local_http
    b.setup()
    poll = Mock(return_value=(None, "waiting"))
    monkeypatch.setattr(server.xiaomi_cloud, "qr_poll", poll)
    assert b.request("/api/xiaomi/qr/poll", "POST", {"ticket": "synthetic-ticket"})[0] == 200
    poll.assert_called_once_with("synthetic-ticket", include_cookies=True)


def test_environment_initializes_once_and_failed_save_rolls_back(monkeypatch):
    password = secrets.token_urlsafe(24)
    monkeypatch.setenv("LOCATOR_PASSWORD", password)
    cfg, saved = {}, []
    auth = local_auth.LocalAuth(cfg, lambda cfg: saved.append(copy.deepcopy(cfg)))
    assert saved and local_auth.verify_password(password, cfg["auth"]["password_hash"])
    old = cfg["auth"]["password_hash"]
    monkeypatch.setenv("LOCATOR_PASSWORD", secrets.token_urlsafe(24))
    local_auth.LocalAuth(cfg, lambda cfg: pytest.fail("must not overwrite"))
    assert cfg["auth"]["password_hash"] == old
    auth.save = Mock(side_effect=OSError("synthetic write failure"))
    with pytest.raises(OSError):
        auth.set_password(secrets.token_urlsafe(24))
    assert cfg["auth"]["password_hash"] == old


def test_password_validation_and_login_throttle(monkeypatch):
    monkeypatch.delenv("LOCATOR_PASSWORD", raising=False)
    with pytest.raises(ValueError):
        local_auth.hash_password("short")
    assert not local_auth.verify_password("abc", "bad-format")
    password = secrets.token_urlsafe(24)
    encoded = local_auth.hash_password(password)
    assert local_auth.verify_password(password, encoded)
    assert not local_auth.verify_password("wrong", encoded)
    assert encoded != local_auth.hash_password(password)
    auth = local_auth.LocalAuth({"auth": {"password_hash": encoded}}, lambda cfg: None)
    for _ in range(5):
        assert auth.login("wrong", "synthetic-address")[1] == 401
    assert auth.login(password, "synthetic-address")[1] == 429
