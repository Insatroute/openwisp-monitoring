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
            .get("traffic", {})
            .get("dpi_summery_v2", {})
            .get("applications", [])
        )
        for app in top_apps:
            label = app.get("label")
            traffic = app.get("traffic", 0)
            if label:
                app_counter[label] += traffic

    # Get top 10 apps
    top_10_apps = app_counter.most_common(10)

    top_10_apps_list = [
        {"label": label.capitalize(), "traffic": traffic} for label, traffic in top_10_apps
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
