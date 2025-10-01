from django.http import JsonResponse
from swapper import load_model
import requests
from rest_framework.authtoken.models import Token
from django.shortcuts import get_object_or_404
 
Device = load_model("config", "Device")
 
def get_api_token(user):
    """Get or create DRF token for the given user dynamically."""
    token, created = Token.objects.get_or_create(user=user)
    return token.key
 
def fetch_device_top_apps(device_id, token):
    """Fetch top apps for a device using the API token."""
    url = f"https://controller.nexapp.co.in/api/v1/monitoring/device/{device_id}/real_time_monitor_data/"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        response = requests.get(url, headers=headers, timeout=10, verify=False)
        response.raise_for_status()
        data = response.json()
 
        latest_raw = data.get("latest_raw", {})
        real_time = latest_raw.get("real_time_traffic", {})
        data = real_time.get("data", {})
        talkers = data.get("talkers", {})
        top_apps = talkers.get("top_apps", [])
 
        if not isinstance(top_apps, list):
            return []
 
        result = []
        for app in top_apps:
            if isinstance(app, dict):
                name = app.get("name")
                value = app.get("value", 0)
                if name:
                    result.append({"name": name, "value": value})
        return result
 
    except Exception as e:
        print(f"Error fetching device {device_id}: {e}")
        return []
 
def global_top_apps_view(request):
    """Return global top 10 applications across all devices."""
    if not request.user.is_authenticated:
        return JsonResponse({"error": "User not authenticated"}, status=401)
 
    token = get_api_token(request.user)
 
    global_apps = {}
 
    for device in Device.objects.all():
        apps = fetch_device_top_apps(device.pk, token)
        for app in apps:
            name = app["name"]
            value = app["value"]
            global_apps[name] = global_apps.get(name, 0) + value
 
    # Sort top 10
    top_10_apps = sorted(global_apps.items(), key=lambda x: x[1], reverse=True)[:10]
    # top_10_apps_list = [{"name": name, "value": value} for name, value in top_10_apps]

    top_10_apps_list = []
    for name, value in top_10_apps:
        parts = name.split('.')
        if len(parts) > 2:
            # take everything after the second dot
            name = '.'.join(parts[2:])
        else:
            # fallback if less than 3 parts
            name = parts[-1]
        top_10_apps_list.append({"name": name, "value": value})
 
    return JsonResponse({"top_10_apps": top_10_apps_list})
 
 
# def fetch_device_traffic(device, token):
#     """
#     Fetch DPI summary (dpi_summery_v2) for a single device.
#     Returns: hourly_traffic, clients, applications, remote_hosts, protocols
#     """
#     url = f"https://controller.nexapp.co.in/api/v1/monitoring/device/{device.pk}/real_time_monitor_data/"
#     headers = {"Authorization": f"Bearer {token}"}
#     try:
#         response = requests.get(url, headers=headers, timeout=10, verify=False)
#         response.raise_for_status()
#         data = response.json()
 
#         dpi_summary = (
#             data.get("latest_raw", {})
#             .get("traffic", {})
#             .get("dpi_summery_v2", {})
#         )
 
#         hourly_traffic = dpi_summary.get("hourly_traffic", [])
#         clients = dpi_summary.get("clients", [])
#         applications = dpi_summary.get("applications", [])
#         remote_hosts = dpi_summary.get("remote_hosts", [])
#         protocols = dpi_summary.get("protocols", [])
 
#         return hourly_traffic, clients, applications, remote_hosts, protocols
 
#     except Exception as e:
#         print(f"[ERROR] Failed for device {device.pk}: {e}")
#         return [], [], [], [], []
 
 
# def traffic_summary_view(request):
#     """Aggregate DPI summary across all devices and return JSON."""
#     if not request.user.is_authenticated:
#         return JsonResponse({"error": "User not authenticated"}, status=401)
 
#     token = get_api_token(request.user)
 
#     # Initialize aggregators
#     hourly_map = {}
#     clients_map = {}
#     applications_map = {}
#     remote_hosts_map = {}
#     protocols_map = {}
 
#     for device in Device.objects.all():
#         hourly, clients, apps, hosts, protos = fetch_device_traffic(device, token)
 
#         # Aggregate hourly traffic
#         for h in hourly:
#             hid = str(h.get("id", "")).zfill(2)
#             t = h.get("traffic", 0) or 0
#             hourly_map[hid] = hourly_map.get(hid, 0) + t
 
#         # Aggregate clients
#         for c in clients:
#             cid = c.get("id")
#             label = c.get("label", cid)
#             t = c.get("traffic", 0) or 0
#             if cid:
#                 if cid not in clients_map:
#                     clients_map[cid] = {"id": cid, "label": label, "traffic": 0}
#                 clients_map[cid]["traffic"] += t
 
#         # Aggregate applications
#         for a in apps:
#             aid = a.get("id")
#             label = a.get("label", aid)
#             t = a.get("traffic", 0) or 0
#             if aid:
#                 if aid not in applications_map:
#                     applications_map[aid] = {"id": aid, "label": label, "traffic": 0}
#                 applications_map[aid]["traffic"] += t
 
#         # Aggregate remote hosts
#         for r in hosts:
#             rid = r.get("id")
#             label = r.get("label", rid)
#             t = r.get("traffic", 0) or 0
#             if rid:
#                 if rid not in remote_hosts_map:
#                     remote_hosts_map[rid] = {"id": rid, "label": label, "traffic": 0}
#                 remote_hosts_map[rid]["traffic"] += t
 
#         # Aggregate protocols
#         for p in protos:
#             pid = p.get("id")
#             label = p.get("label", pid)
#             t = p.get("traffic", 0) or 0
#             if pid:
#                 if pid not in protocols_map:
#                     protocols_map[pid] = {"id": pid, "label": label, "traffic": 0}
#                 protocols_map[pid]["traffic"] += t
 
#     # Prepare response
#     response_data = {
#         "hourly_traffic": [{"id": k, "traffic": v} for k, v in sorted(hourly_map.items())],
#         "clients": sorted(clients_map.values(), key=lambda x: x["traffic"], reverse=True),
#         "applications": sorted(applications_map.values(), key=lambda x: x["traffic"], reverse=True),
#         "remote_hosts": sorted(remote_hosts_map.values(), key=lambda x: x["traffic"], reverse=True),
#         "protocols": sorted(protocols_map.values(), key=lambda x: x["traffic"], reverse=True),
#     }
 
#     return JsonResponse(response_data, safe=False)

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
