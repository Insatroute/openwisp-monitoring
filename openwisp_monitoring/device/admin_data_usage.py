"""
Data Usage Dashboard — Admin view + JSON API endpoints.

Follows the DPI Dashboard monkey-patch pattern (dpi_analytics/admin.py).
Registered via monkey-patch onto DeviceAdmin at the bottom of device/admin.py.
"""

import uuid as uuid_mod

from django.http import JsonResponse
from django.template.response import TemplateResponse
from django.urls import path

from openwisp_monitoring.monitoring.services import (
    DataUsageValidationError,
    get_data_usage_payload_for_request,
)


def _format_bytes(b):
    """Human-readable byte string."""
    if not isinstance(b, (int, float)) or b < 0:
        b = 0
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(b) < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} PB"


def _du_payload_or_error(request):
    try:
        return get_data_usage_payload_for_request(request), None
    except DataUsageValidationError as exc:
        return None, JsonResponse(
            {"detail": str(exc), "code": "invalid_period"},
            status=400,
        )


# ---------------------------------------------------------------------------
# DataUsageDashboardAdmin — NOT registered via @admin.register
# Methods are monkey-patched onto DeviceAdmin (see bottom of admin.py).
# ---------------------------------------------------------------------------

class DataUsageDashboardAdmin:
    """Container for data-usage dashboard view + 8 JSON API endpoints."""

    # ---- URL registration --------------------------------------------------

    @staticmethod
    def get_urls(admin_self):
        """Return URL patterns for the dashboard page + API endpoints."""
        wrap = admin_self.admin_site.admin_view
        return [
            path(
                "data-usage-dashboard/",
                wrap(DataUsageDashboardAdmin.dashboard_view),
                name="data_usage_dashboard",
            ),
            path(
                "api/du/summary/",
                wrap(DataUsageDashboardAdmin.api_du_summary),
                name="du_api_summary",
            ),
            path(
                "api/du/top-apps/",
                wrap(DataUsageDashboardAdmin.api_du_top_apps),
                name="du_api_top_apps",
            ),
            path(
                "api/du/top-devices/",
                wrap(DataUsageDashboardAdmin.api_du_top_devices),
                name="du_api_top_devices",
            ),
            path(
                "api/du/device/<str:device_id>/",
                wrap(DataUsageDashboardAdmin.api_du_device_detail),
                name="du_api_device_detail",
            ),
            path(
                "api/du/mobile/",
                wrap(DataUsageDashboardAdmin.api_du_mobile),
                name="du_api_mobile",
            ),
            path(
                "api/du/wan/",
                wrap(DataUsageDashboardAdmin.api_du_wan),
                name="du_api_wan",
            ),
            path(
                "api/du/timeseries/",
                wrap(DataUsageDashboardAdmin.api_du_timeseries),
                name="du_api_timeseries",
            ),
            path(
                "api/du/apps-by-interface/",
                wrap(DataUsageDashboardAdmin.api_du_apps_by_interface),
                name="du_api_apps_by_interface",
            ),
        ]

    # ---- Dashboard page ----------------------------------------------------

    @staticmethod
    def dashboard_view(request):
        """Render the unified Traffic Insights dashboard (legacy URL kept for compatibility)."""
        from django.contrib.admin.sites import site as admin_site

        context = dict(
            admin_site.each_context(request),
            title="Traffic Insights",
        )
        return TemplateResponse(
            request,
            "admin/monitoring/data_usage_dashboard.html",
            context,
        )

    # ---- API: Summary (6 cards) -------------------------------------------

    @staticmethod
    def api_du_summary(request):
        payload, error = _du_payload_or_error(request)
        if error:
            return error
        summary = payload["summary"]
        return JsonResponse(
            {
                "summary": summary,
                "device_count": payload["meta"].get("device_count", 0),
                "total_fmt": _format_bytes(summary["total"]["total"]),
                "cellular_fmt": _format_bytes(summary["cellular"]["total"]),
                "wired_fmt": _format_bytes(summary["wired"]["total"]),
                "wireless_fmt": _format_bytes(summary["wireless"]["total"]),
                "meta": payload["meta"],
                "warnings": payload["warnings"],
            }
        )

    # ---- API: Top 10 apps --------------------------------------------------

    @staticmethod
    def api_du_top_apps(request):
        payload, error = _du_payload_or_error(request)
        if error:
            return error
        return JsonResponse(
            {
                "top_apps": payload["apps"]["top_apps"],
                "all_apps": payload["apps"]["all_apps"],
                "meta": payload["meta"],
                "warnings": payload["warnings"],
            }
        )

    # ---- API: Top 10 devices -----------------------------------------------

    @staticmethod
    def api_du_top_devices(request):
        payload, error = _du_payload_or_error(request)
        if error:
            return error
        devices = payload["devices"]
        return JsonResponse(
            {
                "top_devices": payload["top_devices"],
                "all_devices": devices[:100],
                "total_count": len(devices),
                "meta": payload["meta"],
                "warnings": payload["warnings"],
            }
        )

    # ---- API: Single device detail -----------------------------------------

    @staticmethod
    def api_du_device_detail(request, device_id):
        try:
            uuid_mod.UUID(str(device_id))
        except (ValueError, AttributeError):
            return JsonResponse({"error": "Invalid device ID"}, status=400)
        payload, error = _du_payload_or_error(request)
        if error:
            return error

        device = next((d for d in payload["devices"] if d["device_id"] == str(device_id)), None)
        if not device:
            return JsonResponse({"error": "Device not found"}, status=404)

        device_apps = payload["apps"]["device_apps"].get(str(device_id), [])
        return JsonResponse(
            {
                "device_id": device["device_id"],
                "hostname": device.get("hostname") or device.get("name", ""),
                "interfaces": device.get("interfaces", []),
                "apps": device_apps[:20],
                "meta": payload["meta"],
                "warnings": payload["warnings"],
            }
        )

    # ---- API: Mobile / Cellular analytics ----------------------------------

    @staticmethod
    def api_du_mobile(request):
        payload, error = _du_payload_or_error(request)
        if error:
            return error
        response = dict(payload["mobile"])
        response["meta"] = payload["meta"]
        response["warnings"] = payload["warnings"]
        return JsonResponse(response)

    # ---- API: WAN links ----------------------------------------------------

    @staticmethod
    def api_du_wan(request):
        payload, error = _du_payload_or_error(request)
        if error:
            return error
        rows = payload["wan"]["rows"]
        return JsonResponse(
            {
                "summary": payload["wan"]["summary"],
                "rows": rows,
                "links": rows,  # legacy key
                "meta": payload["meta"],
                "warnings": payload["warnings"],
            }
        )

    # ---- API: Timeseries (stacked area chart data) -------------------------

    @staticmethod
    def api_du_timeseries(request):
        payload, error = _du_payload_or_error(request)
        if error:
            return error
        response = dict(payload["timeseries"])
        response["meta"] = payload["meta"]
        response["warnings"] = payload["warnings"]
        return JsonResponse(response)

    # ---- API: Apps by interface (Sankey data) ------------------------------

    @staticmethod
    def api_du_apps_by_interface(request):
        payload, error = _du_payload_or_error(request)
        if error:
            return error
        response = dict(payload["apps_by_interface"])
        response["meta"] = payload["meta"]
        response["warnings"] = payload["warnings"]
        return JsonResponse(response)
