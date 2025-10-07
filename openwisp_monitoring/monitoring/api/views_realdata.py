from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from django.shortcuts import get_object_or_404
from swapper import load_model

Device = load_model("config", "Device") 
DeviceData = load_model("device_monitoring", "DeviceData")


def fetch_device_monitoring_data(device):
    """
    Fetch device monitoring data.
    """
    try:
        device_data = DeviceData.objects.get(config=device.config)
        if not device_data or not isinstance(device_data.data_user_friendly, dict):
            return {"traffic": {}, "security": {}, "real_time_traffic": {}}

        realtime = device_data.data_user_friendly.get("realtimemonitor", {})
        traffic = realtime.get("traffic", {})
        security = realtime.get("security", {})
        real_time_traffic = realtime.get("real_time_traffic", {})

        return {"traffic": traffic, "security": security, "real_time_traffic": real_time_traffic}

    except DeviceData.DoesNotExist:
        return {"traffic": {}, "security": {}, "real_time_traffic": {}}

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def traffic_summary_data(request, device_id: str):
    device = get_object_or_404(Device, pk=device_id)
    data = fetch_device_monitoring_data(device)
    traffic = data.get("traffic", {})

    dpi_summery_v2 = traffic.get("dpi_summery_v2", {})
    dpi_client_data = traffic.get("dpi_client_data", [])

    response_data = {
        "dpi_summery_v2": dpi_summery_v2,
        "dpi_client_data": dpi_client_data,
    }

    return Response(response_data)

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def security_summary_data(request, device_id: str):
    device = get_object_or_404(Device, pk=device_id) 
    data = fetch_device_monitoring_data(device)
    security = data.get("security", {})

    blocklist = security.get("blocklist", {})
    brute_force_attack = security.get("brute_force_attack", {})

    response_data = {
        "blocklist": blocklist,
        "brute_force_attack": brute_force_attack,
    }

    return Response(response_data)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def real_time_traffic_summary_data(request, device_id: str):
    device = get_object_or_404(Device, pk=device_id)
    data = fetch_device_monitoring_data(device)
    traffic_data = data.get("real_time_traffic", {}).get("data", {}).get("talkers", {})

    top_protocols = traffic_data.get("top_protocols", [])
    top_hosts = traffic_data.get("top_hosts", [])
    top_apps = traffic_data.get("top_apps", [])

    response_data = {
        "top_protocols": top_protocols,
        "top_hosts": top_hosts,
        "top_apps": top_apps,
    }

    return Response(response_data)