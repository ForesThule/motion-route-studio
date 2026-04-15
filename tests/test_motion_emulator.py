import unittest

from android_motion_emulator import (
    AdvancedSpeedProfile,
    GeoPoint,
    MotionProfile,
    SpeedVariation,
    build_samples_from_options,
    build_web_ui_html,
    parse_options_payload,
    parse_segment_speed_overrides,
    parse_variation_from_payload,
    validate_motion_profile,
)


class MotionEmulatorTests(unittest.TestCase):
    def test_validate_motion_profile_rejects_overlapping_shares(self) -> None:
        with self.assertRaises(ValueError):
            validate_motion_profile(
                MotionProfile(
                    start_curve="ease-in",
                    stop_curve="ease-out",
                    start_share=0.6,
                    stop_share=0.4,
                )
            )

    def test_parse_segment_speed_overrides_converts_kmh_to_mps(self) -> None:
        overrides = parse_segment_speed_overrides(["18", "", 36], segment_count=3)
        self.assertAlmostEqual(overrides[0], 5.0)
        self.assertIsNone(overrides[1])
        self.assertAlmostEqual(overrides[2], 10.0)

    def test_parse_variation_from_payload_returns_none_when_disabled(self) -> None:
        variation = parse_variation_from_payload(
            {
                "variationEnabled": False,
                "variationAmplitudePercent": 0,
                "variationFrequencyHz": 0,
            }
        )
        self.assertIsNone(variation)

    def test_parse_options_payload_builds_advanced_profile(self) -> None:
        payload = {
            "platform": "android",
            "timingMode": "speed",
            "points": [
                {"lat": 37.4219999, "lon": -122.0840575},
                {"lat": 37.4225, "lon": -122.0835},
                {"lat": 37.4232, "lon": -122.0827},
            ],
            "speedKmh": 24,
            "interval": 1,
            "startCurve": "ease-in",
            "stopCurve": "ease-out",
            "startShare": 0.2,
            "stopShare": 0.2,
            "segmentSpeedsKmh": ["", "18"],
            "variationEnabled": True,
            "variationCurve": "sine",
            "variationFrequencyHz": 0.2,
            "variationAmplitudePercent": 20,
        }

        platform, points, duration, interval, adb_path, target_id, altitude, profile, speed_mps, advanced = parse_options_payload(payload)

        self.assertEqual(platform, "android")
        self.assertEqual(len(points), 3)
        self.assertIsNone(duration)
        self.assertEqual(interval, 1)
        self.assertEqual(adb_path, "adb")
        self.assertIsNone(target_id)
        self.assertIsNone(altitude)
        self.assertEqual(profile.start_curve, "ease-in")
        self.assertAlmostEqual(speed_mps, 24 / 3.6)
        self.assertIsNotNone(advanced)
        assert advanced is not None
        self.assertAlmostEqual(advanced.segment_speed_mps[1], 18 / 3.6)
        self.assertIsNotNone(advanced.variation)

    def test_build_samples_from_options_supports_advanced_profile(self) -> None:
        points = [
            GeoPoint(37.4219999, -122.0840575),
            GeoPoint(37.4225, -122.0835),
            GeoPoint(37.4232, -122.0827),
        ]
        profile = MotionProfile(
            start_curve="ease-in",
            stop_curve="ease-out",
            start_share=0.2,
            stop_share=0.2,
        )
        advanced = AdvancedSpeedProfile(
            segment_speed_mps=[None, 18 / 3.6],
            variation=SpeedVariation(
                curve="sine",
                frequency_hz=0.2,
                amplitude_ratio=0.2,
            ),
        )

        route, samples, curve_label, timing = build_samples_from_options(
            points=points,
            duration=60,
            interval=1,
            profile=profile,
            advanced_profile=advanced,
        )

        self.assertGreater(route.total_distance, 0)
        self.assertGreater(len(samples), 2)
        self.assertEqual(samples[-1].route_progress, 1.0)
        self.assertIn("variation=sine", curve_label)
        self.assertEqual(timing.mode, "speed-profile")

    def test_build_web_ui_html_contains_core_controls(self) -> None:
        html = build_web_ui_html()
        self.assertIn("Motion Route Studio", html)
        self.assertIn("advancedSpeedDetails", html)
        self.assertIn("summaryPointsValue", html)
        self.assertIn("fitRouteButton", html)


if __name__ == "__main__":
    unittest.main()
