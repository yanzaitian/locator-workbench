"""Synthetic monthly distance regressions; no configuration, DB or network IO."""
import copy
import unittest
from unittest.mock import patch

import common
import reports


def point(device, minute, lon, day=1):
    return {
        "device_id": device,
        "ts_utc": f"2026-01-{day:02d}T04:{minute:02d}:00Z",
        "lat": 0.0,
        "lon": lon,
        "battery": 80,
    }


class ReportDistanceTests(unittest.TestCase):
    def setUp(self):
        # Fail immediately if any test reaches real configuration, storage or HTTP.
        for target in (
            "common.load_config", "store.connect", "sqlite3.connect",
            "urllib.request.urlopen",
        ):
            guard = patch(target, side_effect=AssertionError("External IO forbidden"))
            guard.start()
            self.addCleanup(guard.stop)

    def aggregate(self, points):
        original = copy.deepcopy(points)
        with patch("reports.store.get_points", return_value=points) as get_points:
            result = reports.aggregate({}, "2026-01")
        get_points.assert_called_once_with(
            {}, *common.month_range("2026-01"), limit=300000
        )
        self.assertEqual(points, original)
        return result

    def assert_distance(self, result, meters, day_index=0):
        self.assertEqual(result["days"][day_index]["distance_km"], round(meters / 1000, 2))
        self.assertEqual(result["total_distance_km"], round(meters / 1000, 1))

    def test_interleaved_stationary_devices_do_not_create_travel(self):
        result = self.aggregate([
            point("a", 0, 0.0), point("b", 1, 0.01),
            point("a", 2, 0.0), point("b", 3, 0.01),
        ])
        self.assert_distance(result, 0.0)
        self.assertEqual(result["devices"], ["a", "b"])
        self.assertEqual(result["point_count"], 4)
        self.assertEqual(result["days"][0]["points"], 4)

    def test_interleaved_nearby_devices_sum_their_own_travel(self):
        result = self.aggregate([
            point("a", 0, 0.0), point("b", 1, 0.01),
            point("a", 2, 0.001), point("b", 3, 0.012),
        ])
        expected = (common.haversine_m(0, 0, 0, 0.001)
                    + common.haversine_m(0, 0.01, 0, 0.012))
        self.assert_distance(result, expected)

    def test_distant_interleaved_devices_preserve_valid_travel(self):
        result = self.aggregate([
            point("a", 0, 0.0), point("b", 1, 1.0),
            point("a", 2, 0.001), point("b", 3, 1.002),
        ])
        expected = (common.haversine_m(0, 0, 0, 0.001)
                    + common.haversine_m(0, 1, 0, 1.002))
        self.assert_distance(result, expected)

    def test_one_point_per_device_has_no_travel(self):
        result = self.aggregate([point("a", 0, 0.0), point("b", 1, 0.01)])
        self.assert_distance(result, 0.0)

    def test_single_device_keeps_existing_distance(self):
        result = self.aggregate([
            point("a", 0, 0.0), point("a", 1, 0.001), point("a", 2, 0.003),
        ])
        expected = (common.haversine_m(0, 0, 0, 0.001)
                    + common.haversine_m(0, 0.001, 0, 0.003))
        self.assert_distance(result, expected)

    def test_jump_filter_uses_previous_point_of_same_device(self):
        result = self.aggregate([
            point("a", 0, 0.0), point("b", 1, 1.0),
            point("a", 2, 0.03), point("b", 3, 1.001),
            point("a", 4, 0.031),
        ])
        expected = (common.haversine_m(0, 0.03, 0, 0.031)
                    + common.haversine_m(0, 1, 0, 1.001))
        self.assert_distance(result, expected)

    def test_exact_two_kilometer_step_remains_excluded(self):
        with patch("reports.common.haversine_m", return_value=2000.0):
            result = self.aggregate([point("a", 0, 0.0), point("a", 1, 0.001)])
        self.assert_distance(result, 0.0)

    def test_devices_reset_at_local_day_boundary_and_month_sums_days(self):
        points = [
            point("a", 0, 0.0), point("b", 1, 1.0),
            point("a", 2, 0.001), point("b", 3, 1.002),
            point("a", 0, 0.002, day=2), point("b", 1, 1.003, day=2),
        ]
        # 15:59Z and 16:00Z straddle midnight in the report's local timezone.
        for p, timestamp in zip(points, (
            "2026-01-01T15:56:00Z", "2026-01-01T15:57:00Z",
            "2026-01-01T15:58:00Z", "2026-01-01T15:59:00Z",
            "2026-01-01T16:00:00Z", "2026-01-01T16:01:00Z",
        )):
            p["ts_utc"] = timestamp
        result = self.aggregate(points)
        expected = (common.haversine_m(0, 0, 0, 0.001)
                    + common.haversine_m(0, 1, 0, 1.002))
        self.assert_distance(result, expected)
        self.assertEqual(result["days"][1]["distance_km"], 0.0)
        self.assertEqual([d["date"] for d in result["days"]], ["2026-01-01", "2026-01-02"])
        self.assertEqual(result["active_days"], 2)

    def test_missing_empty_and_none_device_ids_share_default(self):
        points = [
            point(None, 0, 0.0), point("b", 1, 1.0),
            point("", 2, 0.001), point("default", 3, 0.002),
            point(None, 4, 0.003),
        ]
        del points[0]["device_id"]
        result = self.aggregate(points)
        expected = sum(common.haversine_m(0, a, 0, b) for a, b in (
            (0, 0.001), (0.001, 0.002), (0.002, 0.003),
        ))
        self.assert_distance(result, expected)
        self.assertEqual(result["devices"], ["b", "default"])

    def test_month_sums_unrounded_device_distances_across_days(self):
        points = []
        for day in (1, 2):
            points.extend([
                point("a", 0, 0.0, day=day), point("b", 1, 1.0, day=day),
                point("a", 2, 0.0003, day=day), point("b", 3, 1.0003, day=day),
            ])
        result = self.aggregate(points)
        daily_meters = (common.haversine_m(0, 0, 0, 0.0003)
                        + common.haversine_m(0, 1, 0, 1.0003))
        self.assertEqual(
            [d["distance_km"] for d in result["days"]],
            [round(daily_meters / 1000, 2)] * 2,
        )
        self.assertEqual(result["total_distance_km"], round(2 * daily_meters / 1000, 1))
        self.assertEqual(result["point_count"], 8)

    def test_empty_month_keeps_zero_distance(self):
        result = self.aggregate([])
        self.assertEqual(result["total_distance_km"], 0.0)
        self.assertEqual(result["days"], [])
        self.assertEqual(result["devices"], [])


if __name__ == "__main__":
    unittest.main()
