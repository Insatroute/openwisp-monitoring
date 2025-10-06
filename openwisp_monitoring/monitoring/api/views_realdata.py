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
        device_data = DeviceData.objects.get(device=device)
        if not device_data or not isinstance(device_data.data_user_friendly, dict):
            return {"traffic": {}, "security": {}}

        realtime = device_data.data_user_friendly.get("realtimemonitor", {})
        traffic = realtime.get("traffic", {}).get("dpi_summery_v2", {})
        security = realtime.get("security", {})

        return {"traffic": traffic, "security": security}

    except DeviceData.DoesNotExist:
        return {"traffic": {}, "security": {}}

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def traffic_summary_view(request, device_id: str):
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
def security_summary_view(request, device_id: str):
    device = get_object_or_404(Device, pk=device_id) 
    data = fetch_device_monitoring_data(device)
    security = data["security"]

    blocklist = security.get("blockList", {})
    brute_force = security.get("brute_Force_Attack", {})

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
