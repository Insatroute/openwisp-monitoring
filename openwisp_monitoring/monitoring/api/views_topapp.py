from django.http import JsonResponse
from swapper import load_model
import requests
from rest_framework.authtoken.models import Token
from django.shortcuts import get_object_or_404
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from collections import Counter
from datetime import timedelta
from django.utils import timezone
from .views_realdata import fetch_device_data

 
Device = load_model("config", "Device")
DeviceData = load_model("device_monitoring", "DeviceData")
DeviceLocation = load_model("geo", "DeviceLocation")
 
def get_api_token(user):
    """Get or create DRF token for the given user dynamically."""
    token, created = Token.objects.get_or_create(user=user)
    return token.key

def get_org_devices(user):
    """
    Return devices limited to the organizations the user belongs to.
    Superuser sees all devices.
    """
    qs = Device.objects.all()

    if user.is_superuser:
        return qs

    # if user has many-to-many organizations relation
    user_orgs = user.organizations.all()
    return qs.filter(organization__in=user_orgs)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def global_top_apps(request):
    """
    API endpoint to return top 10 applications across allowed devices.
    """
    app_counter = Counter()

    # 1) Filter devices based on organization access
    allowed_devices = get_org_devices(request.user)

    # 2) Filter DeviceData based on allowed devices
    device_data_qs = DeviceData.objects.filter(device__in=allowed_devices)

    # 3) Aggregate application traffic
    for device_data in device_data_qs:
        data = device_data.data_user_friendly or {}

        top_apps = (
            data.get("realtimemonitor", {})
            .get("traffic", {})
            .get("dpi_summery_v2", {})
            .get("applications", [])
        )

        for app in top_apps:
            app_id = app.get("id", "") or ""

            # Skip specific netify apps
            if app_id in ("netify.nethserver", "netify.snort", "netify.netify"):
                continue

            label = app.get("label")
            traffic = app.get("traffic", 0)

            if label:
                app_counter[label] += traffic

    # 4) Extract top 10 applications
    top_10_apps = app_counter.most_common(10)

    top_10_apps_list = [
        {"label": label.capitalize(), "traffic": traffic}
        for label, traffic in top_10_apps
    ]

    return Response({"top_10_apps": top_10_apps_list})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def global_top_devices(request):
    """
    API endpoint to return top 10 devices based on total rx/tx bytes
    across all interfaces.
    """

    devices = []

    for device_data in DeviceData.objects.all():

        data = device_data.data_user_friendly or {}
        general = data.get("general", {})
        interfaces = data.get("interfaces", [])

        total_rx = total_tx = 0
        for iface in interfaces:
            stats = iface.get("statistics", {})
            total_rx += stats.get("rx_bytes", 0)
            total_tx += stats.get("tx_bytes", 0)

        total_traffic = total_rx + total_tx

        device_name = general.get("hostname")

        devices.append({
            "device": device_name,
            "total_bytes": total_traffic,
            "total_gb": round(total_traffic / (1024 ** 3), 3),
        })

    # Sort and return top 10
    top_devices = sorted(devices, key=lambda d: d["total_bytes"], reverse=True)[:10]

    return Response({
        "top_10_devices": top_devices
    })


def _ipv4_addr_mask(iface: dict):
    ipv4 = next(
        (a for a in iface.get("addresses", []) if a.get("family") == "ipv4"),
        None,
    )
    if not ipv4:
        return None, None
    return ipv4.get("address"), ipv4.get("mask")


# def _link_status(iface: dict) -> str:
#     """
#     connected  -> up and ping ok
#     abnormal   -> up but packet loss > 0 or no ping
#     disconnected -> up == False
#     """
#     if not iface.get("up"):
#         return "disconnected"

#     ping = iface.get("ping") or {}
#     loss = ping.get("packet_loss")
#     latency = ping.get("latency_ms")

#     # packet_loss comes like "0%" in your sample
#     try:
#         loss_val = float(str(loss).replace("%", "")) if loss is not None else 0.0
#     except ValueError:
#         loss_val = 0.0

#     if loss_val > 0 or latency is None:
#         return "abnormal"

#     return "connected"

def _link_status(iface: dict) -> str:
    if iface.get("up"):
        return "connected"
    else:
        return "disconnected"


# def _human_uptime(seconds: int | None) -> str:
#     if not seconds:
#         return "-"
#     td = timedelta(seconds=int(seconds))
#     days = td.days
#     hours, rem = divmod(td.seconds, 3600)
#     minutes, _ = divmod(rem, 60)

#     parts = []
#     if days:
#         parts.append(f"{days} day{'s' if days != 1 else ''}")
#     if hours:
#         parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
#     if minutes and not days:
#         # only show minutes if uptime < 1 day to keep it short
#         parts.append(f"{minutes} min")
#     return " ".join(parts) or "0 min"

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def wan_uplinks_all_devices(request):
    devices = get_org_devices(request.user)

    summary = {
        "total": 0,
        "connected": 0,
        "abnormal": 0,
        "disconnected": 0,
    }

    rows = []

    for device in devices:
        try:
            data = fetch_device_data(device)
        except Exception:
            continue

        general = data.get("general", {}) or {}
        hostname = general.get("hostname") or getattr(device, "hostname", "")
        serialnumber = general.get("serialnumber") or getattr(device, "serialnumber", "")

        interfaces = data.get("interfaces", []) or []

        dl = (
            DeviceLocation.objects
            .filter(content_object_id=device.id)
            .select_related("location")
            .first()
        )
        location_name = dl.location.name if dl and dl.location else "-"

        for iface in interfaces:
            # ONLY ethernet WAN interfaces
            if not (
                iface.get("type") == "ethernet"
                and iface.get("is_wan") is True
            ):
                continue

            # determine link status
            status = _link_status(iface)

            summary["total"] += 1
            summary[status] += 1

            ipv4_addr, ipv4_mask = _ipv4_addr_mask(iface)
            ping = iface.get("ping") or {}

            rows.append({
                "device_id": device.pk,
                "hostname": hostname,
                "serial_number": serialnumber,
                "model": getattr(device, "model", ""),
                "location": location_name,
                "path_label": getattr(device, "wan_path_label", ""),

                "interface_name": iface.get("name"),
                "uplink_type": iface.get("type"),

                "interface_ip": ipv4_addr,
                "interface_mask": ipv4_mask,

                "throughput_tx_bytes": ping.get("throughput", {}).get("tx_bytes"),
                "throughput_rx_bytes": ping.get("throughput", {}).get("rx_bytes"),

                "ping_dest": ping.get("dest_ip"),
                "ping_latency_ms": ping.get("latency_ms"),
                "ping_packet_loss": ping.get("packet_loss"),
                "ping_jitter_ms": ping.get("jitter_ms"),

                "status": status,  # connected / abnormal / disconnected
            })

    return Response({
        "summary": summary,
        "rows": rows,
    })


def _add_traffic(bucket, tx_bytes, rx_bytes):
    bucket["sent"] += tx_bytes or 0
    bucket["received"] += rx_bytes or 0
    bucket["total"] = bucket["sent"] + bucket["received"]


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def data_usage_all_devices(request):

    summary = {
        "total":    {"sent": 0, "received": 0, "total": 0},
        "cellular": {"sent": 0, "received": 0, "total": 0},
        "wired":    {"sent": 0, "received": 0, "total": 0},
        "wireless": {"sent": 0, "received": 0, "total": 0},
    }

    devices = get_org_devices(request.user)

    for device in devices:
        try:
            data = fetch_device_data(device)
        except Exception:
            continue

        interfaces = data.get("interfaces", []) or []

        for iface in interfaces:
            stats = iface.get("statistics") or {}
            tx = stats.get("tx_bytes") or 0
            rx = stats.get("rx_bytes") or 0

            iface_type = iface.get("type")

            # decide which bucket this interface belongs to
            if iface_type == "mobile":
                _add_traffic(summary["cellular"], tx, rx)
            elif iface_type == "ethernet" and iface.get("is_wan") is True:
                _add_traffic(summary["wired"], tx, rx)
            elif iface_type in ("wifi", "wireless"):
                _add_traffic(summary["wireless"], tx, rx)
            else:
                # ignore bridges, tunnels, etc. for category tiles
                continue

    # now compute TOTAL = sum of categories (no extra hidden bytes)
    for key in ("cellular", "wired", "wireless"):
        _add_traffic(
            summary["total"],
            summary[key]["sent"],
            summary[key]["received"],
        )

    return Response(summary)

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def mobile_distribution_all_devices(request):
    devices_qs = get_org_devices(request.user)

    carrier_counter = Counter()
    network_counter = Counter()
    total_modems = 0

    for device in devices_qs:
        try:
            data = fetch_device_data(device)
        except Exception:
            # skip devices we cannot fetch data for
            continue

        interfaces = data.get("interfaces", []) or []

        for iface in interfaces:
            if iface.get("type") != "mobile":
                continue

            mobile = iface.get("mobile", {}) or {}
            total_modems += 1

            # Carrier name
            operator = mobile.get("operator_name") or "Unknown"
            carrier_counter[operator] += 1

            # Network type detection
            signal = mobile.get("signal") or {}
            if "5g" in signal:
                network_counter["5G"] += 1
            elif "lte" in signal:
                network_counter["4G"] += 1
            elif "3g" in signal:
                network_counter["3G"] += 1
            else:
                network_counter["Unknown"] += 1

    return Response({
        "carrier": {
            "labels": list(carrier_counter.keys()),
            "data": list(carrier_counter.values())
        },
        "network": {
            "labels": list(network_counter.keys()),
            "data": list(network_counter.values())
        },
        "total_modems": total_modems
    })