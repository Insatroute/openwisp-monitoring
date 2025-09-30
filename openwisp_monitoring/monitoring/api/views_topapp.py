from django.http import JsonResponse
from swapper import load_model
import requests
from rest_framework.authtoken.models import Token
 
Device = load_model("config", "Device")
 
def get_api_token(user):
    """Get or create DRF token for the given user dynamically."""
    token, created = Token.objects.get_or_create(user=user)
    return token.key
 
def fetch_device_top_apps(device_id, token):
    """Fetch top apps for a device using the API token."""
    url = f"https://controller.nexapp.co.in/api/v1/monitoring/device/{device_id}/dpi_client_summary/"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        response = requests.get(url, headers=headers, timeout=10, verify=False)
        response.raise_for_status()
        data = response.json()
 
        latest_raw = data.get("latest_raw", {})
        real_time = latest_raw.get("real_time_traffic", {}).get("real_time_traffic", {})
        talkers = real_time.get("talkers", {})
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