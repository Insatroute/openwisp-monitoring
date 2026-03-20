from rest_framework import generics, status
from rest_framework.response import Response
from swapper import load_model

from openwisp_monitoring.monitoring.services import (
    DataUsageValidationError,
    get_data_usage_payload_for_request,
)
from openwisp_users.api.mixins import FilterByOrganizationMembership, ProtectedAPIMixin

DeviceData = load_model("device_monitoring", "DeviceData")


def _payload_or_error(request):
    try:
        return get_data_usage_payload_for_request(request), None
    except DataUsageValidationError as exc:
        return None, Response(
            {"detail": str(exc), "code": "invalid_period"},
            status=status.HTTP_400_BAD_REQUEST,
        )


class GlobalTopAppsView(ProtectedAPIMixin, FilterByOrganizationMembership, generics.GenericAPIView):
    queryset = DeviceData.objects.all()
    organization_field = "organization"

    def get(self, request, *args, **kwargs):
        payload, error = _payload_or_error(request)
        if error:
            return error
        return Response(
            {
                "top_10_apps": payload["apps"]["top_apps"],
                "meta": payload["meta"],
                "warnings": payload["warnings"],
            }
        )


class GlobalTopDevicesView(ProtectedAPIMixin, FilterByOrganizationMembership, generics.GenericAPIView):
    queryset = DeviceData.objects.all()
    organization_field = "organization"

    def get(self, request, *args, **kwargs):
        payload, error = _payload_or_error(request)
        if error:
            return error
        top_devices = []
        for item in payload["top_devices"]:
            total_bytes = int(item.get("total_bytes", 0))
            top_devices.append(
                {
                    "device_id": item.get("device_id"),
                    "name": item.get("name") or item.get("hostname") or "",
                    "total_bytes": total_bytes,
                    "total_gb": round(total_bytes / (1024 ** 3), 3),
                }
            )
        return Response(
            {
                "top_10_devices": top_devices,
                "meta": payload["meta"],
                "warnings": payload["warnings"],
            }
        )


class WanUplinksAllDevicesView(ProtectedAPIMixin, FilterByOrganizationMembership, generics.GenericAPIView):
    queryset = DeviceData.objects.select_related("monitoring").all()
    organization_field = "organization"

    def get(self, request, *args, **kwargs):
        payload, error = _payload_or_error(request)
        if error:
            return error
        return Response(
            {
                "summary": payload["wan"]["summary"],
                "rows": payload["wan"]["rows"],
                "meta": payload["meta"],
                "warnings": payload["warnings"],
            }
        )


class DataUsageAllDevicesView(ProtectedAPIMixin, FilterByOrganizationMembership, generics.GenericAPIView):
    queryset = DeviceData.objects.all()
    organization_field = "organization"

    def get(self, request, *args, **kwargs):
        payload, error = _payload_or_error(request)
        if error:
            return error
        response = dict(payload["summary"])
        response["meta"] = payload["meta"]
        response["warnings"] = payload["warnings"]
        return Response(response)


class MobileDistributionAllDevicesView(
    ProtectedAPIMixin,
    FilterByOrganizationMembership,
    generics.GenericAPIView,
):
    queryset = DeviceData.objects.all()
    organization_field = "organization"

    def get(self, request, *args, **kwargs):
        payload, error = _payload_or_error(request)
        if error:
            return error
        response = dict(payload["mobile"])
        response["meta"] = payload["meta"]
        response["warnings"] = payload["warnings"]
        return Response(response)


class IPSecTunnelsStatusView(ProtectedAPIMixin, FilterByOrganizationMembership, generics.GenericAPIView):
    """
    IPSec tunnel status for all devices in the user's organizations.
    Manager-only access.
    """

    # NOTE: we can safely prefetch `monitoring`, NOT `device`
    queryset = DeviceData.objects.select_related("monitoring").all()
    organization_field = "organization"

    def get(self, request, *args, **kwargs):
        device_data_qs = self.get_queryset()

        summary = {
            "total": 0,
            "connected": 0,
            "disconnected": 0,
        }
        rows = []

        for dd in device_data_qs:
            # Get IPSec tunnel data from monitoring JSON
            data = getattr(dd, "data_user_friendly", {}) or {}
            ipsec_data = data.get("ipsec", {}).get("data", {}).get("tunnels", {}).get("tunnels", [])

            for tunnel in ipsec_data:
                tunnel_name = tunnel.get("name", "")
                tunnel_id = tunnel.get("id", "")
                is_connected = str(tunnel.get("connected", "false")).lower() == "true"
                tunnel_status = "connected" if is_connected else "disconnected"

                
                summary["total"] += 1
                summary[tunnel_status] += 1

                # Collect relevant information
                rows.append({
                    "device_id": str(getattr(dd, "id", "")),
                    "tunnel_name": tunnel_name,
                    "tunnel_id": tunnel_id,
                    "status": tunnel_status,
                    "local_network": tunnel.get("local", ""),
                    "remote_network": tunnel.get("remote", ""),
                })

        return Response({"summary": summary, "rows": rows})



global_top_apps = GlobalTopAppsView.as_view()
global_top_devices = GlobalTopDevicesView.as_view()
wan_uplinks_all_devices = WanUplinksAllDevicesView.as_view()
data_usage_all_devices = DataUsageAllDevicesView.as_view()
mobile_distribution_all_devices = MobileDistributionAllDevicesView.as_view()
ipsec_tunnels_status = IPSecTunnelsStatusView.as_view()
