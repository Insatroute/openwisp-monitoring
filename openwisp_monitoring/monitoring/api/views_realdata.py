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
        traffic = realtime.get("traffic", {}).get("dpi_summery_v2", {})
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

    total_traffic = traffic.get("total_traffic", 0)
    hourly_traffic = traffic.get("hourly_traffic", [])
    clients = traffic.get("clients", [])
    applications = traffic.get("applications", [])
    protocols = traffic.get("protocols", [])
    remote_hosts = traffic.get("remote_hosts", [])

    response_data = {
        "total_traffic": total_traffic,
        "hourly_traffic": hourly_traffic,
        "clients": clients,
        "applications": applications,
        "protocols": protocols,
        "remote_hosts": remote_hosts,
    }

    return Response(response_data)

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def security_summary_data(request, device_id: str):
    device = get_object_or_404(Device, pk=device_id) 
    data = fetch_device_monitoring_data(device)
    security = data.get("security", {})

    blocklist = security.get("blocklist", {})
    brute_force = security.get("brute_force_attack", {})

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
            "attack_by_ip": brute_force.get("attack_by_ip", {}),
            "attack_by_hour": brute_force.get("attack_by_hour", []),
        },
    }

    return Response(response_data)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def real_time_traffic_summary_data(request, device_id: str):
    device = get_object_or_404(Device, pk=device_id)
    data = fetch_device_monitoring_data(device)
    traffic_data = data.get("real_time_traffic", {}).get("data", {}).get("talkers", {})

    response_data = {
        "top_protocols": traffic_data.get("top_protocols", []),
        "top_hosts": traffic_data.get("top_hosts", []),
        "top_apps": traffic_data.get("top_apps", []),
    }

    return Response(response_data)