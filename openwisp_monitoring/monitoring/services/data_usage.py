import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone as dt_timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from django.core.cache import cache
from django.db.models import Sum
from django.db.models.functions import TruncHour
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from swapper import load_model

from openwisp_monitoring.db import timeseries_db
from openwisp_monitoring.device.base.models import UP_STATUSES

DeviceData = load_model("device_monitoring", "DeviceData")

ALLOWED_PERIODS = {"1d", "3d", "7d", "30d", "365d"}
INTERNAL_APPS = {"netify.nethserver", "netify.snort", "netify.netify"}
CACHE_TTL_SECONDS = 45


class DataUsageValidationError(ValueError):
    pass


@dataclass(frozen=True)
class WindowParams:
    period: str
    start: datetime
    end: datetime
    is_custom: bool = False


def _safe_int(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value != value:  # nan
            return default
        return int(value)
    try:
        cleaned = str(value).strip()
        if not cleaned:
            return default
        return int(float(cleaned))
    except Exception:
        return default


def _safe_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    try:
        text = str(value)
    except Exception:
        return default
    return text if text else default


def _oid_norm(raw: str) -> str:
    return _safe_str(raw).replace("-", "").lower()


def _normalize_operator(raw: str) -> str:
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


def _app_label(app_name: str) -> str:
    name = _safe_str(app_name).strip()
    if not name:
        return "Unknown"
    if name.startswith("netify."):
        name = name.split(".", 1)[1]
    return name.replace("_", " ").replace(".", " ").strip().title()


def _link_status(device_data, iface: dict) -> str:
    monitoring = getattr(device_data, "monitoring", None)
    live_is_up = bool(monitoring and getattr(monitoring, "status", None) in UP_STATUSES)
    if not live_is_up:
        return "disconnected"
    return "connected" if iface.get("up") else "disconnected"


def _ipv4_addr_mask(iface: dict) -> Tuple[Optional[str], Optional[str]]:
    ipv4 = next((a for a in iface.get("addresses", []) if a.get("family") == "ipv4"), None)
    if not ipv4:
        return None, None
    return ipv4.get("address"), ipv4.get("mask")


def _parse_window(period: Optional[str], start: Optional[str], end: Optional[str]) -> WindowParams:
    now = timezone.now()

    if bool(start) ^ bool(end):
        raise DataUsageValidationError("Both 'start' and 'end' must be provided together")

    if start and end:
        start_dt = parse_datetime(start)
        end_dt = parse_datetime(end)
        if start_dt is None or end_dt is None:
            raise DataUsageValidationError(
                "Invalid date format. Use ISO datetime, e.g. 2026-03-14T10:30:00+05:30"
            )
        if timezone.is_naive(start_dt):
            start_dt = timezone.make_aware(start_dt, timezone.get_current_timezone())
        if timezone.is_naive(end_dt):
            end_dt = timezone.make_aware(end_dt, timezone.get_current_timezone())
        if start_dt > end_dt:
            raise DataUsageValidationError("'start' cannot be greater than 'end'")
        if (end_dt - start_dt) > timedelta(days=365):
            raise DataUsageValidationError("Custom range cannot exceed 365 days")
        return WindowParams(period="custom", start=start_dt, end=end_dt, is_custom=True)

    normalized = _safe_str(period, "7d").lower() or "7d"
    if normalized not in ALLOWED_PERIODS:
        raise DataUsageValidationError(
            f"Unsupported period '{normalized}'. Allowed: {', '.join(sorted(ALLOWED_PERIODS))}"
        )

    if normalized.endswith("h"):
        delta = timedelta(hours=int(normalized[:-1]))
    else:
        delta = timedelta(days=int(normalized[:-1]))

    return WindowParams(period=normalized, start=now - delta, end=now, is_custom=False)


def _window_from_request(request) -> WindowParams:
    params = getattr(request, "query_params", None)
    if params is None:
        params = getattr(request, "GET", {})
    period = params.get("period") or params.get("time") or "7d"
    start = params.get("start")
    end = params.get("end")
    return _parse_window(period, start, end)


def _scope_devicedata_qs(user):
    qs = DeviceData.objects.select_related("monitoring").all()
    if user.is_superuser:
        return qs
    return qs.filter(organization__in=user.organizations.all())


def _org_scope_label(user) -> str:
    return "superuser_all" if user.is_superuser else "organization_membership"


def _cache_key(user, window: WindowParams) -> str:
    if user.is_superuser:
        scope = "superuser"
    else:
        org_ids = list(user.organizations.values_list("id", flat=True).order_by("id"))
        scope = f"orgs:{','.join(str(i) for i in org_ids)}"
    return (
        f"ow:du:v2:{user.pk}:{scope}:{window.period}:"
        f"{window.start.isoformat()}:{window.end.isoformat()}"
    )


def _format_window_iso(dt: datetime) -> str:
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.get_current_timezone())
    return dt.astimezone(dt_timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _influx_time_clause(window: WindowParams) -> str:
    if not window.is_custom:
        return f"time >= now() - {window.period}"
    return f"time >= '{_format_window_iso(window.start)}' AND time <= '{_format_window_iso(window.end)}'"


def _chunks(items: List[str], size: int) -> Iterable[List[str]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _sum_row_field(row: Dict[str, Any], keys: List[str]) -> int:
    for key in keys:
        if key in row:
            return _safe_int(row.get(key), 0)
    lower = {str(k).lower(): v for k, v in row.items()}
    for key in keys:
        if key.lower() in lower:
            return _safe_int(lower.get(key.lower()), 0)
    return 0


def _build_timeseries_iface_totals(device_ids: List[str], window: WindowParams) -> Tuple[Dict[str, Dict[str, Dict[str, int]]], List[str]]:
    totals: Dict[str, Dict[str, Dict[str, int]]] = defaultdict(lambda: defaultdict(lambda: {"rx": 0, "tx": 0}))
    warnings: List[str] = []

    if not device_ids:
        return totals, warnings

    # Query both dashed and non-dashed UUID forms for compatibility.
    id_variants = sorted({v for device_id in device_ids for v in (device_id, device_id.replace("-", "")) if v})
    time_clause = _influx_time_clause(window)

    try:
        for chunk in _chunks(id_variants, 200):
            if len(chunk) == 1:
                object_filter = f"object_id = '{chunk[0]}'"
            else:
                regex = "|".join(re.escape(x) for x in chunk)
                object_filter = f"object_id =~ /^(?:{regex})$/"

            query = (
                "SELECT SUM(tx_bytes) AS tx_bytes, SUM(rx_bytes) AS rx_bytes "
                "FROM traffic "
                f"WHERE {time_clause} AND (content_type = 'config.device' OR content_type = 'device') AND {object_filter} "
                "GROUP BY object_id, ifname"
            )
            result = timeseries_db.query(query)
            for key, points in result.items():
                tags = key[1] if len(key) > 1 else {}
                object_id = _safe_str(tags.get("object_id", ""))
                if not object_id:
                    continue
                ifname = _safe_str(tags.get("ifname", ""), "unknown")
                oid = _oid_norm(object_id)
                for pt in points:
                    totals[oid][ifname]["rx"] += _safe_int(pt.get("rx_bytes"), 0)
                    totals[oid][ifname]["tx"] += _safe_int(pt.get("tx_bytes"), 0)
    except Exception as exc:
        warnings.append(f"timeseries_query_failed:{exc}")

    return totals, warnings


def _build_snapshot_iface_totals(device_rows: List[Dict[str, Any]]) -> Dict[str, Dict[str, Dict[str, int]]]:
    totals: Dict[str, Dict[str, Dict[str, int]]] = defaultdict(lambda: defaultdict(lambda: {"rx": 0, "tx": 0}))
    for row in device_rows:
        oid = _oid_norm(row["device_id"])
        for iface in row["interfaces_meta"]:
            stats = iface.get("statistics") or {}
            ifname = _safe_str(iface.get("name"), "unknown")
            totals[oid][ifname]["rx"] += _safe_int(stats.get("rx_bytes"), 0)
            totals[oid][ifname]["tx"] += _safe_int(stats.get("tx_bytes"), 0)
    return totals


def _collect_device_rows(user) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for dd in _scope_devicedata_qs(user):
        data = getattr(dd, "data_user_friendly", None) or {}
        general = data.get("general") or {}
        interfaces_meta = data.get("interfaces") or []

        name = general.get("hostname") or getattr(dd, "name", "") or str(dd.pk)
        rows.append(
            {
                "device": dd,
                "device_id": str(dd.pk),
                "name": name,
                "hostname": general.get("hostname") or name,
                "serial_number": general.get("serialnumber") or getattr(dd, "serial_number", "") or "",
                "model": getattr(dd, "model", "") or "",
                "path_label": getattr(dd, "wan_path_label", "") or "",
                "interfaces_meta": interfaces_meta,
            }
        )
    return rows


def _build_location_map(device_ids: List[str]) -> Dict[str, str]:
    location_map: Dict[str, str] = {}
    try:
        DeviceLocation = load_model("geo", "DeviceLocation")
        qs = (
            DeviceLocation.objects.filter(content_object_id__in=device_ids)
            .select_related("location")
            .only("content_object_id", "location__name")
        )
        for item in qs:
            if str(item.content_object_id) not in location_map:
                location_map[str(item.content_object_id)] = item.location.name if getattr(item, "location", None) else "-"
    except Exception:
        pass
    return location_map


def _classify_network(signal: Dict[str, Any]) -> str:
    if "5g" in signal:
        return "5G"
    if "lte" in signal:
        return "4G LTE"
    if "3g" in signal:
        return "3G"
    return "Unknown"


def _top_apps_from_dpi(user, window: WindowParams) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, List[Dict[str, Any]]], List[Dict[str, Any]], List[str]]:
    warnings: List[str] = []
    global_counter: Counter = Counter()
    device_counter: Dict[str, Counter] = defaultdict(Counter)
    raw_rows: List[Dict[str, Any]] = []

    try:
        from dpi_analytics.models import DpiAppTraffic

        qs = DpiAppTraffic.objects.filter(period_start__gte=window.start, period_start__lte=window.end)
        if not user.is_superuser:
            qs = qs.filter(device__organization__in=user.organizations.all())

        rows = qs.values("device_id", "app_name").annotate(
            total_down=Sum("download_bytes"),
            total_up=Sum("upload_bytes"),
        )
        for row in rows:
            app_name = _safe_str(row.get("app_name")).strip()
            if not app_name or app_name in INTERNAL_APPS:
                continue
            traffic = _safe_int(row.get("total_down"), 0) + _safe_int(row.get("total_up"), 0)
            if traffic <= 0:
                continue
            label = _app_label(app_name)
            global_counter[label] += traffic
            device_counter[_safe_str(row.get("device_id"))][label] += traffic
            raw_rows.append({"app_name": app_name, "label": label, "traffic": traffic})
    except Exception as exc:
        warnings.append(f"dpi_app_traffic_unavailable:{exc}")

    top_apps = [{"label": label, "traffic": traffic} for label, traffic in global_counter.most_common(10)]
    all_apps = [{"label": label, "traffic": traffic} for label, traffic in global_counter.most_common(50)]

    device_apps: Dict[str, List[Dict[str, Any]]] = {}
    for device_id, counter in device_counter.items():
        device_apps[device_id] = [
            {"label": label, "traffic": traffic}
            for label, traffic in counter.most_common(20)
        ]

    return top_apps, all_apps, device_apps, raw_rows, warnings


def _top_apps_from_snapshot(device_rows: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, List[Dict[str, Any]]]]:
    global_counter: Counter = Counter()
    device_counter: Dict[str, Counter] = defaultdict(Counter)

    for row in device_rows:
        data = getattr(row["device"], "data_user_friendly", None) or {}
        apps = (
            data.get("realtimemonitor", {})
            .get("traffic", {})
            .get("dpi_summery_v2", {})
            .get("applications", [])
        )
        for app in apps:
            app_id = _safe_str(app.get("id"))
            if app_id in INTERNAL_APPS:
                continue
            label = _safe_str(app.get("label"))
            traffic = _safe_int(app.get("traffic"), 0)
            if not label or traffic <= 0:
                continue
            global_counter[label] += traffic
            device_counter[row["device_id"]][label] += traffic

    top_apps = [{"label": label, "traffic": traffic} for label, traffic in global_counter.most_common(10)]
    all_apps = [{"label": label, "traffic": traffic} for label, traffic in global_counter.most_common(50)]
    device_apps = {
        device_id: [
            {"label": label, "traffic": traffic}
            for label, traffic in counter.most_common(20)
        ]
        for device_id, counter in device_counter.items()
    }
    return top_apps, all_apps, device_apps


def _hourly_dpi_series(user, window: WindowParams) -> Tuple[List[Dict[str, Any]], List[str]]:
    warnings: List[str] = []
    series: List[Dict[str, Any]] = []
    try:
        from dpi_analytics.models import DpiAppTraffic

        qs = DpiAppTraffic.objects.filter(period_start__gte=window.start, period_start__lte=window.end)
        if not user.is_superuser:
            qs = qs.filter(device__organization__in=user.organizations.all())

        grouped = (
            qs.annotate(hour=TruncHour("period_start"))
            .values("hour")
            .annotate(download=Sum("download_bytes"), upload=Sum("upload_bytes"))
            .order_by("hour")
        )
        for row in grouped:
            if not row.get("hour"):
                continue
            series.append(
                {
                    "time": row["hour"].isoformat(),
                    "download": _safe_int(row.get("download"), 0),
                    "upload": _safe_int(row.get("upload"), 0),
                }
            )
    except Exception as exc:
        warnings.append(f"dpi_hourly_unavailable:{exc}")
    return series, warnings


def build_data_usage_payload(user, window: WindowParams) -> Dict[str, Any]:
    device_rows = _collect_device_rows(user)
    device_ids = [row["device_id"] for row in device_rows]
    location_map = _build_location_map(device_ids)

    warnings: List[str] = []
    iface_totals, ts_warnings = _build_timeseries_iface_totals(device_ids, window)
    warnings.extend(ts_warnings)

    if not iface_totals:
        warnings.append("timeseries_fallback_snapshot")
        iface_totals = _build_snapshot_iface_totals(device_rows)

    summary = {
        "total": {"sent": 0, "received": 0, "total": 0},
        "cellular": {"sent": 0, "received": 0, "total": 0},
        "wired": {"sent": 0, "received": 0, "total": 0},
        "wireless": {"sent": 0, "received": 0, "total": 0},
    }
    wan_summary = {"total": 0, "connected": 0, "abnormal": 0, "disconnected": 0}

    devices_payload: List[Dict[str, Any]] = []
    wan_rows: List[Dict[str, Any]] = []
    carrier_counter: Counter = Counter()
    network_counter: Counter = Counter()
    modem_details: List[Dict[str, Any]] = []

    for row in device_rows:
        dd = row["device"]
        oid = _oid_norm(row["device_id"])

        interfaces_payload: List[Dict[str, Any]] = []
        total_rx = 0
        total_tx = 0

        for iface in row["interfaces_meta"]:
            ifname = _safe_str(iface.get("name"), "unknown")
            traffic_row = iface_totals.get(oid, {}).get(ifname)
            if traffic_row is None:
                # case-insensitive fallback
                traffic_row = iface_totals.get(oid, {}).get(ifname.lower())
            if traffic_row is None:
                # safe default if timeseries has no row for this interface
                traffic_row = {"rx": 0, "tx": 0}

            rx = _safe_int(traffic_row.get("rx"), 0)
            tx = _safe_int(traffic_row.get("tx"), 0)
            total = rx + tx

            ipv4_addr, ipv4_mask = _ipv4_addr_mask(iface)
            iface_type = _safe_str(iface.get("type")).lower()
            is_wan_eth = iface_type == "ethernet" and iface.get("is_wan") is True
            is_mobile = iface_type == "mobile"
            is_wireless = iface_type in ("wifi", "wireless")

            if is_mobile:
                summary["cellular"]["sent"] += tx
                summary["cellular"]["received"] += rx
            elif is_wan_eth:
                summary["wired"]["sent"] += tx
                summary["wired"]["received"] += rx
            elif is_wireless:
                summary["wireless"]["sent"] += tx
                summary["wireless"]["received"] += rx

            total_rx += rx
            total_tx += tx

            status = _link_status(dd, iface)
            iface_payload = {
                "name": ifname,
                "type": iface_type or "unknown",
                "is_wan": bool(iface.get("is_wan")),
                "up": bool(iface.get("up")),
                "status": status,
                "ip": ipv4_addr,
                "mask": ipv4_mask,
                "rx_bytes": rx,
                "tx_bytes": tx,
                "total": total,
                "mobile": iface.get("mobile") or {},
                "ping": iface.get("ping") or {},
            }
            interfaces_payload.append(iface_payload)

            if is_mobile or is_wan_eth:
                wan_summary["total"] += 1
                wan_summary[status] += 1

                raw_name = _safe_str(ifname).lower()
                if is_mobile:
                    if raw_name == "modem":
                        display_name = "Cellular1"
                    elif raw_name == "modem2":
                        display_name = "Cellular2"
                    else:
                        display_name = "Cellular"
                    display_type = "cellular"
                else:
                    display_name = ifname
                    display_type = "ethernet"

                ping = iface_payload["ping"]
                throughput = ping.get("throughput") or {}
                wan_rows.append(
                    {
                        "device_id": row["device_id"],
                        "hostname": row["hostname"],
                        "serial_number": row["serial_number"],
                        "model": row["model"],
                        "location": location_map.get(row["device_id"], "-"),
                        "path_label": row["path_label"],
                        "interface_name": display_name,
                        "interface": ifname,
                        "uplink_type": display_type,
                        "type": display_type,
                        "interface_ip": ipv4_addr,
                        "ip": ipv4_addr,
                        "interface_mask": ipv4_mask,
                        "status": status,
                        "throughput_tx_bytes": _safe_int(throughput.get("tx_bytes"), tx),
                        "throughput_rx_bytes": _safe_int(throughput.get("rx_bytes"), rx),
                        "tx_bytes": tx,
                        "rx_bytes": rx,
                        "ping_dest": ping.get("dest_ip"),
                        "ping_latency_ms": ping.get("latency_ms"),
                        "ping_packet_loss": ping.get("packet_loss"),
                        "ping_jitter_ms": ping.get("jitter_ms"),
                    }
                )

            if is_mobile:
                mobile = iface_payload["mobile"]
                operator = _normalize_operator(_safe_str(mobile.get("operator_name"), "Unknown"))
                signal = mobile.get("signal") or {}
                network_type = _classify_network(signal)
                carrier_counter[operator] += 1
                network_counter[network_type] += 1
                modem_details.append(
                    {
                        "hostname": row["hostname"],
                        "device_id": row["device_id"],
                        "name": ifname,
                        "carrier": operator,
                        "network": network_type,
                        "rx_bytes": rx,
                        "tx_bytes": tx,
                    }
                )

        devices_payload.append(
            {
                "device_id": row["device_id"],
                "name": row["name"],
                "hostname": row["hostname"],
                "serial_number": row["serial_number"],
                "model": row["model"],
                "location": location_map.get(row["device_id"], "-"),
                "path_label": row["path_label"],
                "total_bytes": total_rx + total_tx,
                "rx_bytes": total_rx,
                "tx_bytes": total_tx,
                "interfaces": sorted(interfaces_payload, key=lambda x: x["total"], reverse=True),
            }
        )

    for key in ("cellular", "wired", "wireless"):
        summary[key]["total"] = summary[key]["sent"] + summary[key]["received"]
        summary["total"]["sent"] += summary[key]["sent"]
        summary["total"]["received"] += summary[key]["received"]
    summary["total"]["total"] = summary["total"]["sent"] + summary["total"]["received"]

    devices_payload.sort(key=lambda d: d["total_bytes"], reverse=True)

    top_apps, all_apps, device_apps, raw_app_rows, app_warnings = _top_apps_from_dpi(user, window)
    warnings.extend(app_warnings)
    if not top_apps and app_warnings:
        snap_top, snap_all, snap_device_apps = _top_apps_from_snapshot(device_rows)
        if snap_top:
            warnings.append("top_apps_fallback_snapshot")
            top_apps, all_apps, device_apps = snap_top, snap_all, snap_device_apps

    hourly, hourly_warnings = _hourly_dpi_series(user, window)
    warnings.extend(hourly_warnings)

    iface_traffic_counter = Counter({
        row["interface_name"]: row["rx_bytes"] + row["tx_bytes"]
        for row in wan_rows
    })
    total_iface_traffic = sum(iface_traffic_counter.values()) or 1
    top_ifaces = iface_traffic_counter.most_common(10)

    app_counter_for_links = Counter({a["label"]: a["traffic"] for a in top_apps})
    links = []
    for label, app_bytes in app_counter_for_links.items():
        for iface_name, iface_bytes in top_ifaces:
            proportion = iface_bytes / total_iface_traffic
            link_value = int(app_bytes * proportion)
            if link_value > 0:
                links.append({"source": label, "target": iface_name, "value": link_value})

    payload = {
        "meta": {
            "period": window.period,
            "window_start": window.start.isoformat(),
            "window_end": window.end.isoformat(),
            "org_scope": _org_scope_label(user),
            "device_count": len(devices_payload),
        },
        "warnings": sorted({w for w in warnings if w}),
        "summary": summary,
        "devices": devices_payload,
        "top_devices": devices_payload[:10],
        "wan": {
            "summary": wan_summary,
            "rows": wan_rows,
        },
        "mobile": {
            "carrier": {"labels": list(carrier_counter.keys()), "data": list(carrier_counter.values())},
            "network": {"labels": list(network_counter.keys()), "data": list(network_counter.values())},
            "total_modems": len(modem_details),
            "modems": modem_details,
        },
        "apps": {
            "top_apps": top_apps,
            "all_apps": all_apps,
            "raw_rows": raw_app_rows,
            "device_apps": device_apps,
        },
        "timeseries": {
            "by_type": {
                "cellular": summary["cellular"]["total"],
                "wired": summary["wired"]["total"],
                "wireless": summary["wireless"]["total"],
            },
            "hourly": hourly,
        },
        "apps_by_interface": {
            "apps": [{"label": label, "traffic": traffic} for label, traffic in app_counter_for_links.most_common(10)],
            "interfaces": [{"name": name, "traffic": traffic} for name, traffic in top_ifaces],
            "links": links,
        },
    }

    return payload


def get_data_usage_payload_for_request(request) -> Dict[str, Any]:
    window = _window_from_request(request)
    key = _cache_key(request.user, window)
    cached = cache.get(key)
    if cached:
        return cached

    payload = build_data_usage_payload(request.user, window)
    cache.set(key, payload, CACHE_TTL_SECONDS)
    return payload
