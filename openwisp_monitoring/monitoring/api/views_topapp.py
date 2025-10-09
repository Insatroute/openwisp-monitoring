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
        name = name.capitalize()
        top_10_apps_list.append({"name": name, "value": value})

    return Response({"top_10_apps": top_10_apps_list})


# @api_view(["GET"])
# @permission_classes([IsAuthenticated])
# def global_top_devices(request):
#     """
#     API endpoint to return top 10 devices based on total rx/tx bytes
#     across all interfaces in the last 30 days.
#     """
#     # start_time = timezone.now() - timedelta(days=30)

#     devices = []

#     for device_data in DeviceData.objects.all():
#         # Skip if no data_timestamp
#         # if not device_data.data_timestamp:
#         #     continue

#         # # Convert data_timestamp to datetime for comparison
#         # try:
#         #     ts = device_data.data_timestamp
#         #     if isinstance(ts, str):
#         #         ts = timezone.datetime.fromisoformat(ts)
#         #     if ts.tzinfo is None:
#         #         ts = ts.replace(tzinfo=timezone.get_current_timezone())
#         # except Exception:
#         #     continue

#         # # Only include entries in the last 30 days
#         # if ts < start_time:
#         #     continue

#         data = device_data.data_user_friendly or {}
#         general = data.get("general", {})
#         interfaces = data.get("interfaces", [])

#         total_rx = total_tx = 0
#         for iface in interfaces:
#             stats = iface.get("statistics", {})
#             total_rx += stats.get("rx_bytes", 0)
#             total_tx += stats.get("tx_bytes", 0)

#         total_traffic = total_rx + total_tx

#         device_name = general.get("hostname")

#         devices.append({
#             "device": device_name,
#             "total_bytes": total_traffic,
#             "total_gb": round(total_traffic / (1024 ** 3), 3),
#         })

#     # Sort and return top 10
#     top_devices = sorted(devices, key=lambda d: d["total_bytes"], reverse=True)[:10]

#     return Response({
#         "top_10_devices": top_devices
#     })


from collections import defaultdict
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from ...db import  timeseries_db

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def global_top_devices(request):
    """
    Returns top 10 devices based on total rx/tx bytes from InfluxDB 'traffic' measurement (no date filter).
    """
    devices = Device.objects.all().values("id", "name")
    results = []

    for d in devices:
        device_id = str(d["id"])
        try:
            query = f"""
                SELECT SUM(rx_bytes) AS rx, SUM(tx_bytes) AS tx
                FROM traffic
                WHERE object_id = '{device_id}'
                GROUP BY object_id
            """
            data = timeseries_db.get_list_query(query)
        except Exception:
            data = []

        total_bytes = 0
        for row in data:
            total_bytes += (row.get("rx", 0) or 0) + (row.get("tx", 0) or 0)

        if total_bytes == 0:
            continue

        results.append({
            "device": d["name"],
            "device_id": device_id,
            "total_bytes": total_bytes,
            "total_gb": round(total_bytes / (1024 ** 3), 3),
        })

    # Sort descending and limit to top 10
    results.sort(key=lambda x: x["total_bytes"], reverse=True)
    top_devices = results[:10]

    return Response({
        "window": "all-time",
        "count_devices": len(results),
        "top_10_devices": top_devices,
    })