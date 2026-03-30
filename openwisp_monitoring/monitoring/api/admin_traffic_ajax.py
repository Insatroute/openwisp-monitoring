"""
Admin-only AJAX views for date-filtered traffic data.
Queries the "short".device_data measurement in InfluxDB which stores
complete device monitoring JSON snapshots (including full DPI breakdown).
"""
import json
import logging
import threading
from datetime import datetime, timedelta

from django.views.decorators.csrf import csrf_exempt
from django.http import JsonResponse
from django.views.decorators.http import require_GET
from swapper import load_model

logger = logging.getLogger(__name__)

_thread_local = threading.local()


def _get_influx_client():
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


def _get_device_snapshot(device_id, from_date, to_date):
    """
    Get the last device_data snapshot for each day in the range.
    Returns the DPI traffic data merged across days.
    """
    client = _get_influx_client()
    if client is None:
        return None

    to_next = (datetime.strptime(to_date, '%Y-%m-%d') + timedelta(days=1)).strftime('%Y-%m-%d')
    query = (
        f'SELECT "data" FROM "short".device_data '
        f"WHERE pk='{device_id}' "
        f"AND time >= '{from_date}T00:00:00Z' AND time < '{to_next}T00:00:00Z' "
        f"ORDER BY time DESC LIMIT 1"
    )

    try:
        result = client.query(query)
        points = list(result.get_points())
        if not points:
            return None
        data_str = points[0].get('data', '')
        if not data_str:
            return None
        return json.loads(data_str)
    except Exception as e:
        _thread_local.influx_client = None
        logger.warning("device_data query failed: %s", e)
        return None


def _get_daily_dpi_snapshots(device_id, from_date, to_date):
    """
    Get the last DPI snapshot for each day in the date range.
    Returns merged DPI data across all days.
    """
    client = _get_influx_client()
    if client is None:
        return None

    # For each day in range, get the last snapshot
    start = datetime.strptime(from_date, '%Y-%m-%d')
    end = datetime.strptime(to_date, '%Y-%m-%d')

    all_apps = {}
    all_hosts = {}
    all_protocols = {}
    all_clients = {}
    all_hourly = {}
    daily_traffic = {}  # date -> total bytes
    total_traffic = 0
    all_dpi_client_data = []
    security_data = {}

    current = start
    while current <= end:
        day_str = current.strftime('%Y-%m-%d')
        next_day = (current + timedelta(days=1)).strftime('%Y-%m-%d')

        query = (
            f'SELECT "data" FROM "short".device_data '
            f"WHERE pk='{device_id}' "
            f"AND time >= '{day_str}T00:00:00Z' AND time < '{next_day}T00:00:00Z' "
            f"ORDER BY time DESC LIMIT 1"
        )

        try:
            result = client.query(query)
            points = list(result.get_points())
            if points:
                data_str = points[0].get('data', '')
                if data_str:
                    device_json = json.loads(data_str)
                    rt = device_json.get('realtimemonitor', {})
                    traffic = rt.get('traffic', {})
                    dpi = traffic.get('dpi_summery_v2', {})

                    # Merge applications
                    for app in dpi.get('applications', []):
                        aid = app.get('id', '')
                        at = int(app.get('traffic', 0))
                        if aid in all_apps:
                            all_apps[aid]['traffic'] += at
                        else:
                            all_apps[aid] = {'id': aid, 'label': app.get('label', aid), 'traffic': at}

                    # Merge hosts
                    for host in dpi.get('remote_hosts', []):
                        hid = host.get('id', '')
                        ht = int(host.get('traffic', 0))
                        if hid in all_hosts:
                            all_hosts[hid]['traffic'] += ht
                        else:
                            all_hosts[hid] = {'id': hid, 'traffic': ht}

                    # Merge protocols
                    for proto in dpi.get('protocols', []):
                        pid = proto.get('id', '')
                        pt = int(proto.get('traffic', 0))
                        if pid in all_protocols:
                            all_protocols[pid]['traffic'] += pt
                        else:
                            all_protocols[pid] = {'id': pid, 'label': proto.get('label', pid), 'traffic': pt}

                    # Merge clients
                    for cl in dpi.get('clients', []):
                        cid = cl.get('id', cl.get('label', ''))
                        ct = int(cl.get('traffic', 0))
                        if cid in all_clients:
                            all_clients[cid]['traffic'] += ct
                        else:
                            all_clients[cid] = {'id': cid, 'label': cl.get('label', cid), 'traffic': ct}

                    # Merge hourly (aggregate by hour across days)
                    for h in dpi.get('hourly_traffic', []):
                        hid = h.get('id', '')
                        ht = int(h.get('traffic', 0))
                        all_hourly[hid] = all_hourly.get(hid, 0) + ht

                    day_total = int(dpi.get('total_traffic', 0))
                    total_traffic += day_total
                    daily_traffic[day_str] = daily_traffic.get(day_str, 0) + day_total
                    all_dpi_client_data.extend(traffic.get('dpi_client_data', []))

                    # Security (use latest day's data)
                    sec = rt.get('security', {})
                    if sec:
                        security_data = sec
        except Exception as e:
            logger.warning("Day query failed for %s: %s", day_str, e)

        current += timedelta(days=1)

    # Sort by traffic descending
    apps_list = sorted(all_apps.values(), key=lambda x: x['traffic'], reverse=True)
    total_traffic = sum(a['traffic'] for a in apps_list)
    hosts_list = sorted(all_hosts.values(), key=lambda x: x['traffic'], reverse=True)
    protos_list = sorted(all_protocols.values(), key=lambda x: x['traffic'], reverse=True)
    clients_list = sorted(all_clients.values(), key=lambda x: x['traffic'], reverse=True)
    hourly_list = [{"id": str(i).zfill(2), "traffic": all_hourly.get(str(i).zfill(2), 0)} for i in range(24)]

    # Build sorted daily traffic list
    daily_list = [{"date": d, "traffic": daily_traffic[d]} for d in sorted(daily_traffic.keys())]

    return {
        "traffic": {
            "dpi_summery_v2": {
                "hourly_traffic": hourly_list,
                "daily_traffic": daily_list,
                "total_traffic": total_traffic,
                "applications": apps_list,
                "remote_hosts": hosts_list,
                "protocols": protos_list,
                "clients": clients_list,
            },
            "dpi_client_data": all_dpi_client_data,
        },
        "security": security_data,
    }


@csrf_exempt
@require_GET
def admin_traffic_ajax(request, device_id):
    """Return traffic data for a date range from device_data snapshots."""
    if not request.user.is_authenticated or not request.user.is_staff:
        return JsonResponse({"error": "Not authenticated"}, status=403)

    from_date = request.GET.get('from', '')
    to_date = request.GET.get('to', '')
    if not from_date or not to_date:
        return JsonResponse({"error": "from and to params required"}, status=400)
    try:
        datetime.strptime(from_date, '%Y-%m-%d')
        datetime.strptime(to_date, '%Y-%m-%d')
    except ValueError:
        return JsonResponse({"error": "Invalid date format"}, status=400)

    merged = _get_daily_dpi_snapshots(device_id, from_date, to_date)
    if merged is None:
        return JsonResponse({"dpi_summery_v2": {}, "dpi_client_data": []})

    traffic = merged.get('traffic', {})
    return JsonResponse({
        "dpi_summery_v2": traffic.get('dpi_summery_v2', {}),
        "dpi_client_data": traffic.get('dpi_client_data', []),
    })


@csrf_exempt
@require_GET
def admin_rt_traffic_ajax(request, device_id):
    """Return real-time traffic data for a date range."""
    if not request.user.is_authenticated or not request.user.is_staff:
        return JsonResponse({"error": "Not authenticated"}, status=403)

    from_date = request.GET.get('from', '')
    to_date = request.GET.get('to', '')
    if not from_date or not to_date:
        return JsonResponse({"error": "from and to params required"}, status=400)
    try:
        datetime.strptime(from_date, '%Y-%m-%d')
        datetime.strptime(to_date, '%Y-%m-%d')
    except ValueError:
        return JsonResponse({"error": "Invalid date format"}, status=400)

    # Get snapshot - for RT traffic, get the last snapshot in range
    snapshot = _get_device_snapshot(device_id, from_date, to_date)
    if snapshot is None:
        return JsonResponse({"top_protocols": [], "top_hosts": [], "top_apps": []})

    talkers = snapshot.get('realtimemonitor', {}).get('real_time_traffic', {}).get('data', {}).get('talkers', {})
    top_apps = talkers.get('top_apps', [])
    top_protocols = talkers.get('top_protocols', [])
    top_hosts = talkers.get('top_hosts', [])

    for app in top_apps:
        parts = app.get("name", "").split(".")
        app["name"] = ".".join(parts[2:]).capitalize() if len(parts) > 2 else parts[-1].capitalize()

    return JsonResponse({
        "top_protocols": top_protocols,
        "top_hosts": top_hosts,
        "top_apps": top_apps,
    })


@csrf_exempt
@require_GET
def admin_security_ajax(request, device_id):
    """Return security data for a date range."""
    if not request.user.is_authenticated or not request.user.is_staff:
        return JsonResponse({"error": "Not authenticated"}, status=403)

    from_date = request.GET.get('from', '')
    to_date = request.GET.get('to', '')
    if not from_date or not to_date:
        return JsonResponse({"error": "from and to params required"}, status=400)
    try:
        datetime.strptime(from_date, '%Y-%m-%d')
        datetime.strptime(to_date, '%Y-%m-%d')
    except ValueError:
        return JsonResponse({"error": "Invalid date format"}, status=400)

    merged = _get_daily_dpi_snapshots(device_id, from_date, to_date)
    if merged is None:
        return JsonResponse({
            "blocklist": {"malware_count": 0, "malware_by_hour": [], "first_seen": 0},
            "brute_force_attack": {"attack_count": 0, "attack_by_hour": [], "first_seen": 0}
        })

    security = merged.get('security', {})
    return JsonResponse({
        "blocklist": security.get('blocklist', {"malware_count": 0, "malware_by_hour": [], "first_seen": 0}),
        "brute_force_attack": security.get('brute_force_attack', {"attack_count": 0, "attack_by_hour": [], "first_seen": 0})
    })
