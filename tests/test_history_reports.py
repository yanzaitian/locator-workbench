"""Offline, synthetic evidence for the encrypted-state enrichment contract."""
import copy
import datetime as dt
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cloud.history_reports import enrich_state
from cloud.crypto_state import decrypt, encrypt
from cloud.fetch import collect


def point(ts="2026-10-20T00:00:00Z", lat=31.2, lon=121.5, **extra):
    return dict(ts=ts, lat=lat, lon=lon, acc=20, battery=0,
                source="xiaomi-cloud", gcj_lat=31.198, gcj_lon=121.504, **extra)


def state_with(*points):
    return {"devices": [{"device_id": f"device-{i}", "name": "synthetic",
                         "last_point": p} for i, p in enumerate(points)],
            "plan": {"schedule": {}}, "unrelated": {"keep": [1]}}


def report(state, month="2026-10", did="device-0"):
    return next(item for item in state["reports"]
                if item["month"] == month and item["device_id"] == did)


def test_preserves_input_and_existing_point_schema_without_aliases():
    original = state_with(point())
    before = copy.deepcopy(original)
    result = enrich_state(original, "2026-10-26T00:00:00Z")
    assert original == before
    assert result["history"]["device-0"]["points"] == [point()]
    result["history"]["device-0"]["points"][0]["battery"] = 50
    result["unrelated"]["keep"].append(2)
    assert original == before


def test_cached_point_never_becomes_daily_trajectory_and_calls_are_idempotent():
    first = enrich_state(state_with(point()), "2026-10-20T01:00:00Z")
    assert enrich_state(first, "2026-10-20T01:00:00Z") == first
    later = enrich_state(first, "2026-10-30T01:00:00Z")
    assert later["history"] == first["history"]
    closed = enrich_state(later, "2026-11-01T00:00:00Z")
    stats = report(closed)["summary"]
    assert (stats["point_count"], stats["active_days"]) == (1, 1)
    assert stats["sample_start"] == stats["sample_end"] == point()["ts"]
    assert stats["coverage"] == "partial_window"
    assert "月中开始积累" in report(closed)["content"]


def test_same_timestamp_different_locations_kept_same_location_new_time_kept():
    first = enrich_state(state_with(point()), "2026-10-21T00:00:00Z")
    first["devices"][0]["last_point"] = point(lat=32)
    second = enrich_state(first, "2026-10-22T00:00:00Z")
    second["devices"][0]["last_point"] = point("2026-10-21T00:00:00Z")
    third = enrich_state(second, "2026-10-23T00:00:00Z")
    assert len(third["history"]["device-0"]["points"]) == 3


def test_equivalent_offset_dedup_first_stored_metadata_wins():
    first = enrich_state(state_with(point()), "2026-10-21T00:00:00Z")
    changed = point("2026-10-20T08:00:00+08:00")
    changed["battery"] = 99
    first["devices"][0]["last_point"] = changed
    result = enrich_state(first, "2026-10-22T00:00:00Z")
    assert result["history"]["device-0"]["points"] == [point()]


def test_retains_every_sample_in_previous_and_current_month_and_sorts():
    points = [point("2026-09-30T15:59:59Z"), point("2026-09-30T16:00:00Z"),
              point("2026-10-31T15:59:59Z"), point("2026-10-31T16:00:00Z")]
    # Above 10-minute nominal frequency: retention must not downsample.
    points += [point(f"2026-10-20T00:{i:02d}:00Z") for i in range(60)]
    state = state_with(None)
    state["history"] = {"device-0": {"started_at": "2026-09-01T00:00:00Z",
                                     "points": list(reversed(points))}}
    result = enrich_state(state, "2026-11-01T00:00:00Z")
    kept = result["history"]["device-0"]["points"]
    assert len(kept) == 63
    assert kept == sorted(points[1:], key=lambda p: p["ts"])
    assert report(result)["summary"]["point_count"] == 62
    assert report(result)["summary"]["coverage"] == "full_window"


def test_removed_devices_are_kept_separate_and_receive_reports():
    original = enrich_state(state_with(point(), point(lat=5)), "2026-10-21T00:00:00Z")
    original["devices"] = original["devices"][1:]
    result = enrich_state(original, "2026-11-01T00:00:00Z")
    assert set(result["history"]) == {"device-0", "device-1"}
    assert {r["device_id"] for r in result["reports"] if r["month"] == "2026-10"} == {"device-0", "device-1"}
    assert all(r["summary"]["point_count"] == 1 for r in result["reports"] if r["month"] == "2026-10")


def test_first_run_distinguishes_not_started_from_started_but_empty():
    first = enrich_state(state_with(None), "2026-10-20T00:00:00Z")
    september = report(first, "2026-09")
    assert september["summary"]["coverage"] == "not_started"
    assert "尚未开始积累" in september["content"]
    next_month = enrich_state(first, "2026-11-01T00:00:00Z")
    october = report(next_month)
    assert october["summary"]["data_status"] == "no_samples"
    assert october["summary"]["sample_start"] is None
    assert "已开始积累但该月未保存有效样本" in october["content"]


def test_first_run_with_real_old_cached_point_marks_coverage_honestly():
    first = enrich_state(state_with(point()), "2026-11-01T00:00:00Z")
    stats = report(first)["summary"]
    assert stats["coverage"] == "not_started"
    assert stats["point_count"] == stats["samples_before_accumulation"] == 1
    assert "真实缓存时间" in report(first)["content"]


@pytest.mark.parametrize("now,month,start,end", [
    ("2026-12-31T16:00:00Z", "2026-12", "2026-11-30T16:00:00Z", "2026-12-31T16:00:00Z"),
    ("2028-02-29T16:00:00Z", "2028-02", "2028-01-31T16:00:00Z", "2028-02-29T16:00:00Z"),
])
def test_local_month_boundaries_year_and_leap_day(now, month, start, end):
    result = enrich_state(state_with(point(start)), now)
    stats = report(result, month)["summary"]
    assert stats["window_start"] == start
    assert stats["window_end_exclusive"] == end
    assert stats["point_count"] == 1
    assert report(result, month)["is_final"] is True


def test_no_current_month_report_before_local_midnight():
    result = enrich_state(state_with(point()), "2026-10-31T15:59:59Z")
    assert report(result, "2026-09")["is_final"] is True
    assert report(result)["is_final"] is False
    result = enrich_state(result, "2026-10-31T16:00:00Z")
    assert report(result)["summary"]["point_count"] == 1


def test_late_real_sample_revises_previous_summary_but_not_unrelated_reports():
    state = enrich_state(state_with(None), "2026-10-10T00:00:00Z")
    state = enrich_state(state, "2026-11-01T00:00:00Z")
    old_september = copy.deepcopy(report(state, "2026-09"))
    state["devices"][0]["last_point"] = point()
    revised = enrich_state(state, "2026-11-02T00:00:00Z")
    assert report(revised)["summary"]["point_count"] == 1
    assert report(revised, "2026-09") == old_september
    assert enrich_state(revised, "2026-11-03T00:00:00Z")["reports"] == revised["reports"]


def test_report_retention_keeps_twelve_closed_months():
    state = state_with(None)
    months = [f"2025-{i:02d}" for i in range(1, 13)] + [f"2026-{i:02d}" for i in range(1, 12)]
    state["reports"] = [{"month": m, "device_id": "prior", "content": m} for m in months]
    state["reports"].append({"month": "invalid", "device_id": "prior"})
    result = enrich_state(state, "2026-11-01T00:00:00Z")
    assert len({r["month"] for r in result["reports"]}) == 13
    assert min(r["month"] for r in result["reports"]) == "2025-11"
    assert max(r["month"] for r in result["reports"]) == "2026-11"
    assert report(result, "2025-11", "prior") == report(state, "2025-11", "prior")


@pytest.mark.parametrize("bad", [None, {}, point("not-a-date"), point("2026-10-20T00:00:00"),
    point("2026-12-01T00:00:00Z"), point(lat=float("nan")), point(lon=float("inf")),
    point(lat=91), point(lon=-181), point(lat=True), point(lat="31.2")])
def test_invalid_and_future_points_do_not_become_evidence(bad):
    result = enrich_state(state_with(bad), "2026-11-01T00:00:00Z")
    assert result["history"]["device-0"]["points"] == []
    assert report(result)["summary"]["point_count"] == 0
    json.dumps(result["history"], allow_nan=False)


def test_ai_receives_only_detached_summary_and_no_id_coordinates_or_name(capsys):
    seen = []
    def summarizer(stats, key):
        assert key == "synthetic-key"
        seen.append(copy.deepcopy(stats))
        stats["point_count"] = 987
        return {"observations": ["sample_limits"]}
    state = enrich_state(state_with(point()), "2026-10-21T00:00:00Z")
    result = enrich_state(state, "2026-11-01T00:00:00Z", "synthetic-key", summarizer=summarizer)
    assert len(seen) == 1
    payload = json.dumps(seen)
    assert all(word not in payload for word in ['"lat"', '"lon"', "device-0", "synthetic", "31.2", "121.5"])
    assert report(result)["summary"]["point_count"] == 1
    assert report(result)["ai_status"] == "ok"
    assert report(result)["ai_notes"]
    assert "synthetic-key" not in json.dumps(result)
    assert capsys.readouterr() == ("", "")
    enrich_state(result, "2026-11-02T00:00:00Z", "synthetic-key", summarizer=summarizer)
    assert len(seen) == 1


def test_ai_failure_retains_identical_template_and_hides_exception(capsys):
    def fail(stats, key):
        raise RuntimeError("secret-body:" + key)
    state = enrich_state(state_with(point()), "2026-10-21T00:00:00Z")
    local = enrich_state(state, "2026-11-01T00:00:00Z")
    failed = enrich_state(state, "2026-11-01T00:00:00Z", "synthetic-key", summarizer=fail)
    assert report(failed)["ai_status"] == "failed"
    assert report(failed)["content"] == report(local)["content"]
    assert failed["history"] == local["history"]
    assert "secret-body" not in json.dumps(failed)
    assert "synthetic-key" not in json.dumps(failed)
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("response", ["invented address", {"content": "invented address"},
    {"observations": ["invented address"]}, {"observations": ["no_samples"]},
    {"observations": None}, {"observations": [["sample_limits"]]}])
def test_ai_untrusted_or_factually_inapplicable_prose_is_never_stored(response):
    result = enrich_state(state_with(point()), "2026-11-01T00:00:00Z", "synthetic-key",
                          summarizer=lambda stats, key: response)
    assert report(result)["ai_status"] == "invalid_response"
    assert report(result)["ai_notes"] == []
    assert "invented address" not in json.dumps(result)


def test_ai_is_explicitly_optional_and_no_key_never_invokes_callback():
    def forbidden(*args):
        pytest.fail("callback must not run")
    result = enrich_state(state_with(None), "2026-11-01T00:00:00Z", summarizer=forbidden)
    assert report(result)["ai_status"] == "disabled"
    result = enrich_state(state_with(point()), "2026-11-01T00:00:00Z", "synthetic-key")
    assert report(result)["ai_status"] == "not_configured"


def test_default_clock_is_injectable_and_naive_now_is_rejected(monkeypatch):
    from cloud import history_reports
    monkeypatch.setattr(history_reports.common, "now_utc",
                        lambda: dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc))
    assert report(enrich_state(state_with(None)))["is_final"] is True
    with pytest.raises(ValueError, match="^invalid_now$"):
        enrich_state(state_with(None), dt.datetime(2026, 11, 1))


def test_invalid_history_fails_closed_without_mutating_or_echoing_payload():
    state = state_with(None)
    state["history"] = {"device-0": {"started_at": "secret-payload", "points": []}}
    original = copy.deepcopy(state)
    with pytest.raises(ValueError, match="^invalid_history_schema$"):
        enrich_state(state, "2026-11-01T00:00:00Z")
    assert state == original


def test_collect_enrich_encrypt_roundtrip_entirely_offline(monkeypatch):
    import builtins
    import socket
    import urllib.request
    def forbidden(*args, **kwargs):
        pytest.fail("unexpected filesystem or network operation")
    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    class Client:
        def device_list(self):
            return [{"fid": "fake-device", "userId": "fake-user", "model": "Phone"}]
        def device_status(self, device, mode):
            return {"componentList": [{"batteryInfo": {"level": 0}, "locationList": [{
                "clientUpdateTime": 1792454400000,
                "gpsInfoTransformed": [{"latitude": 31.2, "longitude": 121.5,
                                        "coordinateType": "wgs84", "accuracy": 20}]}]}]}
    now = dt.datetime(2026, 10, 26, tzinfo=dt.timezone.utc)
    collected = collect(Client(), now=now)
    enriched = enrich_state(collected, now)
    did = collected["devices"][0]["device_id"]
    assert enriched["history"][did]["points"] == [collected["devices"][0]["last_point"]]
    envelope = encrypt(enriched, "synthetic-password-123")
    assert set(envelope) == {"version", "iterations", "salt", "nonce", "ciphertext"}
    assert decrypt(envelope, "synthetic-password-123") == enriched
    assert "history" not in json.dumps(envelope)


def test_subsecond_samples_are_preserved_and_offset_equivalents_deduplicated():
    state = enrich_state(state_with(point("2026-10-20T00:00:00.100Z")), "2026-10-21T00:00:00Z")
    state["devices"][0]["last_point"] = point("2026-10-20T00:00:00.200Z")
    state = enrich_state(state, "2026-10-22T00:00:00Z")
    state["devices"][0]["last_point"] = point("2026-10-20T08:00:00.100+08:00")
    state = enrich_state(state, "2026-10-23T00:00:00Z")
    assert [p["ts"] for p in state["history"]["device-0"]["points"]] == [
        "2026-10-20T00:00:00.100000Z", "2026-10-20T00:00:00.200000Z"]


def test_multiple_months_roll_history_forward_and_preserve_completed_report():
    state = enrich_state(state_with(point()), "2026-10-21T00:00:00Z")
    state = enrich_state(state, "2026-11-01T00:00:00Z")
    october = copy.deepcopy(report(state))
    state["devices"][0]["last_point"] = point("2026-11-20T00:00:00Z")
    state = enrich_state(state, "2026-11-21T00:00:00Z")
    state = enrich_state(state, "2026-12-01T00:00:00Z")
    assert [p["ts"] for p in state["history"]["device-0"]["points"]] == ["2026-11-20T00:00:00Z"]
    assert report(state) == october
    assert report(state, "2026-11")["summary"]["point_count"] == 1
    assert report(state, "2026-11")["summary"]["coverage"] == "full_window"


def test_local_days_and_gaps_are_derived_only_from_sorted_real_samples():
    points = [point("2026-10-02T16:00:00Z"), point("2026-10-01T16:00:00Z"),
              point("2026-10-01T15:59:59Z")]
    state = state_with(None)
    state["history"] = {"device-0": {"started_at": "2026-09-30T16:00:00Z", "points": points}}
    result = enrich_state(state, "2026-11-01T00:00:00Z")
    stats = report(result)["summary"]
    assert stats["active_days"] == stats["point_count"] == 3
    assert stats["longest_sample_gap_hours"] == 24
    assert stats["sample_start"] == "2026-10-01T15:59:59Z"
    assert stats["sample_end"] == "2026-10-02T16:00:00Z"


def test_factually_supported_ai_codes_render_local_text():
    state = state_with(None)
    state["history"] = {"device-0": {"started_at": "2026-10-01T00:00:00Z", "points": [
        point("2026-10-10T00:00:00Z"), point("2026-10-11T00:00:00Z")]}}
    result = enrich_state(state, "2026-11-01T00:00:00Z", "synthetic-key",
                          summarizer=lambda stats, key: {"observations": ["collection_gap"]})
    assert report(result)["ai_status"] == "ok"
    assert "采样间隔" in report(result)["ai_notes"][0]
    empty = enrich_state(state_with(None), "2026-11-01T00:00:00Z", "synthetic-key",
                         summarizer=lambda stats, key: {"observations": ["no_samples"]})
    assert report(empty)["ai_status"] == "no_samples"


@pytest.mark.parametrize("state", [None, {"devices": {}}, {"history": []}, {"reports": {}},
    {"devices": [{"device_id": None}]}, {"devices": [{}]}])
def test_invalid_state_schema_uses_stable_errors(state):
    with pytest.raises(ValueError, match="^invalid_(state|state_schema|device_schema)$"):
        enrich_state(state, "2026-11-01T00:00:00Z")


def test_huge_numeric_coordinate_is_rejected_without_overflow():
    result = enrich_state(state_with(point(lat=10**500)), "2026-11-01T00:00:00Z")
    assert result["history"]["device-0"]["points"] == []


def test_first_accumulation_produces_array_preview_without_any_ai_cost():
    def forbidden(*args):
        pytest.fail("first-day preview or empty previous month must not call AI")
    result = enrich_state(state_with(point("2026-10-26T00:00:00Z")),
                          "2026-10-26T01:00:00Z", "synthetic-key", summarizer=forbidden)
    assert isinstance(result["reports"], list)
    preview = report(result)
    assert {"device_id", "month", "content", "source", "generated_at"} <= preview.keys()
    assert preview["status"] == "preview"
    assert preview["is_final"] is False
    assert preview["ai_status"] == "preview"
    assert preview["ai_notes"] == []
    assert "月份未结束" in preview["content"]
    assert "当月尚未结束" in preview["content"]
    assert "2026-10-26T01:00:00Z" == preview["summary"]["accumulation_started_at"]
    assert report(result, "2026-09")["ai_status"] == "no_samples"


def test_empty_first_day_still_has_usable_current_preview():
    result = enrich_state(state_with(None), "2026-10-26T00:00:00Z")
    preview = report(result)
    assert preview["summary"]["point_count"] == 0
    assert preview["summary"]["coverage"] == "partial_window"
    assert "已开始积累但该月未保存有效样本" in preview["content"]
    assert "月份未结束" in preview["content"]


def test_preview_updates_with_samples_without_ai_and_closes_once_at_month_boundary():
    calls = []
    def summarizer(stats, key):
        calls.append(copy.deepcopy(stats))
        return {"observations": ["sample_limits"]}
    state = enrich_state(state_with(point("2026-10-26T00:00:00Z")),
                         "2026-10-26T01:00:00Z", "synthetic-key", summarizer=summarizer)
    before = report(state)["content"]
    state["devices"][0]["last_point"] = point("2026-10-27T00:00:00Z")
    state = enrich_state(state, "2026-10-27T01:00:00Z", "synthetic-key", summarizer=summarizer)
    assert report(state)["content"] != before
    assert report(state)["summary"]["point_count"] == 2
    assert calls == []
    state = enrich_state(state, "2026-10-31T16:00:00Z", "synthetic-key", summarizer=summarizer)
    assert len(calls) == 1
    closed = report(state)
    assert closed["status"] == "completed" and closed["is_final"] is True
    assert closed["ai_status"] == "ok"
    assert "月份未结束" not in closed["content"]
    assert report(state, "2026-11")["is_final"] is False
    assert report(state, "2026-11")["summary"]["point_count"] == 0
    assert len([r for r in state["reports"] if r["month"] == "2026-10"]) == 1
    again = enrich_state(state, "2026-11-01T00:00:00Z", "synthetic-key", summarizer=summarizer)
    assert again["reports"] == state["reports"]
    assert len(calls) == 1


def test_month_closure_runs_ai_even_when_preview_summary_has_not_changed():
    state = enrich_state(state_with(point()), "2026-10-30T00:00:00Z")
    calls = []
    def summarizer(stats, key):
        calls.append(stats)
        return {"observations": []}
    closed = enrich_state(state, "2026-11-01T00:00:00Z", "synthetic-key", summarizer=summarizer)
    assert report(state)["summary"] == report(closed)["summary"]
    assert report(state)["is_final"] is False
    assert report(closed)["is_final"] is True
    assert len(calls) == 1


def test_unknown_frontend_report_fields_are_preserved_for_unchanged_reports():
    state = enrich_state(state_with(point()), "2026-10-26T00:00:00Z")
    report(state)["frontend_extension"] = {"keep": True}
    state = enrich_state(state, "2026-10-27T00:00:00Z")
    assert report(state)["frontend_extension"] == {"keep": True}
    assert len({(r["device_id"], r["month"]) for r in state["reports"]}) == len(state["reports"])


def test_whole_second_and_fractional_second_samples_sort_chronologically():
    state = state_with(None)
    state["history"] = {"device-0": {"started_at": "2026-10-01T00:00:00Z", "points": [
        point("2026-10-20T00:00:00.100Z"), point("2026-10-20T00:00:00Z")]}}
    result = enrich_state(state, "2026-11-01T00:00:00Z")
    stats = report(result)["summary"]
    assert stats["sample_start"] == "2026-10-20T00:00:00Z"
    assert stats["sample_end"] == "2026-10-20T00:00:00.100000Z"
    assert [p["ts"] for p in result["history"]["device-0"]["points"]] == [
        stats["sample_start"], stats["sample_end"]]


@pytest.fixture(autouse=True)
def forbid_real_http(monkeypatch):
    """Every test in this module must stay offline, including accidental calls."""
    import requests
    def forbidden(*args, **kwargs):
        pytest.fail("real HTTP is forbidden in history/report tests")
    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)


def deepseek_envelope(content='{"observations":["sample_limits"]}', finish="stop"):
    return {"choices": [{"finish_reason": finish, "message": {
        "role": "assistant", "content": content}}]}


@pytest.fixture
def deepseek_http(monkeypatch):
    from unittest.mock import Mock
    from cloud import history_reports
    response = Mock(status_code=200, text=json.dumps(deepseek_envelope()))
    post = Mock(return_value=response)
    monkeypatch.setattr(history_reports.requests, "post", post)
    return post, response


def deepseek_stats():
    state = enrich_state(state_with(point()), "2026-11-01T00:00:00Z")
    return report(state)["summary"]


def test_deepseek_adapter_request_contract_and_private_fields_never_sent(deepseek_http, capsys):
    from cloud.history_reports import summarize_deepseek
    post, response = deepseek_http
    stats = deepseek_stats()
    stats.update(device_id="private-device", name="private-name", lat=31.2, lon=121.5,
                 devices=[{"address": "private-address"}], points=[point()])
    original = copy.deepcopy(stats)
    assert summarize_deepseek(stats, "synthetic-key") == {"observations": ["sample_limits"]}
    post.assert_called_once()
    args, kwargs = post.call_args
    assert args == ("https://api.deepseek.com/chat/completions",)
    assert kwargs["timeout"] == 30
    assert kwargs["allow_redirects"] is False
    assert kwargs["headers"] == {"Authorization": "Bearer synthetic-key",
                                 "Content-Type": "application/json"}
    payload = kwargs["json"]
    assert payload["model"] == "deepseek-chat"
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["stream"] is False
    assert [m["role"] for m in payload["messages"]] == ["system", "user"]
    assert "observations" in payload["messages"][0]["content"]
    assert "sample_limits" in payload["messages"][0]["content"]
    assert "无重复" in payload["messages"][0]["content"]
    sent = json.loads(payload["messages"][1]["content"])
    assert set(sent) == {"point_count", "active_days", "samples_before_accumulation",
                         "longest_sample_gap_hours", "coverage", "data_status"}
    wire = json.dumps(payload)
    assert all(value not in wire for value in ["private-device", "private-name", "private-address",
        "31.2", "121.5", "synthetic-key", stats["sample_start"]])
    assert stats == original
    response.raise_for_status.assert_called_once_with()
    response.close.assert_called_once_with()
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("codes,gap", [([], 0), (["sample_limits"], 0),
    (["collection_gap"], 2), (["sample_limits", "collection_gap"], 2)])
def test_deepseek_adapter_accepts_only_supported_factually_valid_codes(deepseek_http, codes, gap):
    from cloud.history_reports import summarize_deepseek
    post, response = deepseek_http
    stats = deepseek_stats()
    if gap:
        stats.update(point_count=2, longest_sample_gap_hours=gap)
    response.text = json.dumps(deepseek_envelope(json.dumps({"observations": codes})))
    assert summarize_deepseek(stats, "synthetic-key") == {"observations": codes}
    if gap:
        assert "collection_gap" in post.call_args.kwargs["json"]["messages"][0]["content"]


@pytest.mark.parametrize("key", [None, "", "  ", 123, "synthetic\rkey", "synthetic\nkey"])
def test_deepseek_adapter_rejects_invalid_key_before_http(deepseek_http, key):
    from cloud.history_reports import summarize_deepseek
    post, _ = deepseek_http
    with pytest.raises(ValueError, match="^invalid_deepseek_input$"):
        summarize_deepseek(deepseek_stats(), key)
    post.assert_not_called()


@pytest.mark.parametrize("field,value", [
    ("point_count", 0), ("point_count", -1), ("point_count", True), ("point_count", 1.0),
    ("active_days", 0), ("active_days", 2), ("active_days", "private-address"),
    ("samples_before_accumulation", 2), ("samples_before_accumulation", -1),
    ("longest_sample_gap_hours", float("nan")), ("longest_sample_gap_hours", float("inf")),
    ("longest_sample_gap_hours", -1), ("longest_sample_gap_hours", True),
    ("longest_sample_gap_hours", {"lat": 31.2}), ("longest_sample_gap_hours", 1),
    ("coverage", "private-address"), ("coverage", {"device_id": "private-device"}),
    ("data_status", "no_samples"),
])
def test_deepseek_adapter_rejects_invalid_whitelisted_values_before_http(deepseek_http, field, value):
    from cloud.history_reports import summarize_deepseek
    post, _ = deepseek_http
    stats = deepseek_stats()
    stats[field] = value
    with pytest.raises(ValueError, match="^invalid_deepseek_input$"):
        summarize_deepseek(stats, "synthetic-key")
    post.assert_not_called()


@pytest.mark.parametrize("stats", [None, [], {}, {"device_id": "private-device"}])
def test_deepseek_adapter_rejects_missing_or_non_summary_input(deepseek_http, stats):
    from cloud.history_reports import summarize_deepseek
    post, _ = deepseek_http
    with pytest.raises(ValueError, match="^invalid_deepseek_input$"):
        summarize_deepseek(stats, "synthetic-key")
    post.assert_not_called()


@pytest.mark.parametrize("status", [201, 204, 301, 302, 400, 401, 403, 429, 500, 503])
def test_deepseek_adapter_rejects_http_failures_and_redirects_without_retry(deepseek_http, status, capsys):
    from cloud.history_reports import summarize_deepseek
    post, response = deepseek_http
    response.status_code = status
    response.text = "secret-response-body synthetic-key"
    with pytest.raises(RuntimeError, match="^deepseek_summary_failed$") as caught:
        summarize_deepseek(deepseek_stats(), "synthetic-key")
    assert caught.value.__suppress_context__ is True
    post.assert_called_once()
    response.raise_for_status.assert_called_once()
    response.close.assert_called_once()
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("kind", ["timeout", "connection", "http"])
def test_deepseek_adapter_sanitizes_request_exceptions(deepseek_http, kind, capsys):
    from cloud import history_reports
    post, response = deepseek_http
    errors = {"timeout": history_reports.requests.Timeout,
              "connection": history_reports.requests.ConnectionError,
              "http": history_reports.requests.HTTPError}
    error = errors[kind]("private-response-body synthetic-key")
    if kind == "http":
        response.raise_for_status.side_effect = error
    else:
        post.side_effect = error
    with pytest.raises(RuntimeError, match="^deepseek_summary_failed$") as caught:
        history_reports.summarize_deepseek(deepseek_stats(), "synthetic-key")
    assert "synthetic-key" not in str(caught.value)
    assert "private-response-body" not in str(caught.value)
    assert caught.value.__suppress_context__ is True
    post.assert_called_once()
    if kind == "http":
        response.close.assert_called_once()
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("body", ["", "<html>private-response</html>", "null", "[]", "{}",
    '{"error":{"message":"private-response"}}', '{"choices":[]}', '{"choices":[null]}',
    '{"choices":null}', '{"choices":[],"choices":[]}', '{"choices":[],"x":NaN}',
    json.dumps(deepseek_envelope()) + " trailing", pytest.param(" " * 65537, id="oversized-json"),
    json.dumps({"choices": deepseek_envelope()["choices"] * 2}),
    json.dumps(deepseek_envelope(finish="length")),
    json.dumps(deepseek_envelope(finish="content_filter")),
    json.dumps({"choices": [{"finish_reason": "stop", "message": {"role": "user", "content": "{}"}}]}),
    json.dumps({"choices": [{"finish_reason": "stop", "message": None}]}),
    json.dumps({"choices": [{"finish_reason": "stop", "message": {
        "role": "assistant", "content": "{}", "refusal": "private-response"}}]}),
])
def test_deepseek_adapter_strictly_rejects_invalid_outer_json(deepseek_http, body):
    from cloud.history_reports import summarize_deepseek
    _, response = deepseek_http
    response.text = body
    with pytest.raises(RuntimeError, match="^deepseek_summary_failed$"):
        summarize_deepseek(deepseek_stats(), "synthetic-key")
    response.close.assert_called_once()


@pytest.mark.parametrize("content", [None, {}, "", "not-json", "[]", "null", "{}",
    '```json\n{"observations":[]}\n```', '{"observations":[],"content":"private-address"}',
    '{"observations":null}', '{"observations":"sample_limits"}',
    '{"observations":[1]}', '{"observations":[["sample_limits"]]}',
    '{"observations":["private-address"]}', '{"observations":["collection_gap"]}',
    '{"observations":["sample_limits","sample_limits"]}',
    '{"observations":[],"observations":["sample_limits"]}', '{"observations":[NaN]}',
    '{"observations":[Infinity]}', pytest.param(" " * 65537, id="oversized-json")])
def test_deepseek_adapter_strictly_rejects_invalid_content_json(deepseek_http, content):
    from cloud.history_reports import summarize_deepseek
    _, response = deepseek_http
    response.text = json.dumps(deepseek_envelope(content))
    with pytest.raises(RuntimeError, match="^deepseek_summary_failed$"):
        summarize_deepseek(deepseek_stats(), "synthetic-key")


def test_deepseek_adapter_integrates_only_for_completed_month_once(deepseek_http):
    from cloud.history_reports import summarize_deepseek
    post, _ = deepseek_http
    state = enrich_state(state_with(point()), "2026-10-26T00:00:00Z", "synthetic-key",
                         summarizer=summarize_deepseek)
    post.assert_not_called()
    assert report(state)["ai_status"] == "preview"
    closed = enrich_state(state, "2026-11-01T00:00:00Z", "synthetic-key",
                          summarizer=summarize_deepseek)
    post.assert_called_once()
    assert report(closed)["ai_status"] == "ok"
    assert report(closed)["ai_notes"]
    assert report(closed, "2026-11")["ai_status"] == "preview"
    again = enrich_state(closed, "2026-11-02T00:00:00Z", "synthetic-key",
                         summarizer=summarize_deepseek)
    post.assert_called_once()
    assert again["reports"] == closed["reports"]


def test_deepseek_adapter_failure_preserves_template_history_and_no_secret(deepseek_http, capsys):
    from cloud.history_reports import summarize_deepseek
    post, response = deepseek_http
    response.text = json.dumps(deepseek_envelope('{"observations":["synthetic-key private-response"]}'))
    state = enrich_state(state_with(point()), "2026-10-26T00:00:00Z")
    local = enrich_state(state, "2026-11-01T00:00:00Z")
    failed = enrich_state(state, "2026-11-01T00:00:00Z", "synthetic-key",
                          summarizer=summarize_deepseek)
    post.assert_called_once()
    assert report(failed)["ai_status"] == "failed"
    assert report(failed)["content"] == report(local)["content"]
    assert failed["history"] == local["history"]
    assert "synthetic-key" not in json.dumps(failed)
    assert "private-response" not in json.dumps(failed)
    assert capsys.readouterr() == ("", "")


def test_deepseek_adapter_rejects_duplicate_codes_even_when_both_codes_allowed(deepseek_http):
    from cloud.history_reports import summarize_deepseek
    _, response = deepseek_http
    stats = deepseek_stats()
    stats.update(point_count=2, longest_sample_gap_hours=2)
    response.text = json.dumps(deepseek_envelope('{"observations":["collection_gap","collection_gap"]}'))
    with pytest.raises(RuntimeError, match="^deepseek_summary_failed$"):
        summarize_deepseek(stats, "synthetic-key")


def test_deepseek_adapter_rejects_error_envelope_even_with_valid_choices(deepseek_http):
    from cloud.history_reports import summarize_deepseek
    _, response = deepseek_http
    envelope = deepseek_envelope()
    envelope["error"] = {"message": "private-response synthetic-key"}
    response.text = json.dumps(envelope)
    with pytest.raises(RuntimeError, match="^deepseek_summary_failed$"):
        summarize_deepseek(deepseek_stats(), "synthetic-key")
