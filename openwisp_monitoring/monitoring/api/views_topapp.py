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
DeviceData = load_model("device_monitoring", "DeviceData")
 
def get_api_token(user):
    """Get or create DRF token for the given user dynamically."""
    token, created = Token.objects.get_or_create(user=user)
    return token.key

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def global_top_apps(request):
    """
    API endpoint to return top 10 applications across all devices.
    """
    app_counter = Counter()

    # Aggregate app traffic across all devices
    for device_data in DeviceData.objects.all():
        data = device_data.data_user_friendly or {}
        top_apps = (
            data.get("realtimemonitor", {})
            .get("real_time_traffic", {})
            .get("data", {})
            .get("talkers", {})
            .get("top_apps", [])
        )
        for app in top_apps:
            app_counter[app["name"]] += app["value"]

    # Get top 10 apps
    top_10_apps = app_counter.most_common(10)

    # Split app names after second dot
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

    return Response({"top_10_apps": top_10_apps_list})
