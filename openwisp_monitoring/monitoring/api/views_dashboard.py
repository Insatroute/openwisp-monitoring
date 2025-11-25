from collections import Counter
from swapper import load_model
from rest_framework import generics
from rest_framework.response import Response
from openwisp_users.api.mixins import (
    ProtectedAPIMixin,
    FilterByOrganizationMembership,
)
from openwisp_users.api.permissions import IsOrganizationMember, DjangoModelPermissions


Device = load_model("config", "Device")
DeviceData = load_model("device_monitoring", "DeviceData")
DeviceLocation = load_model("geo", "DeviceLocation")
# -------------------------------------------------------------------
# Helper functions
# -------------------------------------------------------------------
def _ipv4_addr_mask(iface: dict):
    ipv4 = next(
        (a for a in iface.get("addresses", []) if a.get("family") == "ipv4"),
        None,
    )
    if not ipv4:
        return None, None
    return ipv4.get("address"), ipv4.get("mask")


def _link_status(iface: dict) -> str:
    return "connected" if iface.get("up") else "disconnected"


def _add_traffic(bucket, tx_bytes, rx_bytes):
    bucket["sent"] += tx_bytes or 0
    bucket["received"] += rx_bytes or 0
    bucket["total"] = bucket["sent"] + bucket["received"]


# -------------------------------------------------------------------
# Views: all org-filtered via FilterByOrganizationMembership
# and protected via ProtectedAPIMixin
# -------------------------------------------------------------------

class GlobalTopAppsView(
    ProtectedAPIMixin,
    FilterByOrganizationMembership,
    generics.GenericAPIView,
):
    """
    Top 10 applications across allowed devices.
    Org lookup uses Device.organization (via device__organization).
    """

    queryset = DeviceData.objects.all()
    organization_field = "organization"

    def get(self, request, *args, **kwargs):
        app_counter = Counter()

        # queryset already org-filtered by FilterByOrganizationMembership
        for device_data in self.get_queryset():
            data = device_data.data_user_friendly or {}
            apps = (
                data.get("realtimemonitor", {})
                .get("traffic", {})
                .get("dpi_summery_v2", {})
                .get("applications", [])
            )

            for app in apps:
                app_id = app.get("id") or ""
                if app_id in ("netify.nethserver", "netify.snort", "netify.netify"):
                    continue

                label = app.get("label")
                traffic = app.get("traffic", 0)

                if label:
                    app_counter[label] += traffic

        top_10_apps = [
            {"label": label.capitalize(), "traffic": traffic}
            for label, traffic in app_counter.most_common(10)
        ]
        return Response({"top_10_apps": top_10_apps})

class GlobalTopDevicesView(
    ProtectedAPIMixin,
    FilterByOrganizationMembership,
    generics.GenericAPIView,
):
    """
    Top 10 devices by total rx+tx bytes (overall project, but scoped by org).
    """
    # IMPORTANT: DeviceData itself has organization, so no select_related('device')
    queryset = DeviceData.objects.all()

    # FilterByOrganizationMembership will filter on this field
    organization_field = "organization"

    def get(self, request, *args, **kwargs):
        devices = []

        # get_queryset() already applies org filter according to user
        for obj in self.get_queryset():
            data = getattr(obj, "data_user_friendly", None) or {}
            general = data.get("general") or {}
            interfaces = data.get("interfaces") or []

            total_rx = 0
            total_tx = 0

            for iface in interfaces:
                stats = iface.get("statistics") or {}
                total_rx += stats.get("rx_bytes") or 0
                total_tx += stats.get("tx_bytes") or 0

            total_bytes = total_rx + total_tx

            # Try hostname from monitoring first, then model fields
            name = (
                general.get("hostname")
                or getattr(obj, "name", "")
                or getattr(obj, "serial_number", "")
                or str(obj.pk)
            )

            devices.append(
                {
                    "device_id": str(obj.pk),
                    "name": name,
                    "total_bytes": int(total_bytes),
                    "total_gb": round(total_bytes / (1024 ** 3), 3),
                }
            )

        # Sort by total traffic (desc) and take top 10
        devices.sort(key=lambda d: d["total_bytes"], reverse=True)
        top_10 = devices[:10]

        return Response({"top_10_devices": top_10})
    
class WanUplinksAllDevicesView(
    ProtectedAPIMixin,
    FilterByOrganizationMembership,
    generics.GenericAPIView,
):
    """
    WAN uplink status for all devices in the user's organizations.
    Manager-only access.
    """

    queryset = DeviceData.objects.select_related("device").all()
    organization_field = "organization"

    def get(self, request, *args, **kwargs):
        device_data_qs = self.get_queryset()

        summary = {
            "total": 0,
            "connected": 0,
            "abnormal": 0,
            "disconnected": 0,
        }

        rows = []

        for dd in device_data_qs:
            device = dd.device  # real device model
            data = dd.data_user_friendly or {}

            general = data.get("general", {}) or {}
            hostname = general.get("hostname") or device.name or ""
            serialnumber = general.get("serialnumber") or device.serial_number or ""
            interfaces = data.get("interfaces", []) or []

            # get location from Device model, not DeviceData
            dl = (
                DeviceLocation.objects
                .filter(content_object_id=device.id)
                .select_related("location")
                .first()
            )
            location_name = dl.location.name if dl and dl.location else "-"

            for iface in interfaces:
                if not (iface.get("type") == "ethernet" and iface.get("is_wan") is True):
                    continue

                status = _link_status(iface)

                summary["total"] += 1
                summary[status] += 1

                ipv4_addr, ipv4_mask = _ipv4_addr_mask(iface)
                ping = iface.get("ping") or {}
                throughput = ping.get("throughput") or {}

                rows.append({
                    "device_id": str(device.id),
                    "hostname": hostname,
                    "serial_number": serialnumber,
                    "model": device.model or "",
                    "location": location_name,
                    "path_label": getattr(device, "wan_path_label", ""),
                    "interface_name": iface.get("name"),
                    "uplink_type": iface.get("type"),

                    "interface_ip": ipv4_addr,
                    "interface_mask": ipv4_mask,

                    "throughput_tx_bytes": throughput.get("tx_bytes"),
                    "throughput_rx_bytes": throughput.get("rx_bytes"),

                    "ping_dest": ping.get("dest_ip"),
                    "ping_latency_ms": ping.get("latency_ms"),
                    "ping_packet_loss": ping.get("packet_loss"),
                    "ping_jitter_ms": ping.get("jitter_ms"),

                    "status": status,
                })

        return Response({
            "summary": summary,
            "rows": rows,
        })

class DataUsageAllDevicesView(
    ProtectedAPIMixin,
    FilterByOrganizationMembership,
    generics.GenericAPIView,
):
    queryset = DeviceData.objects.all()
    organization_field = "organization"

    def get(self, request, *args, **kwargs):
        summary = {
            "total": {"sent": 0, "received": 0, "total": 0},
            "cellular": {"sent": 0, "received": 0, "total": 0},
            "wired": {"sent": 0, "received": 0, "total": 0},
            "wireless": {"sent": 0, "received": 0, "total": 0},
        }

        for device_data in self.get_queryset():
            data = getattr(device_data, "data_user_friendly", {}) or {}

            interfaces = data.get("interfaces", []) or []

            for iface in interfaces:
                stats = iface.get("statistics") or {}
                tx = stats.get("tx_bytes") or 0
                rx = stats.get("rx_bytes") or 0

                iface_type = iface.get("type")

                if iface_type == "mobile":
                    _add_traffic(summary["cellular"], tx, rx)
                elif iface_type == "ethernet" and iface.get("is_wan") is True:
                    _add_traffic(summary["wired"], tx, rx)
                elif iface_type in ("wifi", "wireless"):
                    _add_traffic(summary["wireless"], tx, rx)

        for key in ("cellular", "wired", "wireless"):
            _add_traffic(summary["total"], summary[key]["sent"], summary[key]["received"])

        return Response(summary)

class MobileDistributionAllDevicesView(
    ProtectedAPIMixin,
    FilterByOrganizationMembership,
    generics.GenericAPIView,
):
    queryset = DeviceData.objects.all()
    organization_field = "organization"

    def get(self, request, *args, **kwargs):
        carrier_counter = Counter()
        network_counter = Counter()
        total_modems = 0

        for device_data in self.get_queryset():
            data = getattr(device_data, "data_user_friendly", {}) or {}

            interfaces = data.get("interfaces", []) or []

            for iface in interfaces:
                if iface.get("type") != "mobile":
                    continue

                mobile = iface.get("mobile", {}) or {}
                total_modems += 1

                operator = mobile.get("operator_name") or "Unknown"
                carrier_counter[operator] += 1

                signal = mobile.get("signal") or {}
                if "5g" in signal:
                    network_counter["5G"] += 1
                elif "lte" in signal:
                    network_counter["4G"] += 1
                elif "3g" in signal:
                    network_counter["3G"] += 1
                else:
                    network_counter["Unknown"] += 1

        return Response({
            "carrier": {
                "labels": list(carrier_counter.keys()),
                "data": list(carrier_counter.values()),
            },
            "network": {
                "labels": list(network_counter.keys()),
                "data": list(network_counter.values()),
            },
            "total_modems": total_modems,
        })

global_top_apps = GlobalTopAppsView.as_view()
global_top_devices = GlobalTopDevicesView.as_view()
wan_uplinks_all_devices = WanUplinksAllDevicesView.as_view()
data_usage_all_devices = DataUsageAllDevicesView.as_view()
mobile_distribution_all_devices = MobileDistributionAllDevicesView.as_view()

