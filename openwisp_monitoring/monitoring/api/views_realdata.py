import logging
import threading
from datetime import datetime, timedelta

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from django.shortcuts import get_object_or_404
from swapper import load_model

logger = logging.getLogger(__name__)

Device = load_model("config", "Device")
DeviceData = load_model("device_monitoring", "DeviceData")

# ---------------------------------------------------------------
# InfluxDB helpers
# ---------------------------------------------------------------
_thread_local = threading.local()


def _get_influx_client():
    """Thread-safe InfluxDB client (one connection per thread)."""
    client = getattr(_thread_local, 'influx_client', None)
    if client is None:
        try:
            from influxdb import InfluxDBClient
            from django.conf import settings
            client = InfluxDBClient(
                host=getattr(settings, 'INFLUXDB_HOST', 'localhost'),
                port=getattr(settings, 'INFLUXDB_PORT', 8086),
                username=getattr(settings, 'INFLUXDB_USER', ''),
                password=getattr(settings, 'INFLUXDB_PASSWORD', ''),
                database=getattr(settings, 'INFLUXDB_DATABASE', 'openwisp2'),
            )
            _thread_local.influx_client = client
        except ImportError:
            return None
    return client


def _influx_query(query_str):
    client = _get_influx_client()
    if client is None:
        return None
    try:
        return client.query(query_str)
    except Exception as e:
        _thread_local.influx_client = None
        logger.warning("InfluxDB query failed: %s", e)
        return None


def _influx_points(query_str):
    result = _influx_query(query_str)
    if result is None:
        return []
    return list(result.get_points())


def _influx_grouped_points(query_str):
    result = _influx_query(query_str)
    if result is None:
        return []
    rows = []
    for (measurement, tags), points in result.items():
        for point in points:
            row = dict(tags) if tags else {}
            row.update(point)
            rows.append(row)
    return rows


# ---------------------------------------------------------------
# Date param helpers
# ---------------------------------------------------------------
def _parse_date_params(request):
    """Parse from/to date query params. Returns (from_str, to_str) or (None, None)."""
    from_date = request.GET.get('from')
    to_date = request.GET.get('to')
    if from_date and to_date:
        try:
            datetime.strptime(from_date, '%Y-%m-%d')
            datetime.strptime(to_date, '%Y-%m-%d')
            return from_date, to_date
        except ValueError:
            pass
    return None, None


def _is_today(from_date, to_date):
    today = datetime.now().strftime('%Y-%m-%d')
    return from_date == today and to_date == today


def _time_filter(from_date, to_date):
    to_next = (datetime.strptime(to_date, '%Y-%m-%d') + timedelta(days=1)).strftime('%Y-%m-%d')
    return f"time >= '{from_date}T00:00:00Z' AND time < '{to_next}T00:00:00Z'"


# ---------------------------------------------------------------
# InfluxDB query builders
# ---------------------------------------------------------------
def _build_traffic_from_influx(device_id, from_date, to_date):
    """Build traffic summary response from InfluxDB dpi_app_traffic for a date range."""
    tf = _time_filter(from_date, to_date)
    where = f"WHERE object_id = '{device_id}' AND {tf}"

    # 1. Total traffic
    pts = _influx_points(
        f'SELECT SUM("rx_bytes") AS rx, SUM("tx_bytes") AS tx FROM "dpi_app_traffic" {where}'
    )
    total = (int(pts[0].get('rx') or 0) + int(pts[0].get('tx') or 0)) if pts else 0

    # 2. Hourly traffic (aggregate by hour-of-day across all days in range)
    h_pts = _influx_points(
        f'SELECT SUM("rx_bytes") AS rx, SUM("tx_bytes") AS tx '
        f'FROM "dpi_app_traffic" {where} GROUP BY time(1h)'
    )
    hourly = {}
    for p in h_pts:
        if p.get('time'):
            try:
                dt = datetime.fromisoformat(p['time'].replace('Z', '+00:00'))
                h = str(dt.hour).zfill(2)
                hourly[h] = hourly.get(h, 0) + int(p.get('rx') or 0) + int(p.get('tx') or 0)
            except Exception:
                pass
    hourly_traffic = [
        {"id": str(i).zfill(2), "traffic": hourly.get(str(i).zfill(2), 0)}
        for i in range(24)
    ]

    # 3. Applications (GROUP BY app_name)
    a_pts = _influx_grouped_points(
        f'SELECT SUM("rx_bytes") AS rx, SUM("tx_bytes") AS tx '
        f'FROM "dpi_app_traffic" {where} GROUP BY "app_name"'
    )
    apps = []
    for ap in a_pts:
        name = ap.get('app_name', 'unknown')
        traffic = int(ap.get('rx') or 0) + int(ap.get('tx') or 0)
        if traffic > 0:
            parts = name.split(".")
            label = ".".join(parts[1:]).replace("-", " ").title() if len(parts) > 1 else name.title()
            apps.append({"id": name, "label": label, "traffic": traffic})
    apps.sort(key=lambda x: x['traffic'], reverse=True)

    return {
        "dpi_summery_v2": {
            "hourly_traffic": hourly_traffic,
            "total_traffic": total,
            "applications": apps,
            "remote_hosts": [],
            "protocols": [],
            "clients": []
        },
        "dpi_client_data": []
    }


def _build_rt_traffic_from_influx(device_id, from_date, to_date):
    """Build real-time traffic summary from InfluxDB for a date range."""
    tf = _time_filter(from_date, to_date)
    where = f"WHERE object_id = '{device_id}' AND {tf}"

    a_pts = _influx_grouped_points(
        f'SELECT SUM("rx_bytes") AS rx, SUM("tx_bytes") AS tx '
        f'FROM "dpi_app_traffic" {where} GROUP BY "app_name"'
    )
    top_apps = []
    for ap in a_pts:
        name = ap.get('app_name', 'unknown')
        traffic = int(ap.get('rx') or 0) + int(ap.get('tx') or 0)
        if traffic > 0:
            parts = name.split(".")
            label = ".".join(parts[2:]).capitalize() if len(parts) > 2 else parts[-1].capitalize()
            top_apps.append({"name": label, "value": traffic})
    top_apps.sort(key=lambda x: x['value'], reverse=True)

    return {
        "top_protocols": [],
        "top_hosts": [],
        "top_apps": top_apps[:20]
    }


def _build_security_from_influx(device_id, from_date, to_date):
    """
    Security data is not stored in InfluxDB historically.
    Return empty structure so the frontend shows 'No data' gracefully.
    """
    return {
        "blocklist": {
            "malware_count": 0,
            "malware_by_hour": [],
            "first_seen": 0
        },
        "brute_force_attack": {
            "attack_count": 0,
            "attack_by_hour": [],
            "first_seen": 0
        }
    }


# ---------------------------------------------------------------
# Existing data fetch helpers (unchanged)
# ---------------------------------------------------------------
def fetch_device_data(device):
    """Fetch device data from the associated device configuration."""
    try:
        device_data = DeviceData.objects.get(config=device.config)
        if not device_data or not isinstance(device_data.data_user_friendly, dict):
            return {}
        return device_data.data_user_friendly
    except DeviceData.DoesNotExist:
        return {}


def fetch_cellular_data(device):
    """Fetch cellular data from the associated device configuration."""
    try:
        device_data = DeviceData.objects.get(config=device.config)
        if not device_data or not isinstance(device_data.data_user_friendly, dict):
            return {"cellular": {}}
        cellular = device_data.data_user_friendly.get("cellular", {})
        return {"cellular": cellular}
    except DeviceData.DoesNotExist:
        return {"cellular": {}}


def fetch_device_info(device):
    """Fetch device information from the associated device configuration."""
    try:
        device_data = DeviceData.objects.get(config=device.config)
        if not device_data or not isinstance(device_data.data_user_friendly, dict):
            return {"device": {}}
        device_info = device_data.data_user_friendly.get("device", {})
        return {"device": device_info}
    except DeviceData.DoesNotExist:
        return {"device": {}}


def fetch_device_monitoring_data(device):
    """Fetch device monitoring data."""
    try:
        device_data = DeviceData.objects.get(config=device.config)
        if not device_data or not isinstance(device_data.data_user_friendly, dict):
            return {"traffic": {}, "security": {}, "real_time_traffic": {}, "wan_uplink": {}, "cellular": {}}
        realtime = device_data.data_user_friendly.get("realtimemonitor", {})
        cellular = device_data.data_user_friendly.get("cellular", {})
        traffic = realtime.get("traffic", {})
        security = realtime.get("security", {})
        real_time_traffic = realtime.get("real_time_traffic", {})
        wan_uplink = realtime.get("wan_uplink", {})
        return {
            "traffic": traffic,
            "security": security,
            "real_time_traffic": real_time_traffic,
            "wan_uplink": wan_uplink,
            "cellular": cellular,
        }
    except DeviceData.DoesNotExist:
        return {"traffic": {}, "security": {}, "real_time_traffic": {}, "wan_uplink": {}, "cellular": {}}


# ---------------------------------------------------------------
# API Views
# ---------------------------------------------------------------
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def traffic_summary_data(request, device_id: str):
    device = get_object_or_404(Device, pk=device_id)
    from_date, to_date = _parse_date_params(request)

    if from_date and to_date and not _is_today(from_date, to_date):
        return Response(_build_traffic_from_influx(str(device_id), from_date, to_date))

    # Default: real-time snapshot (richer data with hosts, protocols, clients)
    data = fetch_device_monitoring_data(device)
    traffic = data.get("traffic", {})
    return Response({
        "dpi_summery_v2": traffic.get("dpi_summery_v2", {}),
        "dpi_client_data": traffic.get("dpi_client_data", []),
    })


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def security_summary_data(request, device_id: str):
    device = get_object_or_404(Device, pk=device_id)
    from_date, to_date = _parse_date_params(request)

    if from_date and to_date and not _is_today(from_date, to_date):
        return Response(_build_security_from_influx(str(device_id), from_date, to_date))

    # Default: real-time snapshot
    data = fetch_device_monitoring_data(device)
    security = data.get("security", {})
    return Response({
        "blocklist": security.get("blocklist", {}),
        "brute_force_attack": security.get("brute_force_attack", {}),
    })


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def real_time_traffic_summary_data(request, device_id: str):
    device = get_object_or_404(Device, pk=device_id)
    from_date, to_date = _parse_date_params(request)

    if from_date and to_date and not _is_today(from_date, to_date):
        return Response(_build_rt_traffic_from_influx(str(device_id), from_date, to_date))

    # Default: real-time snapshot
    data = fetch_device_monitoring_data(device)
    traffic_data = data.get("real_time_traffic", {}).get("data", {}).get("talkers", {})

    top_protocols = traffic_data.get("top_protocols", [])
    top_hosts = traffic_data.get("top_hosts", [])
    top_apps = traffic_data.get("top_apps", [])

    for app in top_apps:
        parts = app["name"].split(".")
        if len(parts) > 2:
            app_name = ".".join(parts[2:])
        else:
            app_name = parts[-1]
        app["name"] = app_name.capitalize()

    return Response({
        "top_protocols": top_protocols,
        "top_hosts": top_hosts,
        "top_apps": top_apps,
    })


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def wan_uplink_summary_data(request, device_id: str):
    device = get_object_or_404(Device, pk=device_id)
    data = fetch_device_monitoring_data(device)
    wan_uplink_data = data.get("wan_uplink", {})
    return Response({"wan_uplink": wan_uplink_data})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def cellular_summary_data(request, device_id: str):
    device = get_object_or_404(Device, pk=device_id)
    data = fetch_cellular_data(device)
    cellular_data = data.get("cellular", {})
    return Response({"cellular": cellular_data})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def device_info_summary_data(request, device_id: str):
    device = get_object_or_404(Device, pk=device_id)
    data = fetch_device_info(device)
    device_info = data.get("device", {}).get("device_info", {})
    return Response({"device_info": device_info})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def interfaces_summary_data(request, device_id: str):
    device = get_object_or_404(Device, pk=device_id)
    data = fetch_device_data(device)
    interfaces = data.get("interfaces", [])
    return Response({"interfaces": interfaces, "count": len(interfaces)})
