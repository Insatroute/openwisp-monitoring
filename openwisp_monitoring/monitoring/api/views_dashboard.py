import logging

from rest_framework import generics, status
from rest_framework.response import Response
from swapper import load_model

from openwisp_monitoring.db import timeseries_db
from openwisp_monitoring.monitoring.services import (
    DataUsageValidationError,
    get_data_usage_payload_for_request,
)
from openwisp_users.api.mixins import FilterByOrganizationMembership, ProtectedAPIMixin

DeviceData = load_model("device_monitoring", "DeviceData")

logger = logging.getLogger(__name__)

_PERIOD_TO_INFLUX = {
    '1d': '1d', '3d': '3d', '7d': '7d',
    '30d': '30d', '365d': '365d',
}


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
        period = request.query_params.get('period', '24h')
        influx_interval = _PERIOD_TO_INFLUX.get(period, '1d')
        limit = min(int(request.query_params.get('limit', 10) or 10), 50)

        try:
            return self._influx_top_devices(request, influx_interval, limit)
        except Exception:
            logger.debug(
                'GlobalTopDevicesView: InfluxDB failed, falling back',
                exc_info=True,
            )
            # Fallback to original build_data_usage_payload path
            payload, error = _payload_or_error(request)
            if error:
                return error
            top_devices = []
            for item in payload["top_devices"]:
                total_bytes = int(item.get("total_bytes", 0))
                top_devices.append({
                    "device_id": item.get("device_id"),
                    "name": item.get("name") or item.get("hostname") or "",
                    "total_bytes": total_bytes,
                    "total_gb": round(total_bytes / (1024 ** 3), 3),
                })
            return Response({
                "top_10_devices": top_devices,
                "meta": payload["meta"],
                "warnings": payload["warnings"],
            })

    def _influx_top_devices(self, request, influx_interval, limit):
        where_parts = [f"time > now() - {influx_interval}"]

        # Org scoping for non-superusers
        if not request.user.is_superuser:
            org_ids = list(
                request.user.organizations.values_list('id', flat=True)
            )
            if org_ids:
                org_regex = '|'.join(str(oid) for oid in org_ids)
                where_parts.append(
                    f"organization_id =~ /^({org_regex})$/"
                )

        where_clause = ' AND '.join(where_parts)
        query = (
            f'SELECT SUM("rx_bytes") AS total_down, SUM("tx_bytes") AS total_up '
            f'FROM "traffic" '
            f'WHERE {where_clause} '
            f'GROUP BY "object_id"'
        )
        result = timeseries_db.query(query)

        device_traffic = {}
        for key, points in result.items():
            tags = key[1] if len(key) > 1 else {}
            object_id = tags.get('object_id', '')
            if not object_id:
                continue
            for pt in points:
                rx = pt.get('total_down') or 0
                tx = pt.get('total_up') or 0
                device_traffic[object_id] = {
                    'rx': rx, 'tx': tx, 'total': rx + tx,
                }

        # Resolve device names
        device_ids = list(device_traffic.keys())
        name_map = {}
        if device_ids:
            try:
                from openwisp_controller.config.models import Device
                qs = Device.objects.filter(
                    id__in=device_ids
                ).values_list('id', 'name')
                name_map = {str(did): dname for did, dname in qs}
            except Exception:
                pass

        devices_list = []
        for did, traffic in device_traffic.items():
            if traffic['total'] <= 0:
                continue
            # Skip deleted devices (not in Django)
            if did not in name_map:
                continue
            devices_list.append({
                'device_id': did,
                'name': name_map[did],
                'total_bytes': traffic['total'],
                'total_gb': round(traffic['total'] / (1024 ** 3), 3),
            })

        devices_list.sort(key=lambda x: x['total_bytes'], reverse=True)
        return Response({'top_10_devices': devices_list[:limit]})


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
