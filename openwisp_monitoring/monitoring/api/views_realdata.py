from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from django.shortcuts import get_object_or_404
from swapper import load_model

Device = load_model("config", "Device") 
DeviceData = load_model("device_monitoring", "DeviceData")

def fetch_device_data(device):
    """Fetch device data from the associated device configuration."""
    try:
        device_data = DeviceData.objects.get(config=device.config)
        if not device_data or not isinstance(device_data.data_user_friendly, dict):
            return {}

        data = device_data.data_user_friendly
        return data

    except DeviceData.DoesNotExist:
        return {}


def fetch_cellular_data(device):
    """Fetch cellular data from the associated device configuration."""
    try:
        device_data = DeviceData.objects.get(config=device.config)
        if not device_data or not isinstance(device_data.data_user_friendly, dict):
            return {"cellular": {}}

        cellular = device_data.data_user_friendly.get("cellular", {})

        return {"cellular": cellular}

    except DeviceData.DoesNotExist:
        return {"cellular": {}}
    
def fetch_device_info(device):
    """Fetch device information from the associated device configuration."""
    try:
        device_data = DeviceData.objects.get(config=device.config)
        if not device_data or not isinstance(device_data.data_user_friendly, dict):
            return {"device": {}}

        device_info = device_data.data_user_friendly.get("device", {})

        return {"device": device_info}

    except DeviceData.DoesNotExist:
        return {"device": {}}


def fetch_device_monitoring_data(device):
    """
    Fetch device monitoring data.
    """
    try:
        device_data = DeviceData.objects.get(config=device.config)
        if not device_data or not isinstance(device_data.data_user_friendly, dict):
            return {"traffic": {}, "security": {}, "real_time_traffic": {}, "wan_uplink": {}, "cellular": {}}

        realtime = device_data.data_user_friendly.get("realtimemonitor", {})
        cellular = device_data.data_user_friendly.get("cellular", {})
        traffic = realtime.get("traffic", {})
        security = realtime.get("security", {})
        real_time_traffic = realtime.get("real_time_traffic", {})
        wan_uplink = realtime.get("wan_uplink", {})

        return {"traffic": traffic, "security": security, "real_time_traffic": real_time_traffic, "wan_uplink": wan_uplink, "cellular": cellular}

    except DeviceData.DoesNotExist:
        return {"traffic": {}, "security": {}, "real_time_traffic": {}, "wan_uplink": {}, "cellular": {}}

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

    for app in top_apps:
        parts = app["name"].split(".")
        if len(parts) > 2:
            app_name = ".".join(parts[2:])
        else:
            app_name = parts[-1]
        app["name"] = app_name.capitalize()  

    response_data = {
        "top_protocols": top_protocols,
        "top_hosts": top_hosts,
        "top_apps": top_apps,
    }

    return Response(response_data)

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def wan_uplink_summary_data(request, device_id: str):
    device = get_object_or_404(Device, pk=device_id)
    data = fetch_device_monitoring_data(device)
    wan_uplink_data = data.get("wan_uplink", {})

    response_data = {
        "wan_uplink": wan_uplink_data,
    }
    return Response(response_data)

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def cellular_summary_data(request, device_id: str):
    device = get_object_or_404(Device, pk=device_id)
    data = fetch_cellular_data(device)
    cellular_data = data.get("cellular", {})

    response_data = {
        "cellular": cellular_data,
    }

    return Response(response_data)

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def device_info_summary_data(request, device_id: str):
    device = get_object_or_404(Device, pk=device_id)
    data = fetch_device_info(device)
    device_info = data.get("device", {}).get("device_info", {})

    response_data = {
        "device_info": device_info,
    }

    return Response(response_data)

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def interfaces_summary_data(request, device_id: str):
    device = get_object_or_404(Device, pk=device_id)
    data = fetch_device_data(device)
    interfaces = data.get("interfaces", [])

    response_data = {
        "interfaces": interfaces,
        "count": len(interfaces)
    }
    return Response(response_data)