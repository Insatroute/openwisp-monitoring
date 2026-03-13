"""
Data Usage Dashboard — Admin view + JSON API endpoints.

Follows the DPI Dashboard monkey-patch pattern (dpi_analytics/admin.py).
Registered via monkey-patch onto DeviceAdmin at the bottom of device/admin.py.
"""

import logging
from collections import Counter
from datetime import timedelta

from django.http import JsonResponse
from django.template.response import TemplateResponse
from django.urls import path
from django.utils import timezone
from swapper import load_model

logger = logging.getLogger(__name__)

Device = load_model("config", "Device")
DeviceData = load_model("device_monitoring", "DeviceData")


# ---------------------------------------------------------------------------
# Helpers (copied from views_dashboard.py to avoid import coupling)
# ---------------------------------------------------------------------------

def _add_traffic(bucket, tx_bytes, rx_bytes):
    bucket["sent"] += tx_bytes or 0
    bucket["received"] += rx_bytes or 0
    bucket["total"] = bucket["sent"] + bucket["received"]


def _ipv4_addr(iface):
    ipv4 = next(
        (a for a in iface.get("addresses", []) if a.get("family") == "ipv4"),
        None,
    )
    return ipv4.get("address") if ipv4 else None


def _link_status(iface):
    return "connected" if iface.get("up") else "disconnected"


def _normalize_operator(raw):
    if not raw:
        return "Unknown"
    upper = raw.strip().upper()
    if "JIO" in upper:
        return "Jio"
    if "AIRTEL" in upper:
        return "Airtel"
    if any(k in upper for k in ("VI ", "VODAFONE", "IDEA")) or upper.startswith("VI"):
        return "Vi India"
    if "BSNL" in upper:
        return "BSNL"
    return " ".join(p.capitalize() for p in raw.strip().split())


def _get_org_device_data(user):
    """Return DeviceData queryset scoped to user's organisations."""
    qs = DeviceData.objects.all()
    if user.is_superuser:
        return qs
    user_orgs = user.organizations.all()
    return qs.filter(organization__in=user_orgs)


def _format_bytes(b):
    """Human-readable byte string."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(b) < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} PB"


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
        """Render the standalone Data Usage Analytics dashboard."""
        from django.contrib.admin.sites import site as admin_site

        qs = _get_org_device_data(request.user)
        total_devices = qs.count()

        # Quick summary for server-rendered cards
        summary = {
            "total": {"sent": 0, "received": 0, "total": 0},
            "cellular": {"sent": 0, "received": 0, "total": 0},
            "wired": {"sent": 0, "received": 0, "total": 0},
            "wireless": {"sent": 0, "received": 0, "total": 0},
        }

        for dd in qs:
            data = getattr(dd, "data_user_friendly", None) or {}
            for iface in data.get("interfaces", []) or []:
                stats = iface.get("statistics") or {}
                tx = stats.get("tx_bytes") or 0
                rx = stats.get("rx_bytes") or 0
                itype = iface.get("type")
                if itype == "mobile":
                    _add_traffic(summary["cellular"], tx, rx)
                elif itype == "ethernet" and iface.get("is_wan") is True:
                    _add_traffic(summary["wired"], tx, rx)
                elif itype in ("wifi", "wireless"):
                    _add_traffic(summary["wireless"], tx, rx)

        for key in ("cellular", "wired", "wireless"):
            _add_traffic(summary["total"], summary[key]["sent"], summary[key]["received"])

        context = dict(
            admin_site.each_context(request),
            title="Data Usage Analytics",
            total_devices=total_devices,
            summary=summary,
            total_fmt=_format_bytes(summary["total"]["total"]),
            cellular_fmt=_format_bytes(summary["cellular"]["total"]),
            wired_fmt=_format_bytes(summary["wired"]["total"]),
            wireless_fmt=_format_bytes(summary["wireless"]["total"]),
        )
        return TemplateResponse(
            request,
            "admin/monitoring/data_usage_dashboard.html",
            context,
        )

    # ---- API: Summary (6 cards) -------------------------------------------

    @staticmethod
    def api_du_summary(request):
        qs = _get_org_device_data(request.user)
        summary = {
            "total": {"sent": 0, "received": 0, "total": 0},
            "cellular": {"sent": 0, "received": 0, "total": 0},
            "wired": {"sent": 0, "received": 0, "total": 0},
            "wireless": {"sent": 0, "received": 0, "total": 0},
        }
        device_count = 0

        for dd in qs:
            device_count += 1
            data = getattr(dd, "data_user_friendly", None) or {}
            for iface in data.get("interfaces", []) or []:
                stats = iface.get("statistics") or {}
                tx = stats.get("tx_bytes") or 0
                rx = stats.get("rx_bytes") or 0
                itype = iface.get("type")
                if itype == "mobile":
                    _add_traffic(summary["cellular"], tx, rx)
                elif itype == "ethernet" and iface.get("is_wan") is True:
                    _add_traffic(summary["wired"], tx, rx)
                elif itype in ("wifi", "wireless"):
                    _add_traffic(summary["wireless"], tx, rx)

        for key in ("cellular", "wired", "wireless"):
            _add_traffic(summary["total"], summary[key]["sent"], summary[key]["received"])

        return JsonResponse({
            "summary": summary,
            "device_count": device_count,
            "total_fmt": _format_bytes(summary["total"]["total"]),
            "cellular_fmt": _format_bytes(summary["cellular"]["total"]),
            "wired_fmt": _format_bytes(summary["wired"]["total"]),
            "wireless_fmt": _format_bytes(summary["wireless"]["total"]),
        })

    # ---- API: Top 10 apps --------------------------------------------------

    @staticmethod
    def api_du_top_apps(request):
        qs = _get_org_device_data(request.user)
        app_counter = Counter()

        for dd in qs:
            data = getattr(dd, "data_user_friendly", None) or {}
            apps = (
                data.get("realtimemonitor", {})
                .get("traffic", {})
                .get("dpi_summery_v2", {})
                .get("applications", [])
            )
            for app in apps:
                app_id = app.get("id") or ""
                if app_id in ("netify.nethserver", "netify.snort", "netify.netify"):
                    continue
                label = app.get("label")
                traffic = app.get("traffic", 0)
                if label:
                    app_counter[label] += traffic

        top_apps = [
            {"label": label.capitalize(), "traffic": traffic}
            for label, traffic in app_counter.most_common(10)
        ]
        all_apps = [
            {"label": label.capitalize(), "traffic": traffic}
            for label, traffic in app_counter.most_common()
        ]
        return JsonResponse({"top_apps": top_apps, "all_apps": all_apps})

    # ---- API: Top 10 devices -----------------------------------------------

    @staticmethod
    def api_du_top_devices(request):
        qs = _get_org_device_data(request.user)
        devices = []

        for dd in qs:
            data = getattr(dd, "data_user_friendly", None) or {}
            general = data.get("general") or {}
            interfaces = data.get("interfaces") or []

            total_rx = total_tx = 0
            iface_breakdown = []
            for iface in interfaces:
                stats = iface.get("statistics") or {}
                rx = stats.get("rx_bytes") or 0
                tx = stats.get("tx_bytes") or 0
                total_rx += rx
                total_tx += tx
                if rx + tx > 0:
                    iface_breakdown.append({
                        "name": iface.get("name", "unknown"),
                        "type": iface.get("type", "unknown"),
                        "rx": rx,
                        "tx": tx,
                        "total": rx + tx,
                    })

            name = (
                general.get("hostname")
                or getattr(dd, "name", "")
                or str(dd.pk)
            )
            devices.append({
                "device_id": str(dd.pk),
                "name": name,
                "total_bytes": total_rx + total_tx,
                "rx_bytes": total_rx,
                "tx_bytes": total_tx,
                "interfaces": sorted(
                    iface_breakdown, key=lambda x: x["total"], reverse=True
                ),
            })

        devices.sort(key=lambda d: d["total_bytes"], reverse=True)
        return JsonResponse({
            "top_devices": devices[:10],
            "all_devices": devices,
        })

    # ---- API: Single device detail -----------------------------------------

    @staticmethod
    def api_du_device_detail(request, device_id):
        try:
            dd = _get_org_device_data(request.user).get(pk=device_id)
        except DeviceData.DoesNotExist:
            return JsonResponse({"error": "Device not found"}, status=404)

        data = getattr(dd, "data_user_friendly", None) or {}
        general = data.get("general") or {}
        interfaces = data.get("interfaces") or []

        iface_list = []
        for iface in interfaces:
            stats = iface.get("statistics") or {}
            rx = stats.get("rx_bytes") or 0
            tx = stats.get("tx_bytes") or 0
            iface_list.append({
                "name": iface.get("name", "unknown"),
                "type": iface.get("type", "unknown"),
                "up": iface.get("up", False),
                "ip": _ipv4_addr(iface),
                "rx_bytes": rx,
                "tx_bytes": tx,
                "total": rx + tx,
            })

        # DPI apps for this device
        apps = (
            data.get("realtimemonitor", {})
            .get("traffic", {})
            .get("dpi_summery_v2", {})
            .get("applications", [])
        )
        app_list = []
        for app in apps:
            app_id = app.get("id") or ""
            if app_id in ("netify.nethserver", "netify.snort", "netify.netify"):
                continue
            label = app.get("label")
            traffic = app.get("traffic", 0)
            if label:
                app_list.append({"label": label.capitalize(), "traffic": traffic})
        app_list.sort(key=lambda a: a["traffic"], reverse=True)

        return JsonResponse({
            "device_id": str(dd.pk),
            "hostname": general.get("hostname", ""),
            "interfaces": sorted(iface_list, key=lambda x: x["total"], reverse=True),
            "apps": app_list[:20],
        })

    # ---- API: Mobile / Cellular analytics ----------------------------------

    @staticmethod
    def api_du_mobile(request):
        qs = _get_org_device_data(request.user)
        carrier_counter = Counter()
        network_counter = Counter()
        total_modems = 0
        modem_details = []

        for dd in qs:
            data = getattr(dd, "data_user_friendly", None) or {}
            general = data.get("general") or {}
            hostname = general.get("hostname") or str(dd.pk)
            interfaces = data.get("interfaces") or []

            for iface in interfaces:
                if iface.get("type") != "mobile":
                    continue
                mobile = iface.get("mobile") or {}
                total_modems += 1

                raw_op = mobile.get("operator_name") or "Unknown"
                operator = _normalize_operator(raw_op)
                carrier_counter[operator] += 1

                signal = mobile.get("signal") or {}
                if "5g" in signal:
                    net_type = "5G"
                elif "lte" in signal:
                    net_type = "4G LTE"
                elif "3g" in signal:
                    net_type = "3G"
                else:
                    net_type = "Unknown"
                network_counter[net_type] += 1

                stats = iface.get("statistics") or {}
                modem_details.append({
                    "hostname": hostname,
                    "device_id": str(dd.pk),
                    "name": iface.get("name", "modem"),
                    "carrier": operator,
                    "network": net_type,
                    "rx_bytes": stats.get("rx_bytes") or 0,
                    "tx_bytes": stats.get("tx_bytes") or 0,
                })

        return JsonResponse({
            "carrier": {
                "labels": list(carrier_counter.keys()),
                "data": list(carrier_counter.values()),
            },
            "network": {
                "labels": list(network_counter.keys()),
                "data": list(network_counter.values()),
            },
            "total_modems": total_modems,
            "modems": modem_details,
        })

    # ---- API: WAN links ----------------------------------------------------

    @staticmethod
    def api_du_wan(request):
        qs = _get_org_device_data(request.user)
        summary = {"total": 0, "connected": 0, "disconnected": 0}
        rows = []

        for dd in qs:
            data = getattr(dd, "data_user_friendly", None) or {}
            general = data.get("general") or {}
            hostname = general.get("hostname") or str(dd.pk)

            for iface in data.get("interfaces") or []:
                itype = (iface.get("type") or "").lower()
                is_wan_eth = itype == "ethernet" and iface.get("is_wan") is True
                is_mobile = itype == "mobile"
                if not (is_wan_eth or is_mobile):
                    continue

                status = _link_status(iface)
                summary["total"] += 1
                summary[status] += 1

                stats = iface.get("statistics") or {}
                rows.append({
                    "device_id": str(dd.pk),
                    "hostname": hostname,
                    "interface": iface.get("name", ""),
                    "type": "cellular" if is_mobile else "ethernet",
                    "ip": _ipv4_addr(iface),
                    "status": status,
                    "rx_bytes": stats.get("rx_bytes") or 0,
                    "tx_bytes": stats.get("tx_bytes") or 0,
                })

        return JsonResponse({"summary": summary, "links": rows})

    # ---- API: Timeseries (stacked area chart data) -------------------------

    @staticmethod
    def api_du_timeseries(request):
        """
        Return per-interface-type traffic suitable for a stacked area chart.
        Groups by device and classifies each interface into cellular/wired/wifi.
        """
        qs = _get_org_device_data(request.user)
        buckets = {"cellular": 0, "wired": 0, "wireless": 0}

        for dd in qs:
            data = getattr(dd, "data_user_friendly", None) or {}
            for iface in data.get("interfaces") or []:
                stats = iface.get("statistics") or {}
                total = (stats.get("rx_bytes") or 0) + (stats.get("tx_bytes") or 0)
                itype = iface.get("type")
                if itype == "mobile":
                    buckets["cellular"] += total
                elif itype == "ethernet" and iface.get("is_wan") is True:
                    buckets["wired"] += total
                elif itype in ("wifi", "wireless"):
                    buckets["wireless"] += total

        # Try to get DpiAppTraffic hourly data if available
        hourly_data = []
        try:
            from dpi_analytics.models import DpiAppTraffic
            cutoff = timezone.now() - timedelta(hours=24)
            from django.db.models import Sum
            from django.db.models.functions import TruncHour

            hourly = (
                DpiAppTraffic.objects
                .filter(period_start__gte=cutoff)
                .annotate(hour=TruncHour("period_start"))
                .values("hour")
                .annotate(
                    download=Sum("download_bytes"),
                    upload=Sum("upload_bytes"),
                )
                .order_by("hour")
            )
            for row in hourly:
                hourly_data.append({
                    "time": row["hour"].isoformat(),
                    "download": row["download"] or 0,
                    "upload": row["upload"] or 0,
                })
        except Exception:
            pass

        return JsonResponse({
            "by_type": buckets,
            "hourly": hourly_data,
        })

    # ---- API: Apps by interface (Sankey data) ------------------------------

    @staticmethod
    def api_du_apps_by_interface(request):
        """
        Map apps to WAN interfaces for a Sankey-style visualization.
        Returns links: [{source: "YouTube", target: "wan1", value: bytes}]
        """
        qs = _get_org_device_data(request.user)
        # Aggregate interface traffic
        iface_traffic = Counter()
        app_traffic = Counter()

        for dd in qs:
            data = getattr(dd, "data_user_friendly", None) or {}

            # WAN interfaces
            for iface in data.get("interfaces") or []:
                itype = (iface.get("type") or "").lower()
                is_wan = (itype == "ethernet" and iface.get("is_wan")) or itype == "mobile"
                if not is_wan:
                    continue
                stats = iface.get("statistics") or {}
                total = (stats.get("rx_bytes") or 0) + (stats.get("tx_bytes") or 0)
                name = iface.get("name", "unknown")
                iface_traffic[name] += total

            # Apps
            apps = (
                data.get("realtimemonitor", {})
                .get("traffic", {})
                .get("dpi_summery_v2", {})
                .get("applications", [])
            )
            for app in apps:
                app_id = app.get("id") or ""
                if app_id in ("netify.nethserver", "netify.snort", "netify.netify"):
                    continue
                label = app.get("label")
                traffic = app.get("traffic", 0)
                if label:
                    app_traffic[label.capitalize()] += traffic

        # Build Sankey links — distribute apps proportionally across interfaces
        total_iface = sum(iface_traffic.values()) or 1
        top_apps = app_traffic.most_common(10)
        top_ifaces = iface_traffic.most_common(10)
        links = []

        for app_label, app_bytes in top_apps:
            for iface_name, iface_bytes in top_ifaces:
                proportion = iface_bytes / total_iface
                link_value = int(app_bytes * proportion)
                if link_value > 0:
                    links.append({
                        "source": app_label,
                        "target": iface_name,
                        "value": link_value,
                    })

        return JsonResponse({
            "apps": [{"label": l, "traffic": t} for l, t in top_apps],
            "interfaces": [{"name": n, "traffic": t} for n, t in top_ifaces],
            "links": links,
        })
