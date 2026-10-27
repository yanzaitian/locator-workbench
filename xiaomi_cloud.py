"""小米云「查找设备」v3 接口：账号登录 + 设备列表 + 定位查询。

协议参考社区实现（HA xiaomi-cloud）。仅供本人账号使用，风险自担。
"""
import json
import time

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36")
FIND_HEADERS = {"Accept-Language": "zh-CN", "Referer": "https://i.mi.com/find"}


class XiaomiError(Exception):
    pass


_DEVICE_LIST_CATEGORIES = frozenset({
    "http_non_200", "non_json", "api_rejected", "invalid_structure",
    "network_error", "empty_list", "unexpected_error",
})


class DeviceListError(XiaomiError):
    """Device-list diagnostics contain only fixed categories and HTTP integers."""

    def __init__(self, category, http_status=None):
        self.category = (category if type(category) is str and
                         category in _DEVICE_LIST_CATEGORIES else "unexpected_error")
        self.http_status = (http_status if type(http_status) is int and
                            100 <= http_status <= 599 else None)
        diagnostic = self.category
        if self.http_status is not None:
            diagnostic += ":http_status=%d" % self.http_status
        super().__init__(diagnostic)


def device_list_diagnostic(error):
    """Revalidate structured fields; never forward exception text or class names."""
    if isinstance(error, DeviceListError):
        return str(DeviceListError(error.category, error.http_status))
    return "unexpected_error"


def _ts():
    return int(time.time() * 1000)


def _json(text):
    return json.loads(text.replace("&&&START&&&", "").strip())


def find_credentials(cookies):
    """Select unexpired cookies applicable to the actual Find API, not other services."""
    host, path = "i.mi.com", "/find/v3/device/status/list"
    selected = {}
    for name in ("serviceToken", "userId"):
        candidates = []
        for cookie in cookies:
            if cookie.name != name or cookie.is_expired():
                continue
            domain = cookie.domain.lstrip(".").lower()
            if not domain or not (host == domain or (
                    cookie.domain_specified and host.endswith("." + domain))):
                continue
            cookie_path = cookie.path or "/"
            if not (path == cookie_path or (path.startswith(cookie_path) and (
                    cookie_path.endswith("/") or path[len(cookie_path):].startswith("/")))):
                continue
            value = cookie.value
            if not isinstance(value, str) or not value or any(
                    ord(c) <= 32 or ord(c) >= 127 or c in ';,"\\' for c in value):
                continue
            candidates.append(((len(domain), len(cookie_path)), value))
        if not candidates:
            raise XiaomiError("未取得适用于查找设备接口的有效 Cookie")
        rank = max(item[0] for item in candidates)
        values = {value for score, value in candidates if score == rank}
        if len(values) != 1:
            raise XiaomiError("查找设备 Cookie 存在歧义，请重新授权")
        selected[name] = values.pop()
    return selected["serviceToken"], selected["userId"]


def login(username, password):
    """返回 (service_token, user_id, cookie_header)。"""
    session = requests.Session()
    session.headers.update({"User-Agent": UA})
    try:
        r = session.get("https://i.mi.com/api/user/login", params={
            "ts": _ts(), "followUp": "https://i.mi.com/", "_locale": "zh_CN"}, timeout=30)
        login_url = (r.json().get("data") or {}).get("loginUrl")
        if not login_url:
            raise XiaomiError("获取登录地址失败")
        r = session.get(login_url, timeout=30)
        password_url = str(r.url)
        r = session.post("https://account.xiaomi.com/pass/serviceLoginAuth2", timeout=30, data={
            "bizDeviceType": "", "needTheme": "false", "showActiveX": "false",
            "user": username, "hash": __import__("hashlib").md5(password.encode()).hexdigest().upper(),
            "_json": "true"}, headers={
            **FIND_HEADERS, "Origin": "https://account.xiaomi.com", "Referer": password_url,
            "Accept": "application/json, text/plain, */*"})
        auth = _json(r.text)
        if auth.get("code") != 0:
            raise XiaomiError("账号或密码不正确，或需要短信验证（%s）" % auth.get("code"))
        notification_url = auth.get("notificationUrl")
        if not notification_url:
            raise XiaomiError("登录未返回校验地址")
        session.get(notification_url, timeout=30)
        context = __import__("urllib.parse", fromlist=["parse_qs"]).parse_qs(
            __import__("urllib.parse", fromlist=["urlparse"]).urlparse(notification_url).query)["context"][0]
        r = session.get("https://account.xiaomi.com/identity/list", params={
            "sid": "i.mi.com", "supportedMask": "0", "_locale": "zh_CN", "context": context}, timeout=30)
        find_device_url = _json(r.text).get("findDeviceUrl")
        if not find_device_url:
            raise XiaomiError("未获取到查找设备授权")
        session.get(find_device_url, timeout=30)
        token, user_id = find_credentials(session.cookies)
        header = "userId=%s;serviceToken=%s" % (user_id, token)
        return token, user_id, header
    except requests.RequestException:
        raise XiaomiError("小米授权网络请求失败") from None


QR_SESSIONS = {}


def qr_start():
    """生成小米扫码登录二维码，返回 (ticket, 二维码地址)。"""
    s = requests.Session()
    s.headers.update({"User-Agent": UA})
    r = s.get("https://i.mi.com/api/user/login", params={
        "ts": _ts(), "followUp": "https://i.mi.com/", "_locale": "zh_CN"}, timeout=30)
    login_url = (r.json().get("data") or {}).get("loginUrl")
    if not login_url:
        raise XiaomiError("无法获取登录地址")
    from urllib.parse import urlparse
    query = urlparse(login_url).query
    r2 = s.get("https://account.xiaomi.com/longPolling/loginUrl?" + query + "&_json=true", timeout=30)
    data = _json(r2.text)
    lp = data.get("lp")
    qr = data.get("qr")
    if not lp or not qr:
        raise XiaomiError("二维码生成失败")
    QR_SESSIONS[lp] = {"session": s, "created": time.time()}
    return lp, qr, data.get("qrTips", "")


def qr_poll(ticket):
    """查询扫码结果；成功返回 (token, uid)。"""
    item = QR_SESSIONS.get(ticket)
    if not item:
        return None, "expired"
    s = item["session"]
    try:
        r = s.get(ticket, timeout=35)
        data = _json(r.text)
    except Exception:
        return None, "waiting"
    if data.get("code") == 0 and data.get("location"):
        try:
            callback = s.get(data["location"], timeout=30)
            callback.raise_for_status()
        except requests.RequestException:
            return None, "授权回调网络请求失败，请重试"
        try:
            token, uid = find_credentials(s.cookies)
        except XiaomiError:
            return None, "未取得查找设备授权，请重新扫码"
        QR_SESSIONS.pop(ticket, None)
        return (token, uid), "ok"
    return None, "小米未返回登录授权，请重新扫码" if data.get("desc") else "waiting"


class Client:
    def __init__(self, service_token, user_id):
        self.token = service_token
        self.user_id = user_id
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": UA, **FIND_HEADERS})

    def _headers(self):
        return {"Cookie": "userId=%s;serviceToken=%s" % (self.user_id, self.token)}

    def device_list(self):
        try:
            r = self.session.get("https://i.mi.com/find/v3/device/status/list",
                                 headers=self._headers(), params={"ts": _ts()}, timeout=30,
                                 allow_redirects=False)
        except requests.RequestException:
            # Request exceptions can contain URLs, cookies or response text.
            raise DeviceListError("network_error") from None
        status = r.status_code
        if type(status) is not int or not 100 <= status <= 599:
            raise DeviceListError("invalid_structure") from None
        if status != 200:
            raise DeviceListError("http_non_200", status) from None
        try:
            data = r.json()
        except ValueError:
            raise DeviceListError("non_json", status) from None
        if not isinstance(data, dict) or not isinstance(data.get("result"), str):
            raise DeviceListError("invalid_structure", status) from None
        if data["result"] != "ok":
            # Do not include result, API codes, messages, or redirect URLs.
            raise DeviceListError("api_rejected", status) from None
        raw = data.get("data")
        rows = raw if isinstance(raw, list) else (
            raw.get("deviceList") if isinstance(raw, dict) else None)
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise DeviceListError("invalid_structure", status) from None
        return rows

    def device_status(self, device, sync_mode=0):
        r = self.session.get("https://i.mi.com/find/v3/device/status", headers=self._headers(),
                             params={"ts": _ts(), "targetUserId": device["userId"],
                                     "targetFid": device["fid"], "syncMode": sync_mode}, timeout=40)
        data = r.json() if r.status_code == 200 else {}
        if data.get("result") != "ok":
            return None
        raw = data.get("data") or {}
        if isinstance(raw, dict) and "deviceList" in raw:
            return next((d for d in raw["deviceList"] if str(d.get("fid")) == str(device["fid"])), None)
        return raw if isinstance(raw, dict) else None


def _component(device):
    for c in device.get("componentList") or []:
        if c.get("locationList"):
            return c
    coms = device.get("componentList") or []
    return coms[0] if coms else None


def extract(device):
    """把 v3 设备对象转成位置点。"""
    comp = _component(device or {})
    if not comp:
        return None
    locs = comp.get("locationList") or []
    if not locs:
        return None
    loc = max(locs, key=lambda x: x.get("clientUpdateTime") or 0)
    if not loc.get("clientUpdateTime"):
        return None
    transformed = loc.get("gpsInfoTransformed") or loc.get("gpsInfo") or []
    point = None
    for item in transformed:
        if item.get("latitude") is not None:
            ct = (item.get("coordinateType") or "").lower()
            lat, lon = float(item["latitude"]), float(item["longitude"])
            # original = gcj02；wgs84 直接使用
            if ct in ("original", "gcj02") or ct == "":
                lat, lon = _gcj_to_wgs(lat, lon)
            point = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(
                        loc["clientUpdateTime"] / 1000)) if loc.get("clientUpdateTime") else None,
                     "lat": lat, "lon": lon,
                     "acc": item.get("accuracy"),
                     "battery": (comp.get("batteryInfo") or {}).get("level"),
                     "source": "xiaomi-cloud"}
            break
    if point is None:
        return None
    if not point.get("battery"):
        point["battery"] = (comp.get("batteryInfo") or {}).get("level")
    return point


def status_of(device):
    """提取设备在线/充电/地址/更新时间等状态。"""
    comp = _component(device or {}) or {}
    locs = comp.get("locationList") or []
    loc = max(locs, key=lambda item: item.get("clientUpdateTime") or 0) if locs else {}
    battery = comp.get("batteryInfo") or {}
    return {
        "online": 1 if comp.get("online") else 0,
        "charging": 1 if battery.get("charging") else 0,
        "address": loc.get("address") or "",
        "accuracy": loc.get("accuracy"),
        "status": device.get("status") or "",
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(
            loc["clientUpdateTime"] / 1000)) if loc.get("clientUpdateTime") else None,
    }


def device_meta(device):
    comp = _component(device or {}) or {}
    models = device.get("componentModelInfo") or {}
    name = ""
    try:
        info = list(models.values())[0]
        raw = (info.get("modelName") or {}).get("zhCN") or (info.get("modelName") or {}).get("defaultName")
        if raw:
            name = json.loads(raw).get("modelName", "")
    except Exception:
        name = ""
    return {
        "device_id": "xiaomi-" + str(device.get("fid")),
        "fid": device.get("fid"),
        "user_id": device.get("userId"),
        "did": comp.get("did"),
        "name": name or ("小米设备 " + str(device.get("fid"))[-4:]),
        "online": any(c.get("online") for c in device.get("componentList") or []),
        "support_cmds": comp.get("supportCmds") or [],
    }


_PI = 3.141592653589793
_A = 6378245.0
_EE = 0.00669342162296594323


def _out(lat, lon):
    return not (72.004 <= lon <= 137.8347 and 0.8293 <= lat <= 55.8271)


def _tl(x, y):
    r = -100 + 2*x + 3*y + 0.2*y*y + 0.1*x*y + 0.2*abs(x)**0.5
    r += (20*__import__("math").sin(6*x*_PI) + 20*__import__("math").sin(2*x*_PI))*2/3
    r += (20*__import__("math").sin(y*_PI) + 40*__import__("math").sin(y/3*_PI))*2/3
    r += (160*__import__("math").sin(y/12*_PI) + 320*__import__("math").sin(y*_PI/30))*2/3
    return r


def _tg(x, y):
    r = 300 + x + 2*y + 0.1*x*x + 0.1*x*y + 0.1*abs(x)**0.5
    r += (20*__import__("math").sin(6*x*_PI) + 20*__import__("math").sin(2*x*_PI))*2/3
    r += (20*__import__("math").sin(x*_PI) + 40*__import__("math").sin(x/3*_PI))*2/3
    r += (150*__import__("math").sin(x/12*_PI) + 300*__import__("math").sin(x/30*_PI))*2/3
    return r


def _gcj_to_wgs(lat, lon):
    if _out(lat, lon):
        return lat, lon
    dlat, dlon = _tl(lon-105, lat-35), _tg(lon-105, lat-35)
    rad = lat/180*_PI
    magic = 1 - _EE*__import__("math").sin(rad)**2
    sq = __import__("math").sqrt(magic)
    dlat = dlat*180/((_A*(1-_EE))/(magic*sq)*_PI)
    dlon = dlon*180/(_A/sq*__import__("math").cos(rad)*_PI)
    return lat - dlat, lon - dlon
