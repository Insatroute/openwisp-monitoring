from django.http import JsonResponse
from swapper import load_model
import requests
from rest_framework.authtoken.models import Token
from django.shortcuts import get_object_or_404
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from django.utils.timezone import now, timedelta
from openwisp_monitoring.device.models import RealTraffic
from collections import Counter
 
Device = load_model("config", "Device")
 
def get_api_token(user):
    """Get or create DRF token for the given user dynamically."""
    token, created = Token.objects.get_or_create(user=user)
    return token.key

# @api_view(["GET"])
# def global_top_apps(request):
#     """
#     API to fetch Top 10 apps from all DPIRecords
#     """
#     days = now() - timedelta(days=7) #last 7 days
#     apps_dict = {}

#     records = RealTraffic.objects.filter(created__gte=days)

#     for record in records:
#         raw = record.raw

#         # If raw is a list, take the first element
#         if isinstance(raw, list) and raw:
#             raw = raw[0]

#         if not isinstance(raw, dict):
#             continue

#         top_apps = (
#             raw
#             .get("real_time_traffic", {})
#             .get("data", {})
#             .get("talkers", {})
#             .get("top_apps", [])
#         )

#         for app in top_apps:
#             name = app.get("name")
#             value = app.get("value", 0)
#             if name:
#                 apps_dict[name] = apps_dict.get(name, 0) + value

#     top_10_apps = sorted(apps_dict.items(), key=lambda x: x[1], reverse=True)[:10]

#     top_10_apps_list = []
#     for name, value in top_10_apps:
#         parts = name.split('.')
#         if len(parts) > 2:
#             # take everything after the second dot
#             name = '.'.join(parts[2:])
#         else:
#             # fallback if less than 3 parts
#             name = parts[-1]
#         top_10_apps_list.append({"name": name, "value": value})

#     return Response({
#         "time_range": "last_7_days",
#         "top_10_apps": top_10_apps_list
#     })


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def global_top_apps_view(request):
    """
    Aggregate top apps from all devices and return top 10 applications.
    """
    from .views_realdata import fetch_device_monitoring_data  # adjust import

    total_apps_counter = Counter()

    devices = Device.objects.all()
    for device in devices:
        data = fetch_device_monitoring_data(device)
        top_apps = data.get("real_time_traffic", {}).get("data", {}).get("talkers", {}).get("top_apps", [])
        
        for app in top_apps:
            total_apps_counter[app["name"]] += app["value"]

    # Get top 10 applications
    top_10_apps = total_apps_counter.most_common(10)

    # Prepare response
    response_data = [{"name": name, "value": value} for name, value in top_10_apps]
    
    return Response(response_data)


def fetch_device_traffic(device, token: str):
    """
    Fetch DPI summary (dpi_summery_v2) for a single device.
    Returns: hourly_traffic, clients, applications, remote_hosts, protocols
    """
    url = f"https://controller.nexapp.co.in/api/v1/monitoring/device/{device.pk}/real_time_monitor_data/"
    headers = {"Authorization": f"Bearer {token}"}

    try:
        response = requests.get(url, headers=headers, timeout=10, verify=False)
        response.raise_for_status()
        data = response.json()

        dpi_summary = data.get("latest_raw", {}).get("traffic", {}).get("dpi_summery_v2", {})

        hourly_traffic = dpi_summary.get("hourly_traffic", []) or []
        clients = dpi_summary.get("clients", []) or []
        applications = dpi_summary.get("applications", []) or []
        remote_hosts = dpi_summary.get("remote_hosts", []) or []
        protocols = dpi_summary.get("protocols", []) or []
        total_traffic = dpi_summary.get("total_traffic", 0) or 0

        return hourly_traffic, clients, applications, remote_hosts, protocols, total_traffic

    except requests.RequestException as e:
        return [], [], [], [], [], 0


def traffic_summary_view(request, device_id: str) -> JsonResponse:
    """
    Return DPI traffic summary for a single device.
    URL: /api/v1/monitoring/device/<device_id>/traffic-summary/
    """
    if not request.user.is_authenticated:
        return JsonResponse({"error": "User not authenticated"}, status=401)

    token = get_api_token(request.user)
    device = get_object_or_404(Device, pk=device_id)

    hourly, clients, apps, hosts, protocols, total_traffic = fetch_device_traffic(device, token)

    # Safely build response, skip any non-dict items
    response_data = {
        "total_traffic": total_traffic,
        "hourly_traffic": [
            {"id": str(h.get("id", "")).zfill(2), "traffic": h.get("traffic", 0) or 0}
            for h in hourly if isinstance(h, dict)
        ],
        "clients": [
            {"id": c.get("id"), "label": c.get("label", c.get("id")), "traffic": c.get("traffic", 0) or 0}
            for c in clients if isinstance(c, dict)
        ],
        "applications": [
            {"id": a.get("id"), "label": a.get("label", a.get("id")), "traffic": a.get("traffic", 0) or 0}
            for a in apps if isinstance(a, dict)
        ],
        "remote_hosts": [
            {"id": r.get("id"), "label": r.get("label", r.get("id")), "traffic": r.get("traffic", 0) or 0}
            for r in hosts if isinstance(r, dict)
        ],
        "protocols": [
            {"id": p.get("id"), "label": p.get("label", p.get("id")), "traffic": p.get("traffic", 0) or 0}
            for p in protocols if isinstance(p, dict)
        ],
    }

    return JsonResponse(response_data, safe=False)

# def fetch_device_monitoring(device, token: str):
#     """
#     Fetch monitoring data for a single device.
#     Returns a dict with both sections.
#     """
#     url = f"https://controller.nexapp.co.in/api/v1/monitoring/device/{device.pk}/real_time_monitor_data/"
#     headers = {"Authorization": f"Bearer {token}"}

#     try:
#         response = requests.get(url, headers=headers, timeout=10, verify=False)
#         response.raise_for_status()
#         data = response.json().get("latest_raw", {})

#         return {
#             "traffic": data.get("traffic", {}).get("dpi_summery_v2", {}),
#             "security": data.get("Security", {}),
#         }
#     except requests.RequestException:
#         return {"traffic": {}, "security": {}}

def fetch_device_monitoring(device, token: str):
    """
    Prefer reading from DPIRecord.raw (latest),
    fallback to controller API if missing.
    """
    # Try DB first
    last_record = (
        RealTraffic.objects.filter(device=device).order_by("-created").first()
    )
    if last_record:
        data = last_record.raw.get("latest_raw", {})
    else:
        # fallback to API
        url = f"https://controller.nexapp.co.in/api/v1/monitoring/device/{device.pk}/real_time_monitor_data/"
        headers = {"Authorization": f"Bearer {token}"}
        try:
            response = requests.get(url, headers=headers, timeout=10, verify=False)
            response.raise_for_status()
            data = response.json().get("latest_raw", {})
        except requests.RequestException:
            data = {}

    return {
        "traffic": data.get("traffic", {}).get("dpi_summery_v2", {}),
        "security": data.get("Security", {}),
    }


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def security_summary_view(request, device_id: str):
    token = get_api_token(request.user)
    device = get_object_or_404(Device, pk=device_id)

    data = fetch_device_monitoring(device, token)
    security = data["security"]

    blocklist = security.get("BlockList", {})
    brute_force = security.get("Brute_Force_Attack", {})

    response_data = {
        "blocklist": {
            "first_seen": blocklist.get("first_seen", 0),
            "malware_count": blocklist.get("malware_count", 0),
            "malware_by_hour": blocklist.get("malware_by_hour", []),
            "malware_by_category": blocklist.get("malware_by_category", {}),
            "malware_by_chain": blocklist.get("malware_by_chain", {}),
        },
        "brute_force_attack": {
            "first_seen": brute_force.get("first_seen", 0),
            "attack_count": brute_force.get("attack_count", 0),
            "attack_by_ip": brute_force.get("attack_by_ip", []),
            "attack_by_hour": brute_force.get("attack_by_hour", []),
        },
    }

    return Response(response_data)