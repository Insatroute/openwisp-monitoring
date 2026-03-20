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
from .views_realdata import fetch_device_data
from openwisp_monitoring.monitoring.services import (
    DataUsageValidationError,
    get_data_usage_payload_for_request,
)

 
Device = load_model("config", "Device")
DeviceData = load_model("device_monitoring", "DeviceData")
DeviceLocation = load_model("geo", "DeviceLocation")


def _du_payload_or_error(request):
    try:
        return get_data_usage_payload_for_request(request), None
    except DataUsageValidationError as exc:
        return None, Response({"detail": str(exc), "code": "invalid_period"}, status=400)
 
def get_api_token(user):
    """Get or create DRF token for the given user dynamically."""
    token, created = Token.objects.get_or_create(user=user)
    return token.key

def get_org_devices(user):
    """
    Return devices limited to the organizations the user belongs to.
    Superuser sees all devices.
    """
    qs = Device.objects.all()

    if user.is_superuser:
        return qs

    # if user has many-to-many organizations relation
    user_orgs = user.organizations.all()
    return qs.filter(organization__in=user_orgs)

def get_org_device_data(user):
    """
    Returns DeviceData limited by organization via related Device.
    Superuser gets all DeviceData.
    """
    qs = DeviceData.objects.all()

    if user.is_superuser:
        return qs

    user_orgs = user.organizations.all()
    # IMPORTANT: adjust `device__organization` if your FK name differs
    return qs.filter(device__organization__in=user_orgs)

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def global_top_apps(request):
    payload, error = _du_payload_or_error(request)
    if error:
        return error
    return Response(
        {
            "top_10_apps": payload["apps"]["top_apps"],
            "meta": payload["meta"],
            "warnings": payload["warnings"],
            "deprecated": True,
        }
    )

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def global_top_devices(request):
    payload, error = _du_payload_or_error(request)
    if error:
        return error
    top_devices = []
    for item in payload["top_devices"]:
        total_bytes = int(item.get("total_bytes", 0))
        top_devices.append(
            {
                "device": item.get("name") or item.get("hostname") or "",
                "total_bytes": total_bytes,
                "total_gb": round(total_bytes / (1024 ** 3), 3),
            }
        )
    return Response(
        {
            "top_10_devices": top_devices,
            "meta": payload["meta"],
            "warnings": payload["warnings"],
            "deprecated": True,
        }
    )


def _ipv4_addr_mask(iface: dict):
    ipv4 = next(
        (a for a in iface.get("addresses", []) if a.get("family") == "ipv4"),
        None,
    )
    if not ipv4:
        return None, None
    return ipv4.get("address"), ipv4.get("mask")


# def _link_status(iface: dict) -> str:
#     """
#     connected  -> up and ping ok
#     abnormal   -> up but packet loss > 0 or no ping
#     disconnected -> up == False
#     """
#     if not iface.get("up"):
#         return "disconnected"

#     ping = iface.get("ping") or {}
#     loss = ping.get("packet_loss")
#     latency = ping.get("latency_ms")

#     # packet_loss comes like "0%" in your sample
#     try:
#         loss_val = float(str(loss).replace("%", "")) if loss is not None else 0.0
#     except ValueError:
#         loss_val = 0.0

#     if loss_val > 0 or latency is None:
#         return "abnormal"

#     return "connected"

def _link_status(iface: dict) -> str:
    if iface.get("up"):
        return "connected"
    else:
        return "disconnected"


# def _human_uptime(seconds: int | None) -> str:
#     if not seconds:
#         return "-"
#     td = timedelta(seconds=int(seconds))
#     days = td.days
#     hours, rem = divmod(td.seconds, 3600)
#     minutes, _ = divmod(rem, 60)

#     parts = []
#     if days:
#         parts.append(f"{days} day{'s' if days != 1 else ''}")
#     if hours:
#         parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
#     if minutes and not days:
#         # only show minutes if uptime < 1 day to keep it short
#         parts.append(f"{minutes} min")
#     return " ".join(parts) or "0 min"

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def wan_uplinks_all_devices(request):
    payload, error = _du_payload_or_error(request)
    if error:
        return error
    return Response(
        {
            "summary": payload["wan"]["summary"],
            "rows": payload["wan"]["rows"],
            "meta": payload["meta"],
            "warnings": payload["warnings"],
            "deprecated": True,
        }
    )


def _add_traffic(bucket, tx_bytes, rx_bytes):
    bucket["sent"] += tx_bytes or 0
    bucket["received"] += rx_bytes or 0
    bucket["total"] = bucket["sent"] + bucket["received"]


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def data_usage_all_devices(request):
    payload, error = _du_payload_or_error(request)
    if error:
        return error
    response = dict(payload["summary"])
    response["meta"] = payload["meta"]
    response["warnings"] = payload["warnings"]
    response["deprecated"] = True
    return Response(response)

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def mobile_distribution_all_devices(request):
    payload, error = _du_payload_or_error(request)
    if error:
        return error
    response = dict(payload["mobile"])
    response["meta"] = payload["meta"]
    response["warnings"] = payload["warnings"]
    response["deprecated"] = True
    return Response(response)



@api_view(["GET"])
@permission_classes([IsAuthenticated])
def global_all_apps(request):
    payload, error = _du_payload_or_error(request)
    if error:
        return error
    return Response(
        {
            "applications": [
                {"name": item["label"], "value": item["traffic"]}
                for item in payload["apps"]["all_apps"]
            ],
            "meta": payload["meta"],
            "warnings": payload["warnings"],
            "deprecated": True,
        }
    )
