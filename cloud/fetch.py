"""Actions collector; all persistent device data is encrypted."""
import copy
import json
import os
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import common
import schedule
import xiaomi_cloud
from cloud.crypto_state import encrypt, decrypt


class CollectorError(RuntimeError):
    """Stable, non-sensitive error category safe to show in Actions logs."""


def collect(client, previous=None, plans=None, now=None):
    now = now or common.now_utc()
    stamp = common.utc_iso(now)
    state = copy.deepcopy(previous or {"devices": []})
    state.update(updated=stamp, error=None)
    plans = plans or {}
    # Keep the non-secret plan definition with the encrypted state so the
    # dashboard can export a complete, round-trippable cloud-plans.json.
    state["plan"] = {
        "schedule": copy.deepcopy(plans.get("schedule") or {}),
        "holidays": copy.deepcopy(plans.get("holidays") or {}),
    }
    rows = {d["device_id"]: d for d in state["devices"]}
    try:
        listed = client.device_list()
        if not listed:
            raise xiaomi_cloud.DeviceListError("empty_list")
    except Exception as exc:
        category = xiaomi_cloud.device_list_diagnostic(exc)
        if not rows:
            raise CollectorError("device_list_unavailable:" + category) from None
        state["error"] = "设备列表获取失败（%s），显示历史数据" % category
        for row in rows.values():
            row["query_status"] = "设备列表失败（%s），显示历史数据" % category
        return state
    for device in listed:
        meta = xiaomi_cloud.device_meta(device)
        did = meta["device_id"]
        row = rows.setdefault(did, {"device_id": did, "name": meta["name"], "last_point": None})
        row.update(name=meta["name"], listed_at=stamp)
        settings = plans.get("devices", {}).get(did, {})
        row["settings"] = settings
        policy = dict(settings, device_id=did)
        global_plan = plans.get("schedule") or {"enabled": True, "skip_holidays": False,
            "default": {"enabled": True, "start": "00:00", "end": "00:00", "interval_minutes": 10}}
        if not schedule.device_due(policy, global_plan, plans.get("holidays") or {},
                                   row.get("last_query"), now, minimum=10):
            row["query_status"] = "等待计划时段或间隔（显示上次位置）"
            continue
        row["last_query"] = stamp
        try:
            status = client.device_status(device, 0)
            if not status:
                raise ValueError("no status")
            row.update(xiaomi_cloud.status_of(status))
            point = xiaomi_cloud.extract(status)
            if point:
                old = row.get("last_point")
                if not old or point["ts"] >= old["ts"]:
                    point["gcj_lat"], point["gcj_lon"] = common.wgs84_to_gcj02(point["lat"], point["lon"])
                    row["last_point"] = point
                row["query_status"] = "查询成功"
            else:
                row["query_status"] = "暂无新位置，保留上次位置"
        except Exception as exc:
            row["query_status"] = "本次查询失败（%s），保留上次位置" % type(exc).__name__
    state["devices"] = list(rows.values())
    return state


def read_previous(envelope, password, previous_password=None):
    """Try the active and optional previous password; return None if neither works."""
    candidates = [password]
    if previous_password and previous_password != password:
        candidates.append(previous_password)
    for candidate in candidates:
        try:
            return decrypt(envelope, candidate)
        except Exception:
            # A rotated password or stale/corrupt state must not permanently
            # block collection. main() will only replace it after fresh data
            # has been collected successfully.
            continue
    return None


def main():
    required = ("LOCATOR_PASSWORD", "XM_TOKEN", "XM_UID")
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise CollectorError("missing_required_secrets:" + ",".join(missing))
    password = os.environ["LOCATOR_PASSWORD"]
    target = Path("site/state.enc.json")
    previous = None
    reset_unreadable_state = False
    if target.exists():
        try:
            envelope = json.loads(target.read_text(encoding="utf-8"))
        except Exception:
            envelope = None
        previous = read_previous(envelope, password,
                                 os.environ.get("LOCATOR_PREVIOUS_PASSWORD"))
        reset_unreadable_state = previous is None
        if reset_unreadable_state:
            print("警告：旧加密状态无法解锁；本次将重新采集，旧位置历史不会带入。")
    try:
        plans = json.loads(os.environ.get("XM_PLANS") or "{}")
        schedule.validate_cloud_plans(plans)
    except Exception as exc:
        raise CollectorError("invalid_cloud_plans:" + type(exc).__name__) from None
    if len(password) < 12:
        raise CollectorError("invalid_locator_password_length")
    result = collect(xiaomi_cloud.Client(os.environ["XM_TOKEN"], os.environ["XM_UID"]), previous, plans)
    if not any(row.get("last_point") for row in result.get("devices", [])):
        raise CollectorError("no_usable_location; state file unchanged")
    target.parent.mkdir(exist_ok=True)
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(encrypt(result, password)), encoding="utf-8")
    tmp.replace(target)
    print("已生成加密看板数据")

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Never print arbitrary API exception text; show only a stable safe category.
        category = str(exc) if isinstance(exc, CollectorError) else type(exc).__name__
        print("查询或加密失败（%s）；请检查必需 Secrets、计划格式及密码轮换设置。未发布新数据。" % category, file=sys.stderr)
        sys.exit(1)
