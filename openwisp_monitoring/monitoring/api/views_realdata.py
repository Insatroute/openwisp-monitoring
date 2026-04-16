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


_VALID_RANGES = {"30m", "1h", "6h", "24h"}


def _build_wan_traffic_from_influx(device_id, time_range):
    """Query InfluxDB traffic measurement for WAN distribution over a time range."""
    rows = _influx_grouped_points(
        "SELECT SUM(rx_bytes) AS rx_bytes, SUM(tx_bytes) AS tx_bytes "
        "FROM traffic "
        f"WHERE object_id = '{device_id}' "
        f"AND time > now() - {time_range} "
        "GROUP BY ifname"
    )
    wan_traffic = {}
    for row in rows:
        ifname = row.get("ifname", "")
        if not ifname or ifname in ("br_lan", "lo"):
            continue
        rx = row.get("rx_bytes") or 0
        tx = row.get("tx_bytes") or 0
        if rx == 0 and tx == 0:
            continue
        wan_traffic[ifname] = {
            "rx_bytes": rx,
            "tx_bytes": tx,
        }
    return wan_traffic


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def wan_uplink_summary_data(request, device_id: str):
    device = get_object_or_404(Device, pk=device_id)

    # Parse optional ?range= parameter (30m, 1h, 6h, 24h)
    time_range = request.GET.get("range", "").strip()
    influx_traffic = None
    if time_range in _VALID_RANGES:
        influx_traffic = _build_wan_traffic_from_influx(str(device_id), time_range)

    # Check if device is an NsBondDevice
    try:
        from sdwan_tunnel.models.nsbond_device import NsBondDevice
        is_sdwan = NsBondDevice.objects.filter(device_id=device_id).exists()
    except Exception:
        is_sdwan = False

    data = fetch_device_monitoring_data(device)
    wan_uplink_data = data.get("wan_uplink", {})
    wan_uplink_data["is_sdwan"] = is_sdwan

    # If a time range was requested, add influx_traffic alongside real-time wan_traffic
    if influx_traffic is not None:
        wan_uplink_data["influx_traffic"] = influx_traffic
        wan_uplink_data["range"] = time_range

    # Enrich with latest wan_link_quality from InfluxDB
    link_quality = {}
    try:
        rows = _influx_grouped_points(
            "SELECT LAST(healthy) AS healthy, rtt_ms, loss_pct, jitter_ms, score "
            "FROM wan_link_quality "
            f"WHERE device_id = '{device_id}' AND time > now() - 30m "
            "GROUP BY link"
        )
        for row in rows:
            link_name = row.get("link", "")
            if link_name:
                link_quality[link_name] = {
                    "healthy": bool(row.get("healthy", 0)),
                    "rtt_ms": row.get("rtt_ms"),
                    "loss_pct": row.get("loss_pct"),
                    "jitter_ms": row.get("jitter_ms"),
                    "score": row.get("score"),
                }
    except Exception as exc:
        logger.debug("wan_link_quality query failed: %s", exc)

    # Enrich with latest overlay_tunnel from InfluxDB
    overlay = {}
    try:
        rows = _influx_points(
            "SELECT LAST(connected) AS connected, active_tunnels, "
            "uptime_secs, rx_packets, tx_packets, fec_recovered "
            "FROM overlay_tunnel "
            f"WHERE device_id = '{device_id}' AND time > now() - 30m "
            "ORDER BY time DESC LIMIT 1"
        )
        if rows:
            row = rows[0]
            overlay = {
                "connected": bool(row.get("connected", 0)),
                "active_tunnels": int(row.get("active_tunnels", 0)),
                "uptime_secs": int(row.get("uptime_secs", 0)),
                "rx_packets": int(row.get("rx_packets", 0)),
                "tx_packets": int(row.get("tx_packets", 0)),
                "fec_recovered": int(row.get("fec_recovered", 0)),
            }
    except Exception as exc:
        logger.debug("overlay_tunnel query failed: %s", exc)

    wan_uplink_data["link_quality"] = link_quality
    wan_uplink_data["overlay"] = overlay
    return Response({"wan_uplink": wan_uplink_data})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def underlay_performance_data(request, device_id: str):
    """Underlay performance: WAN uptime timeline, path switch history, SLA, live health."""
    device = get_object_or_404(Device, pk=device_id)
    hours = min(int(request.GET.get("hours", 24)), 8760)  # max 365 days

    result = {
        "is_sdwan": False,
        "wan_timeline": [],
        "path_switches": [],
        "sla": None,
        "live_health": {},
    }

    # Check if device is an NsBondDevice. Non-SD-WAN devices still receive
    # wan_performance / wan_usage (both sourced from the generic `traffic`
    # measurement that every openwisp-monitoring device writes). SD-WAN-only
    # blocks (wan_timeline, path_switches, sla, live_health, wan_members)
    # remain gated on `is_sdwan`.
    is_sdwan = False
    try:
        from sdwan_tunnel.models.nsbond_device import NsBondDevice
        is_sdwan = NsBondDevice.objects.filter(device_id=device_id).exists()
    except Exception:
        is_sdwan = False
    result["is_sdwan"] = is_sdwan

    # Build wan_name → eth_name mapping from device interfaces (wan_info)
    wan_to_eth = {}
    try:
        device_data_obj = DeviceData.objects.filter(config=device.config).first()
        if device_data_obj and isinstance(device_data_obj.data_user_friendly, dict):
            for iface in device_data_obj.data_user_friendly.get("interfaces", []):
                wi = iface.get("wan_info")
                if wi and isinstance(wi, dict) and wi.get("iface") and iface.get("name"):
                    wan_to_eth[wi["iface"]] = iface["name"]
    except Exception:
        pass

    def normalize_link_name(name):
        """Convert wan/wan2 to eth1/eth2 using device wan_info mapping."""
        return wan_to_eth.get(name, name)

    # 1. WAN uptime timeline from InfluxDB (wan_link_quality per 5-min intervals).
    # Restricted to this device's own WAN uplinks (wan_info map) — remote-hub
    # probe links like `hub_wan2` are intentionally excluded because they are
    # not this device's WAN interfaces. If the wan_info map is empty we keep
    # everything (back-compat for devices that haven't reported it yet).
    # SD-WAN-only: `wan_link_quality` is only written by the controller's
    # poll_nsbond_device_status task which only runs on NsBondDevices.
    if is_sdwan:
        try:
            wan_eth_names = set(wan_to_eth.values())
            rows = _influx_grouped_points(
                "SELECT healthy, rtt_ms, loss_pct, jitter_ms, score "
                "FROM wan_link_quality "
                f"WHERE device_id = '{device_id}' AND time > now() - {hours}h "
                "GROUP BY link "
                "ORDER BY time ASC"
            )
            links = {}
            for row in rows:
                link = normalize_link_name(row.get("link", ""))
                if not link:
                    continue
                if wan_eth_names and link not in wan_eth_names:
                    continue
                if link not in links:
                    links[link] = []
                links[link].append({
                    "time": row.get("time"),
                    "healthy": bool(row.get("healthy", 0)),
                    "rtt": row.get("rtt_ms"),
                    "loss": row.get("loss_pct"),
                    "jitter": row.get("jitter_ms"),
                    "score": row.get("score"),
                })
            result["wan_timeline"] = links
        except Exception as exc:
            logger.debug("underlay wan_timeline query failed: %s", exc)

    # 1b. WAN traffic usage from InfluxDB traffic measurement (per-interface bytes).
    # Only include interfaces the device has flagged as WAN uplinks (is_wan/wan_info).
    # The `wan_to_eth` map built above already resolves to the authoritative list of
    # WAN ethernet names (its values). LAN interfaces like eth0 on a spoke that also
    # write to the `traffic` measurement must never appear here.
    try:
        wan_eth_names = set(wan_to_eth.values())
        skip_ifaces = {"br_lan", "lo", "vxlan", "wg0", "nsbond0", "nsbond0_ipsec", "eth0"}
        usage_rows = _influx_grouped_points(
            "SELECT SUM(rx_bytes) AS rx, SUM(tx_bytes) AS tx "
            "FROM traffic "
            f"WHERE object_id = '{device_id}' AND time > now() - {hours}h "
            "GROUP BY ifname"
        )
        wan_usage = {}
        for row in usage_rows:
            ifname = row.get("ifname", "")
            if not ifname:
                continue
            # Authoritative filter: if wan_info map is populated, whitelist only its
            # values. Fall back to the skip-list heuristic only when the map is empty.
            if wan_eth_names:
                if ifname not in wan_eth_names:
                    continue
            elif ifname in skip_ifaces:
                continue
            rx = row.get("rx") or 0
            tx = row.get("tx") or 0
            if rx == 0 and tx == 0:
                continue
            wan_usage[ifname] = {"rx_bytes": rx, "tx_bytes": tx, "total": rx + tx}
        # Calculate percentage
        grand = sum(v["total"] for v in wan_usage.values())
        for ifname, v in wan_usage.items():
            v["pct"] = round((v["total"] / grand) * 100, 1) if grand > 0 else 0
        result["wan_usage"] = wan_usage
    except Exception as exc:
        logger.debug("underlay wan_usage query failed: %s", exc)

    # 1c. WAN performance matrix: per-WAN time-series for bandwidth, latency, jitter, loss.
    # Frontend renders as a 4-column grid (one row per WAN) mirroring a per-pair matrix UI.
    # Data source is one-dimensional (per local WAN) because the router / InfluxDB don't
    # track (src_wan, dst_wan) pairs. Bandwidth comes from `traffic` (bytes → bps).
    # Latency / jitter / loss have two sources merged in priority order:
    #   1. `wan_link_quality` — SD-WAN devices, written by the controller's
    #      poll_nsbond_device_status task every ~5 minutes.
    #   2. `wan_ping`         — every openwisp-monitoring device, written by
    #      DeviceDataWriter._write_wan_ping from device_data.interfaces[].ping
    #      whenever the router pushes monitoring data.
    # If a WAN appears in both, wan_link_quality wins for any overlapping
    # bucket (it has a richer score field). wan_ping fills any gaps and is
    # the sole source on non-SD-WAN devices.
    try:
        # Choose bucket width so each chart has ~100-400 points regardless of window
        if hours <= 24:
            bucket_s = 300          # 5m  -> 288 pts / 24h
        elif hours <= 168:
            bucket_s = 1800         # 30m -> 336 pts / 7d
        elif hours <= 720:
            bucket_s = 10800        # 3h  -> 240 pts / 30d
        elif hours <= 2160:
            bucket_s = 21600        # 6h  -> 360 pts / 90d
        else:
            bucket_s = 86400        # 1d  -> 365 pts / 365d

        wan_performance = {}
        # Reuse the same WAN whitelist as wan_timeline / wan_usage — remote-hub
        # probe links are excluded so only this device's own WAN interfaces appear.
        wan_eth_names_perf = set(wan_to_eth.values())

        # Latency / jitter / loss — primary source: wan_link_quality (SD-WAN).
        lq_rows = _influx_grouped_points(
            "SELECT MEAN(rtt_ms) AS rtt, MEAN(jitter_ms) AS jitter, MEAN(loss_pct) AS loss "
            "FROM wan_link_quality "
            f"WHERE device_id = '{device_id}' AND time > now() - {hours}h "
            f"GROUP BY link, time({bucket_s}s) fill(none) "
            "ORDER BY time ASC"
        )
        # Track which (iface, bucket_time) slots wan_link_quality already filled
        # so wan_ping doesn't double-write them.
        filled_slots = {}  # {ifname: {metric: set(times)}}
        for row in lq_rows:
            link = normalize_link_name(row.get("link", ""))
            if not link:
                continue
            if wan_eth_names_perf and link not in wan_eth_names_perf:
                continue
            slot = wan_performance.setdefault(link, {
                "latency": [], "jitter": [], "loss": [], "bandwidth": [],
            })
            t = row.get("time")
            rtt = row.get("rtt")
            jit = row.get("jitter")
            los = row.get("loss")
            if t is None:
                continue
            slot_filled = filled_slots.setdefault(link, {
                "latency": set(), "jitter": set(), "loss": set(),
            })
            if rtt is not None:
                slot["latency"].append({"t": t, "v": round(float(rtt), 2)})
                slot_filled["latency"].add(t)
            if jit is not None:
                slot["jitter"].append({"t": t, "v": round(float(jit), 2)})
                slot_filled["jitter"].add(t)
            if los is not None:
                slot["loss"].append({"t": t, "v": round(float(los), 2)})
                slot_filled["loss"].add(t)

        # Latency / jitter / loss — fallback source: wan_ping (every device).
        # Keyed by ifname directly (already eth name). Fills gaps left by
        # wan_link_quality and is the sole source for non-SD-WAN devices.
        wp_rows = _influx_grouped_points(
            "SELECT MEAN(latency_ms) AS rtt, MEAN(jitter_ms) AS jitter, MEAN(packet_loss) AS loss "
            "FROM wan_ping "
            f"WHERE object_id = '{device_id}' AND time > now() - {hours}h "
            f"GROUP BY ifname, time({bucket_s}s) fill(none) "
            "ORDER BY time ASC"
        )
        for row in wp_rows:
            ifname = row.get("ifname", "")
            if not ifname:
                continue
            if wan_eth_names_perf and ifname not in wan_eth_names_perf:
                continue
            slot = wan_performance.setdefault(ifname, {
                "latency": [], "jitter": [], "loss": [], "bandwidth": [],
            })
            t = row.get("time")
            rtt = row.get("rtt")
            jit = row.get("jitter")
            los = row.get("loss")
            if t is None:
                continue
            slot_filled = filled_slots.setdefault(ifname, {
                "latency": set(), "jitter": set(), "loss": set(),
            })
            if rtt is not None and t not in slot_filled["latency"]:
                slot["latency"].append({"t": t, "v": round(float(rtt), 2)})
            if jit is not None and t not in slot_filled["jitter"]:
                slot["jitter"].append({"t": t, "v": round(float(jit), 2)})
            if los is not None and t not in slot_filled["loss"]:
                slot["loss"].append({"t": t, "v": round(float(los), 2)})

        # wan_ping rows are already in time order within each ifname group but
        # we merged them after wan_link_quality without sorting the combined list.
        # Sort each slot by time so the frontend draws lines in order.
        for series in wan_performance.values():
            for metric in ("latency", "jitter", "loss"):
                series[metric].sort(key=lambda p: p.get("t") or "")

        # Bandwidth — from `traffic` measurement keyed by ifname (already eth names).
        # Restrict to real WAN interfaces. The authoritative source is the wan_to_eth
        # map built earlier from device_data.wan_info — its values are the eth names
        # that are actually WAN uplinks (e.g. eth1, eth2). If that map is empty we
        # fall back to the skip-list heuristic.
        wan_eth_names = set(wan_to_eth.values())
        skip_ifaces = {"br_lan", "lo", "vxlan", "wg0", "nsbond0", "nsbond0_ipsec", "eth0"}
        tr_rows = _influx_grouped_points(
            "SELECT SUM(rx_bytes) AS rx, SUM(tx_bytes) AS tx "
            "FROM traffic "
            f"WHERE object_id = '{device_id}' AND time > now() - {hours}h "
            f"GROUP BY ifname, time({bucket_s}s) fill(none) "
            "ORDER BY time ASC"
        )
        for row in tr_rows:
            ifname = row.get("ifname", "")
            if not ifname:
                continue
            if wan_eth_names:
                if ifname not in wan_eth_names:
                    continue
            elif ifname in skip_ifaces:
                continue
            rx = row.get("rx") or 0
            tx = row.get("tx") or 0
            t = row.get("time")
            if t is None:
                continue
            slot = wan_performance.setdefault(ifname, {
                "latency": [], "jitter": [], "loss": [], "bandwidth": [],
            })
            slot["bandwidth"].append({
                "t": t,
                "rx_bps": round((float(rx) * 8) / bucket_s, 2),
                "tx_bps": round((float(tx) * 8) / bucket_s, 2),
            })

        # Strip WAN entries with zero data across all four metrics
        wan_performance = {
            name: series
            for name, series in wan_performance.items()
            if any(series[k] for k in ("latency", "jitter", "loss", "bandwidth"))
        }
        result["wan_performance"] = wan_performance
        result["wan_performance_bucket_s"] = bucket_s
    except Exception as exc:
        logger.debug("underlay wan_performance query failed: %s", exc)

    # Everything below this point is SD-WAN-only (depends on NsBondDevice /
    # ns-bonding RPCD state). Non-SD-WAN devices get `wan_performance` +
    # `wan_usage` above and nothing else.
    if not is_sdwan:
        return Response(result)

    # 2. Path switch events — InfluxDB only
    try:
        influx_events = _influx_grouped_points(
            "SELECT from_link, to_link, reason, "
            "latency_before, latency_after, loss_before, loss_after, "
            "jitter_before, jitter_after "
            "FROM path_switch_event "
            f"WHERE device_id = '{device_id}' AND time > now() - {hours}h "
            "ORDER BY time DESC LIMIT 50"
        )
        result["path_switches"] = [
            {
                "from_link": normalize_link_name(row.get("from_link", "")),
                "to_link": normalize_link_name(row.get("to_link", "")),
                "reason": row.get("reason", "unknown"),
                "latency_before": row.get("latency_before"),
                "latency_after": row.get("latency_after"),
                "loss_before": row.get("loss_before"),
                "jitter_before": row.get("jitter_before"),
                "timestamp": row.get("time", ""),
                "metadata": {
                    "loss_after": row.get("loss_after"),
                    "jitter_after": row.get("jitter_after"),
                },
            }
            for row in influx_events
        ]
    except Exception as exc:
        logger.debug("underlay path_switches query failed: %s", exc)

    # 2b. Calculate per-link uptime % from InfluxDB wan_link_quality.healthy
    #
    # Each data point has healthy=0 or healthy=1 per link, sampled every ~5 min.
    # uptime_pct = (count of healthy=1 / total samples) * 100
    try:
        timeline = result.get("wan_timeline", {})
        link_uptime = {}
        for link_name, points in timeline.items():
            if not points:
                continue
            total = len(points)
            up_count = sum(1 for p in points if p.get("healthy"))
            link_uptime[link_name] = {
                "samples": total,
                "up_count": up_count,
                "down_count": total - up_count,
                "uptime_pct": round((up_count / total) * 100, 1) if total else 0,
            }
        # Also calculate from path_switch_event if wan_link_quality has no data
        if not link_uptime:
            influx_ps = _influx_grouped_points(
                "SELECT COUNT(event) AS switches "
                "FROM path_switch_event "
                f"WHERE device_id = '{device_id}' AND time > now() - {hours}h "
                "GROUP BY from_link, to_link"
            )
            switch_counts = {}
            for row in influx_ps:
                fl = row.get("from_link", "")
                tl = row.get("to_link", "")
                cnt = row.get("switches", 0)
                if fl:
                    switch_counts[fl] = switch_counts.get(fl, 0) + cnt
                if tl:
                    switch_counts[tl] = switch_counts.get(tl, 0) + cnt
            if switch_counts:
                total_switches = sum(switch_counts.values())
                for lk, cnt in switch_counts.items():
                    # More switches away from a link = less uptime
                    link_uptime[lk] = {
                        "switches_away": cnt,
                        "total_switches": total_switches,
                        "uptime_pct": round(max(100 - (cnt / max(total_switches, 1)) * 100, 0), 1),
                    }
        result["link_uptime"] = link_uptime
    except Exception as exc:
        logger.debug("underlay link_uptime calculation failed: %s", exc)

    # 3. Applied SLA — find NsBondDevice → PathMonitor/LinkMonitor
    try:
        from sdwan_tunnel.models.nsbond_device import NsBondDevice
        nsd = NsBondDevice.objects.filter(
            device_id=device_id
        ).select_related("topology__link_monitor_policy").first()
        if nsd:
            sla_info = {"device_role": nsd.role, "node_id": nsd.node_id}
            # Link monitor policy
            lm = nsd.topology.link_monitor_policy if nsd.topology else None
            if lm:
                sla_info["link_monitor"] = {
                    "name": lm.name,
                    "protocol": lm.protocol,
                    "server": lm.server,
                    "interval_ms": lm.interval_ms,
                    "failtime": lm.failtime,
                    "recovertime": lm.recovertime,
                }
            # Path monitors (SLA thresholds)
            from sdwan_tunnel.models.path_monitor import NsBondPathMonitor
            pms = NsBondPathMonitor.objects.filter(nsbond_device=nsd)
            if not pms.exists() and nsd.topology:
                pms = NsBondPathMonitor.objects.filter(
                    topology=nsd.topology, is_global=True
                )
            sla_info["path_monitors"] = [
                {
                    "name": pm.name,
                    "latency_max": pm.latency_max,
                    "jitter_max": pm.jitter_max,
                    "loss_max": float(pm.loss_max),
                    "mos_min": float(pm.mos_min) if pm.mos_min else None,
                    "protocol": pm.protocol,
                    "server": pm.server,
                }
                for pm in pms[:5]
            ]
            result["sla"] = sla_info
    except Exception as exc:
        logger.debug("underlay sla query failed: %s", exc)

    # 4. Live WAN health from Redis cache
    try:
        from sdwan_tunnel.cache import get_cached_status
        from sdwan_tunnel.models.nsbond_device import NsBondDevice
        nsd = NsBondDevice.objects.filter(device_id=device_id).first()
        if nsd:
            cached = get_cached_status(nsd)
            if cached and isinstance(cached, dict):
                links_data = cached.get("links", {})
                for link_name, link_info in links_data.items():
                    if isinstance(link_info, dict):
                        result["live_health"][normalize_link_name(link_name)] = {
                            "healthy": link_info.get("healthy", False),
                            "rtt_ms": link_info.get("rtt_ms"),
                            "loss_pct": link_info.get("loss_pct"),
                            "jitter_ms": link_info.get("jitter_ms"),
                            "score": link_info.get("score"),
                        }
    except Exception as exc:
        logger.debug("underlay live_health failed: %s", exc)

    # 5. WAN members with path labels
    try:
        from sdwan_tunnel.models.nsbond_device import NsBondDevice
        from sdwan_tunnel.models.wan_member import WanMembers
        from sdwan_tunnel.models.pathlabel import PathLabel
        nsd = NsBondDevice.objects.filter(device_id=device_id).first()
        if nsd:
            members = WanMembers.objects.filter(
                nsbond_device=nsd
            ).select_related("path_label")
            result["wan_members"] = [
                {
                    "id": m.pk,
                    "interface": m.interface_name,
                    "weight": m.weight,
                    "priority": m.priority,
                    "enabled": m.enabled,
                    "path_label": {
                        "id": str(m.path_label.pk),
                        "name": m.path_label.name,
                        "color": m.path_label.color,
                    } if m.path_label else None,
                }
                for m in members
            ]
            result["topology_id"] = str(nsd.topology_id) if nsd.topology_id else None
            # Available path labels for this org
            org_id = nsd.device.organization_id if nsd.device else None
            from django.db.models import Q
            labels = PathLabel.objects.filter(
                Q(organization_id=org_id) | Q(organization__isnull=True)
            ) if org_id else PathLabel.objects.all()
            result["available_path_labels"] = [
                {"id": str(pl.pk), "name": pl.name, "color": pl.color}
                for pl in labels
            ]
    except Exception as exc:
        logger.debug("underlay wan_members/path_labels failed: %s", exc)

    return Response(result)


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
