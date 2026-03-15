from datetime import timedelta

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from openwisp_users.tests.utils import TestOrganizationMixin

from ..services.data_usage import DataUsageValidationError, _parse_window


class TestDataUsageWindowParser(SimpleTestCase):
    def test_valid_period_window(self):
        window = _parse_window("24h", None, None)
        self.assertEqual(window.period, "24h")
        self.assertFalse(window.is_custom)
        self.assertLess(window.start, window.end)

    def test_invalid_period_rejected(self):
        with self.assertRaises(DataUsageValidationError):
            _parse_window("2h", None, None)

    def test_custom_window_requires_both_start_and_end(self):
        with self.assertRaises(DataUsageValidationError):
            _parse_window("24h", timezone.now().isoformat(), None)

    def test_custom_window_over_30_days_rejected(self):
        start = (timezone.now() - timedelta(days=31)).isoformat()
        end = timezone.now().isoformat()
        with self.assertRaises(DataUsageValidationError):
            _parse_window("24h", start, end)

    def test_valid_custom_window(self):
        start = (timezone.now() - timedelta(hours=6)).isoformat()
        end = timezone.now().isoformat()
        window = _parse_window(None, start, end)
        self.assertTrue(window.is_custom)
        self.assertEqual(window.period, "custom")
        self.assertLess(window.start, window.end)


class TestDataUsageEndpointsValidation(TestOrganizationMixin, TestCase):
    invalid_period_paths = [
        "/api/v1/monitoring/data-usage/",
        "/api/v1/monitoring/global-top-apps/",
        "/api/v1/monitoring/global-top-devices/",
        "/api/v1/monitoring/wan-uplinks/",
        "/api/v1/monitoring/mobile-distribution/",
        "/api/v1/monitoring/global-all-apps/",
    ]

    def setUp(self):
        self.admin = self._create_admin()
        self.client.force_login(self.admin)

    def test_invalid_period_returns_400(self):
        for path in self.invalid_period_paths:
            with self.subTest(path=path):
                response = self.client.get(path, {"period": "2h"})
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json().get("code"), "invalid_period")

    def test_data_usage_includes_meta_and_warnings(self):
        response = self.client.get("/api/v1/monitoring/data-usage/")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("meta", payload)
        self.assertIn("warnings", payload)
        self.assertEqual(payload["meta"].get("period"), "24h")
