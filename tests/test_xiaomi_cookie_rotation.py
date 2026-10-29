"""Offline cookie rotation tests; no server, configuration, or database imports."""
import importlib.util
import json
import time
from email.message import Message
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests
from requests.adapters import BaseAdapter, HTTPAdapter


_spec = importlib.util.spec_from_file_location(
    "xiaomi_cookie_rotation_subject", Path(__file__).resolve().parents[1] / "xiaomi_cloud.py")
cloud = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cloud)


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("External transport forbidden")
    monkeypatch.setattr(HTTPAdapter, "send", blocked)


class OfflineAdapter(BaseAdapter):
    def __init__(self, replies):
        self.replies = iter(replies)
        self.requests = []

    def send(self, request, **kwargs):
        self.requests.append(request)
        status, body, cookies, location = next(self.replies)
        response = requests.Response()
        response.status_code = status
        response.url = request.url
        response.request = request
        response._content = json.dumps(body).encode()
        message = Message()
        for value in cookies:
            message.add_header("Set-Cookie", value)
        response.raw = SimpleNamespace(_original_response=SimpleNamespace(msg=message))
        if location:
            response.headers["Location"] = location
        return response

    def close(self):
        pass


def pair():
    jar = requests.cookies.RequestsCookieJar()
    jar.set("serviceToken", "synthetic-old", domain="i.mi.com", path="/", secure=True)
    jar.set("userId", "synthetic-user", domain="i.mi.com", path="/", secure=True)
    return jar


def ok(cookies=()):
    return (200, {"result": "ok", "data": []}, cookies, None)


def prepared(client, url="https://i.mi.com/find/v3/device/status/list"):
    return client.session.prepare_request(requests.Request("GET", url))


def test_legacy_constructor_uses_scoped_jar():
    client = cloud.Client("synthetic-old", "synthetic-user")
    assert client.current_credentials() == ("synthetic-old", "synthetic-user")
    assert "Cookie" not in client.session.headers
    assert "serviceToken=synthetic-old" in prepared(client).headers["Cookie"]
    for url in ("https://account.xiaomi.com/", "https://other.mi.com/",
                "http://i.mi.com/find/v3/device/status/list"):
        assert "Cookie" not in prepared(client, url).headers


def test_real_requests_cookie_extraction_rotates_and_restores():
    client = cloud.Client("synthetic-old", "synthetic-user")
    adapter = OfflineAdapter([
        ok(["serviceToken=synthetic-new; Domain=i.mi.com; Path=/; Secure; HttpOnly; SameSite=Lax",
            "findAux=synthetic-extra; Domain=i.mi.com; Path=/find; Secure"]),
        ok(),
    ])
    client.session.mount("https://", adapter)
    assert client.device_list() == []
    assert client.device_list() == []
    assert "serviceToken=synthetic-old" in adapter.requests[0].headers["Cookie"]
    assert "serviceToken=synthetic-new" in adapter.requests[1].headers["Cookie"]
    assert "serviceToken=synthetic-old" not in adapter.requests[1].headers["Cookie"]
    assert "findAux=synthetic-extra" in adapter.requests[1].headers["Cookie"]
    assert client.current_credentials() == ("synthetic-new", "synthetic-user")
    snapshot = json.loads(json.dumps(client.cookie_snapshot()))
    restored = cloud.Client("synthetic-stale", "synthetic-stale-user", cookies=snapshot)
    assert restored.current_credentials() == client.current_credentials()
    assert "findAux=synthetic-extra" in prepared(restored).headers["Cookie"]
    token = next(c for c in restored.session.cookies if c.name == "serviceToken")
    assert token.has_nonstandard_attr("HttpOnly")
    assert token.get_nonstandard_attr("SameSite") == "Lax"


def test_cookie_deletion_does_not_resurrect_constructor_token():
    client = cloud.Client("synthetic-old", "synthetic-user")
    adapter = OfflineAdapter([
        ok(["serviceToken=; Domain=i.mi.com; Path=/; Max-Age=0"]), ok()])
    client.session.mount("https://", adapter)
    client.device_list()
    client.device_list()
    assert "serviceToken=" not in adapter.requests[1].headers.get("Cookie", "")
    with pytest.raises(cloud.XiaomiError):
        client.current_credentials()
    restored = cloud.Client("synthetic-stale", "synthetic-user", cookies=client.cookie_snapshot())
    with pytest.raises(cloud.XiaomiError):
        restored.current_credentials()


def test_snapshot_preserves_all_cookie_attributes_and_is_detached():
    cookie = requests.cookies.create_cookie(
        "findAux", "synthetic-extra", domain=".mi.com", path="/find", secure=True,
        expires=int(time.time()) + 3600, discard=False, port="443", version=1,
        comment="synthetic comment", comment_url="https://i.mi.com/", rfc2109=True,
        rest={"HttpOnly": None, "SameSite": "Strict", "Partitioned": True})
    jar = pair()
    jar.set_cookie(cookie)
    rows = cloud.export_find_cookies(jar)
    restored = cloud.restore_find_cookies(json.loads(json.dumps(rows)))
    assert cloud.export_find_cookies(restored) == rows
    rows[-1]["rest"]["SameSite"] = "Lax"
    assert cookie.get_nonstandard_attr("SameSite") == "Strict"
    assert next(c for c in restored if c.name == "findAux").get_nonstandard_attr("SameSite") == "Strict"


def test_host_only_flags_preserved():
    jar = pair()
    cookie = requests.cookies.create_cookie("findAux", "synthetic", domain="i.mi.com")
    cookie.domain_specified = False
    cookie.path_specified = False
    jar.set_cookie(cookie)
    restored = cloud.restore_find_cookies(cloud.export_find_cookies(jar))
    cookie = next(c for c in restored if c.name == "findAux")
    assert not cookie.domain_specified and not cookie.path_specified


@pytest.mark.parametrize("domain,path,extra", [
    ("account.xiaomi.com", "/", {}), ("other.mi.com", "/", {}),
    ("i.mi.com.evil.example", "/", {}), ("com", "/", {}), ("", "/", {}),
    ("i.mi.com", "/account", {}), ("i.mi.com", "/find/v30", {}),
    ("i.mi.com", "/find/v3/device/status/listing", {}),
    ("i.mi.com", "/", {"expires": 1}), ("i.mi.com", "/", {"port": "80"}),
])
def test_export_excludes_unrelated_or_expired_cookies(domain, path, extra):
    jar = pair()
    jar.set("unrelatedAccount", "synthetic-private", domain=domain, path=path, **extra)
    assert all(row["name"] != "unrelatedAccount" for row in cloud.export_find_cookies(jar))


@pytest.mark.parametrize("domain,path", [(".mi.com", "/"), ("i.mi.com", "/find"),
    ("i.mi.com", "/find/v3/device/status"), ("i.mi.com", "/find/v3/device/status/list")])
def test_valid_find_scopes(domain, path):
    jar = requests.cookies.RequestsCookieJar()
    jar.set("findAux", "synthetic", domain=domain, path=path)
    assert len(cloud.restore_find_cookies(cloud.export_find_cookies(jar))) == 1


def test_restore_filters_expired_and_unrelated_rows():
    template = cloud.export_find_cookies(pair())[0]
    rows = []
    for domain, path, expires in [("account.xiaomi.com", "/", None),
            ("other.mi.com", "/", None), ("i.mi.com", "/account", None),
            ("i.mi.com", "/", 1), ("com", "/", None)]:
        rows.append({**template, "domain": domain, "path": path, "expires": expires})
    assert list(cloud.restore_find_cookies(rows)) == []


@pytest.mark.parametrize("rows", [None, {}, "[]", (), [None], [{}], [1]])
def test_restore_rejects_malformed_container(rows):
    with pytest.raises(cloud.XiaomiError, match="^Invalid Find cookie snapshot$"):
        cloud.restore_find_cookies(rows)


@pytest.mark.parametrize("field,value", [
    ("value", "synthetic\r\nX: injected"), ("value", "synthetic;extra=value"),
    ("value", "synthetic\\escape"), ("value", "synthetic,extra"), ("value", ""),
    ("value", None), ("name", "bad=name"), ("name", ""),
    ("domain", "i.mi.com\r\n"), ("domain", "..mi.com"),
    ("path", "find"), ("path", "/find;extra"), ("secure", 1),
    ("expires", True), ("expires", 1.5), ("version", True),
    ("rest", []), ("rest", {"HttpOnly": "synthetic\nprivate"}),
    ("rest", {"bad;key": None}), ("rest", {"unknown": object()}),
    ("comment", "synthetic\nprivate"), ("port", "garbage"),
    ("domain_initial_dot", True),
])
def test_restore_rejects_unsafe_or_malformed_fields(field, value):
    row = cloud.export_find_cookies(pair())[0]
    row[field] = value
    with pytest.raises(cloud.XiaomiError, match="^Invalid Find cookie snapshot$"):
        cloud.restore_find_cookies([row])


def test_restore_rejects_unknown_fields_and_duplicates():
    row = cloud.export_find_cookies(pair())[0]
    for rows in ([{**row, "unknown": "synthetic"}], [row, row]):
        with pytest.raises(cloud.XiaomiError):
            cloud.restore_find_cookies(rows)


def test_explicit_empty_snapshot_never_seeds_stale_credentials():
    client = cloud.Client("synthetic-stale", "synthetic-stale-user", cookies=[])
    assert client.cookie_snapshot() == []
    assert "Cookie" not in prepared(client).headers
    with pytest.raises(cloud.XiaomiError):
        client.current_credentials()


def test_constructor_accepts_jar_without_aliasing():
    jar = pair()
    client = cloud.Client("synthetic-stale", "synthetic-stale-user", cookies=jar)
    jar.clear()
    assert client.current_credentials() == ("synthetic-old", "synthetic-user")


@pytest.mark.parametrize("method", ["device_list", "device_status"])
def test_find_requests_never_follow_redirects(method):
    client = cloud.Client("synthetic-old", "synthetic-user")
    adapter = OfflineAdapter([(302, {}, [], "https://account.xiaomi.com/synthetic-login")])
    client.session.mount("https://", adapter)
    if method == "device_list":
        with pytest.raises(cloud.DeviceListError):
            client.device_list()
    else:
        assert client.device_status({"userId": "synthetic-user", "fid": "synthetic-device"}) is None
    assert len(adapter.requests) == 1


def test_device_status_receives_cookie_rotation():
    client = cloud.Client("synthetic-old", "synthetic-user")
    adapter = OfflineAdapter([ok(["serviceToken=synthetic-new; Domain=i.mi.com; Path=/; Secure"])])
    client.session.mount("https://", adapter)
    assert client.device_status({"userId": "synthetic-user", "fid": "synthetic-device"}) == {}
    assert client.current_credentials() == ("synthetic-new", "synthetic-user")


def test_snapshot_credentials_override_stale_arguments_after_rotation():
    snapshot = cloud.export_find_cookies(pair())
    # A restored browser/QR jar uses the canonical Domain attribute representation.
    for row in snapshot:
        row["domain"] = ".i.mi.com"
        row["domain_initial_dot"] = True
    client = cloud.Client("synthetic-stale-argument", "synthetic-stale-user", cookies=snapshot)
    assert client.current_credentials() == ("synthetic-old", "synthetic-user")
    adapter = OfflineAdapter([ok([
        "serviceToken=synthetic-new; Domain=i.mi.com; Path=/; Secure"]), ok()])
    client.session.mount("https://", adapter)
    client.device_list()
    client.device_list()
    header = adapter.requests[-1].headers["Cookie"]
    assert header.count("serviceToken=") == 1
    assert "serviceToken=synthetic-new" in header
    assert "synthetic-stale-argument" not in header
    assert "synthetic-old" not in header
    assert snapshot[0]["value"] == "synthetic-old"
    assert client.current_credentials() == ("synthetic-new", "synthetic-user")
    restored = cloud.Client("synthetic-stale-argument", "synthetic-stale-user",
                            cookies=client.cookie_snapshot())
    assert restored.current_credentials() == ("synthetic-new", "synthetic-user")


def test_host_only_snapshot_rotates_host_only_cookie():
    jar = pair()
    for cookie in jar:
        cookie.domain_specified = False
    client = cloud.Client("synthetic-stale", "synthetic-user", cookies=cloud.export_find_cookies(jar))
    adapter = OfflineAdapter([ok(["serviceToken=synthetic-new; Path=/; Secure"]), ok()])
    client.session.mount("https://", adapter)
    client.device_list()
    client.device_list()
    header = adapter.requests[-1].headers["Cookie"]
    assert header.count("serviceToken=") == 1
    assert "serviceToken=synthetic-new" in header
    assert client.current_credentials() == ("synthetic-new", "synthetic-user")
