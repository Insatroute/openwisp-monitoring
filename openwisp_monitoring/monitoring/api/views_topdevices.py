from __future__ import annotations
from typing import Dict, Any, List, Optional, Tuple
from django.conf import settings
from django.core.cache import cache
from django.views.decorators.cache import never_cache
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

# =========================
# Settings (with safe defaults)
# =========================
INF_V2        = bool(getattr(settings, "TOPDEV_INFLUX_V2", False))  # default: InfluxDB v1

# InfluxDB v1
INF_HOST      = getattr(settings, "TOPDEV_INFLUX_HOST", "127.0.0.1")
INF_PORT      = int(getattr(settings, "TOPDEV_INFLUX_PORT", 8086))
INF_DB        = getattr(settings, "TOPDEV_INFLUX_DB",
                        getattr(settings, "TIMESERIES_DATABASE", {}).get("NAME", "openwisp2"))
INF_USER      = getattr(settings, "TOPDEV_INFLUX_USER",
                        getattr(settings, "TIMESERIES_DATABASE", {}).get("USER", ""))
INF_PASS      = getattr(settings, "TOPDEV_INFLUX_PASS",
                        getattr(settings, "TIMESERIES_DATABASE", {}).get("PASSWORD", ""))
INF_SSL       = bool(getattr(settings, "TOPDEV_INFLUX_SSL", False))
INF_VERIFY    = bool(getattr(settings, "TOPDEV_INFLUX_VERIFY_SSL", False))
INF_RP        = getattr(settings, "TOPDEV_INFLUX_RP", "")  # optional retention policy name

# InfluxDB v2 (optional)
INF_V2_URL    = getattr(settings, "TOPDEV_INFLUX_V2_URL", "http://127.0.0.1:8086")
INF_V2_TOKEN  = getattr(settings, "TOPDEV_INFLUX_V2_TOKEN", "")
INF_V2_ORG    = getattr(settings, "TOPDEV_INFLUX_V2_ORG", "")
INF_V2_BUCKET = getattr(settings, "TOPDEV_INFLUX_V2_BUCKET", "openwisp2")
INF_V2_VERIFY = bool(getattr(settings, "TOPDEV_INFLUX_V2_VERIFY_SSL", False))

# schema
MEASUREMENT   = getattr(settings, "TOPDEV_INFLUX_MEASUREMENT", "traffic")
FIELDS        = list(getattr(settings, "TOPDEV_INFLUX_FIELDS", ["rx_bytes", "tx_bytes"]))  # <- you set these
IFNAME_TAG    = getattr(settings, "TOPDEV_INFLUX_IFNAME_TAG", "ifname")
DEVICE_TAGS   = list(getattr(settings, "TOPDEV_INFLUX_DEVICE_TAGS", ["object_id"]))  # your tag list; put object_id first

# optional extra filter (eg: content_type=config.device)
DEVICE_FILTER_TAG   = getattr(settings, "TOPDEV_INFLUX_DEVICE_FILTER_TAG", None)
DEVICE_FILTER_VALUE = getattr(settings, "TOPDEV_INFLUX_DEVICE_FILTER_VALUE", None)

# =========================
# Helpers
# =========================
def _device_model():
    for path in ("openwisp_controller.config.models.Device", "openwisp_controller.models.Device"):
        try:
            module, cls = path.rsplit(".", 1)
            mod = __import__(module, fromlist=[cls])
            return getattr(mod, cls)
        except Exception:
            continue
    return None

def _parse_bool(s: Optional[str], default=False) -> bool:
    if s is None:
        return default
    return str(s).strip().lower() in ("1", "true", "yes", "y", "on")

def _parse_window(time_arg: Optional[str], start: Optional[str], end: Optional[str]) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    if start and end:
        return None, start, end
    t = (time_arg or "30d").lower()  # e.g., 1d, 7d, 30d
    return t, None, None

def _sum_fields(row: Dict[str, Any]) -> int:
    total = 0
    for f in FIELDS:
        v = row.get(f)
        if v is None:
            continue
        try:
            total += int(v)
        except Exception:
            try:
                total += int(float(v))
            except Exception:
                pass
    return total

def _measurement_with_rp() -> str:
    if INF_RP:
        return f'"{INF_RP}"."{MEASUREMENT}"'
    return f'"{MEASUREMENT}"'

# =========================
# Influx v1
# =========================
def _influx_v1_client():
    try:
        from influxdb import InfluxDBClient  # pip install influxdb
    except Exception as e:
        raise RuntimeError("Install Python package 'influxdb' (InfluxDB v1): pip install influxdb") from e
    return InfluxDBClient(
        host=INF_HOST, port=INF_PORT, username=(INF_USER or None), password=(INF_PASS or None),
        database=INF_DB, ssl=INF_SSL, verify_ssl=INF_VERIFY
    )

def _query_total_v1(cli, selector: str, ifnames: Optional[List[str]],
                    time_arg: Optional[str], start: Optional[str], end: Optional[str]) -> int:
    # time filter
    tfilter = f"time >= now() - {time_arg}" if not (start and end) else f"time >= '{start}' AND time <= '{end}'"

    # interface filter
    if_filter = ""
    if ifnames:
        parts = [x.replace("-", r"\-").replace(".", r"\.") for x in ifnames]
        if_filter = f' AND "{IFNAME_TAG}" =~ /^(?:{"|".join(parts)})$/'

    # optional extra where (eg: content_type)
    extra = ""
    if DEVICE_FILTER_TAG and DEVICE_FILTER_VALUE:
        extra = f' AND "{DEVICE_FILTER_TAG}" = \'{DEVICE_FILTER_VALUE}\''

    total = 0
    for tag in DEVICE_TAGS:
        q = f'''
SELECT SUM("{FIELDS[0]}") AS {FIELDS[0]}, SUM("{FIELDS[1]}") AS {FIELDS[1]}
FROM {_measurement_with_rp()}
WHERE {tfilter} AND "{tag}" = '{selector}' {if_filter} {extra}
'''
        try:
            rs = cli.query(q)
            rows = list(rs.get_points())
        except Exception:
            rows = []
        if rows:
            total = max(total, _sum_fields(rows[0]))
    return total

# =========================
# Influx v2 (optional support)
# =========================
def _influx_v2_client():
    try:
        from influxdb_client import InfluxDBClient  # pip install influxdb-client
    except Exception as e:
        raise RuntimeError("Install 'influxdb-client' for InfluxDB v2: pip install influxdb-client") from e
    return InfluxDBClient(url=INF_V2_URL, token=INF_V2_TOKEN, org=INF_V2_ORG, verify_ssl=INF_V2_VERIFY)

def _query_total_v2(cli, selector: str, ifnames: Optional[List[str]],
                    time_arg: Optional[str], start: Optional[str], end: Optional[str]) -> int:
    # range
    if start and end:
        range_clause = f'|> range(start: time(v: "{start}Z"), stop: time(v: "{end}Z"))'
    else:
        range_clause = f'|> range(start: -{time_arg})'
    # interface
    ifnames_filter = ""
    if ifnames:
        ors = " or ".join([f'r["{IFNAME_TAG}"] == "{x}"' for x in ifnames])
        ifnames_filter = f"|> filter(fn: (r) => {ors})"
    # extra (eg content_type)
    extra = ""
    if DEVICE_FILTER_TAG and DEVICE_FILTER_VALUE:
        extra = f'|> filter(fn: (r) => r["{DEVICE_FILTER_TAG}"] == "{DEVICE_FILTER_VALUE}")'
    total = 0
    qapi = cli.query_api()
    for tag in DEVICE_TAGS:
        flux = f'''
from(bucket: "{INF_V2_BUCKET}")
  {range_clause}
  |> filter(fn: (r) => r["_measurement"] == "{MEASUREMENT}")
  |> filter(fn: (r) => r["{tag}"] == "{selector}")
  |> filter(fn: (r) => r["_field"] == "{FIELDS[0]}" or r["_field"] == "{FIELDS[1]}")
  {ifnames_filter}
  {extra}
  |> aggregateWindow(every: 1h, fn: sum, createEmpty: false)
  |> group()
  |> sum()
'''
        try:
            tables = qapi.query(org=INF_V2_ORG, query=flux)
        except Exception:
            continue
        for tbl in tables:
            for rec in tbl.records:
                v = rec.get_value()
                try:
                    total = max(total, int(v))
                except Exception:
                    try:
                        total = max(total, int(float(v)))
                    except Exception:
                        pass
    return total

def _query_total(selector: str, ifnames: Optional[List[str]],
                 time_arg: Optional[str], start: Optional[str], end: Optional[str]) -> int:
    if INF_V2:
        cli = _influx_v2_client()
        return _query_total_v2(cli, selector, ifnames, time_arg, start, end)
    cli = _influx_v1_client()
    return _query_total_v1(cli, selector, ifnames, time_arg, start, end)

# =========================
# Public API view
# =========================
@api_view(["GET"])
@permission_classes([AllowAny])  # no auth, as requested
@never_cache
def top_devices_simple(request):
    """
    GET /api/v1/monitoring/top-devices-simple/?org=<slug|ALL>&time=30d&limit=5
        [&wan_ifs=eth1,pppoe-wan,eth0.2]
        [&include_all=1]        -> include full device list
        [&include_org=1]        -> include organization slug in each item
        [&start=YYYY-MM-DD HH:MM:SS&end=YYYY-MM-DD HH:MM:SS]  -> override time window

    Returns:
    {
      "org": "ALL" | "<slug>",
      "window": {"time": "30d", "start": null, "end": null},
      "limit": 5 | "all",
      "interface_scope": "eth1,pppoe-wan" | "ALL",
      "top": [ {device_id, name, [organization], total_bytes, total_gb}, ... ],
      "devices": [ ... ]  # only if include_all=1
    }
    """
    org_param = (request.GET.get("org") or request.GET.get("organization_slug") or "").strip()
    if not org_param:
        return Response({"detail": "Missing 'org' (organization slug or ALL)."}, status=400)
    all_orgs = org_param.lower() in ("all", "*")

    # flags
    include_all = _parse_bool(request.GET.get("include_all"), False)
    include_org = _parse_bool(request.GET.get("include_org"), False)

    # time window
    time_arg, start, end = _parse_window(request.GET.get("time"), request.GET.get("start"), request.GET.get("end"))

    # limit
    limit_raw = request.GET.get("limit", "5")
    if isinstance(limit_raw, str) and limit_raw.lower() in ("all", "*", "0"):
        limit = 10**9
        limit_label = "all"
    else:
        try:
            limit = max(1, min(int(limit_raw), 5000))
            limit_label = limit
        except Exception:
            return Response({"detail": "Invalid 'limit'."}, status=400)

    # ifnames
    ifnames = [x.strip() for x in (request.GET.get("wan_ifs") or "").split(",") if x.strip()] or None

    # cache key
    ck = f"td:ALL:{all_orgs}:org={org_param}:t={time_arg}:s={start}:e={end}:ifs={','.join(ifnames) if ifnames else 'ALL'}:lim={limit_label}:incall={int(include_all)}:incorg={int(include_org)}"
    cached = cache.get(ck)
    if cached:
        return Response(cached)

    Device = _device_model()
    if Device is None:
        return Response({"detail": "Cannot import OpenWISP Device model."}, status=500)

    # query device list
    try:
        qs = Device.objects.all() if all_orgs else Device.objects.filter(organization__slug=org_param)
        fields = ["id", "name"]
        if include_org or all_orgs:
            fields.append("organization__slug")
        devices = list(qs.values(*fields))
    except Exception as e:
        return Response({"detail": f"Error reading devices: {e}"}, status=500)

    # sum totals per device
    results: List[Dict[str, Any]] = []
    for d in devices:
        dev_id = str(d["id"])
        try:
            total = _query_total(dev_id, ifnames, time_arg, start, end)
        except Exception:
            total = 0
        item = {
            "device_id": dev_id,
            "name": d.get("name") or dev_id,
            "total_bytes": int(total or 0),
            "total_gb": round((total or 0) / (1024**3), 3),
        }
        if include_org or all_orgs:
            item["organization"] = d.get("organization__slug", None)
        results.append(item)

    # sort by total desc
    results.sort(key=lambda x: x["total_bytes"], reverse=True)

    payload = {
        "org": "ALL" if all_orgs else org_param,
        "window": {"time": time_arg, "start": start, "end": end},
        "limit": limit_label,
        "interface_scope": ",".join(ifnames) if ifnames else "ALL",
        "count_devices": len(results),
        "top": results[:limit],
        "note": f'Read from InfluxDB {"v2" if INF_V2 else "v1"} '
                f'({INF_DB if not INF_V2 else INF_V2_BUCKET}; measurement="{MEASUREMENT}"; fields="{"+".join(FIELDS)}"). '
                f'Use wan_ifs to avoid bridge double-counting.',
    }
    if include_all:
        payload["devices"] = results

    cache.set(ck, payload, 60)
    return Response(payload)
