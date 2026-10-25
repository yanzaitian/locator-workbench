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
            raise ValueError("empty list")
    except Exception:
        if not rows:
            raise RuntimeError("设备列表不可用且没有历史数据；请检查小米 Secrets") from None
        state["error"] = "设备列表获取失败，请检查小米凭据；保留上次位置"
        for row in rows.values():
            row["query_status"] = "设备列表失败，显示历史数据"
        return state
    for device in listed:
        meta = xiaomi_cloud.device_meta(device)
        did = meta["device_id"]
        row = rows.setdefault(did, {"device_id": did, "name": meta["name"], "last_point": None})
        row.update(name=meta["name"], listed_at=stamp)
        settings = plans.get("devices", {}).get(did, {})
        row["settings"] = settings
        policy = dict(settings, device_id=did)
        policy.setdefault("interval_minutes", 10)
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
        except Exception:
            row["query_status"] = "本次查询失败，保留上次位置"
    state["devices"] = list(rows.values())
    return state


def read_previous(envelope, password, previous_password=None):
    try:
        return decrypt(envelope, password)
    except Exception:
        if not previous_password:
            raise
        return decrypt(envelope, previous_password)


def main():
    password = os.environ["LOCATOR_PASSWORD"]
    target = Path("site/state.enc.json")
    previous = None
    if target.exists():
        # Password changes require LOCATOR_PREVIOUS_PASSWORD for one successful run.
        previous = read_previous(json.loads(target.read_text(encoding="utf-8")), password,
                                 os.environ.get("LOCATOR_PREVIOUS_PASSWORD"))
    plans = json.loads(os.environ.get("XM_PLANS") or "{}")
    schedule.validate_cloud_plans(plans)
    result = collect(xiaomi_cloud.Client(os.environ["XM_TOKEN"], os.environ["XM_UID"]), previous, plans)
    target.parent.mkdir(exist_ok=True)
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(encrypt(result, password)), encoding="utf-8")
    tmp.replace(target)
    print("已生成加密看板数据")

if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("查询或加密失败；请检查 Secrets、计划格式及密码轮换设置。未发布新数据。", file=sys.stderr)
        sys.exit(1)
