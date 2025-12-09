from collections import Counter
from swapper import load_model
from rest_framework import generics
from rest_framework.response import Response
from openwisp_users.api.mixins import (
    ProtectedAPIMixin,
    FilterByOrganizationMembership,
)
from openwisp_users.api.permissions import IsOrganizationMember, DjangoModelPermissions
from openwisp_monitoring.device.base.models import UP_STATUSES


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


def _link_status(device, iface: dict) -> str:
    monitoring = getattr(device, "monitoring", None)
    live_is_up = bool(
        monitoring and getattr(monitoring, "status", None) in UP_STATUSES
    )
    if not live_is_up:
        return "disconnected"
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

    # NOTE: we can safely prefetch `monitoring`, NOT `device`
    queryset = DeviceData.objects.select_related("monitoring").all()
    organization_field = "organization"

    def get(self, request, *args, **kwargs):
        device_data_qs = self.get_queryset()

        summary = {
            "total": 0,
            "connected": 0,
            "abnormal": 0,   # kept for compatibility, even if we don't use it now
            "disconnected": 0,
        }
        rows = []

        for dd in device_data_qs:
            # monitoring JSON
            data = getattr(dd, "data_user_friendly", {}) or {}

            general = data.get("general", {}) or {}
            interfaces = data.get("interfaces", []) or []

            # try to reach actual Device via monitoring.device, if present
            monitoring_obj = getattr(dd, "monitoring", None)
            device = getattr(monitoring_obj, "device", None)

            hostname = (
                general.get("hostname")
                or (getattr(device, "name", "") if device else "")
                or (getattr(device, "serial_number", "") if device else "")
                or ""
            )
            serialnumber = (
                general.get("serialnumber")
                or (getattr(device, "serial_number", "") if device else "")
                or ""
            )

            # location via Device if we have it
            location_name = "-"
            if device is not None:
                dl = (
                    DeviceLocation.objects
                    .filter(content_object_id=device.id)
                    .select_related("location")
                    .first()
                )
                if dl and dl.location:
                    location_name = dl.location.name

            for iface in interfaces:
                itype = (iface.get("type") or "").lower()

                # allow WAN ethernet + all mobile interfaces
                allowed = (
                    (itype == "ethernet" and iface.get("is_wan") is True) or
                    (itype == "mobile")
                )

                if not allowed:
                    continue
                
                raw_name = (iface.get("name") or "").lower()
                
                if itype == "mobile":
                    if raw_name == "modem":
                        display_name = "Cellular1"
                    elif raw_name == "modem2":
                        display_name = "Cellular2"
                    else:
                        display_name = "Cellular"
                    display_uplink_type = "cellular"
                else:
                    display_name = iface.get("name")
                    display_uplink_type = iface.get("type")

                status = _link_status(device, iface)

                summary["total"] += 1
                summary[status] += 1

                ipv4_addr, ipv4_mask = _ipv4_addr_mask(iface)
                ping = iface.get("ping") or {}
                throughput = ping.get("throughput") or {}

                rows.append({
                    "device_id": str(getattr(device, "id", dd.id)),
                    "hostname": hostname,
                    "serial_number": serialnumber,
                    "model": getattr(device, "model", "") if device else "",
                    "location": location_name,
                    "path_label": getattr(device, "wan_path_label", "") if device else "",
                    "interface_name": display_name,
                    "uplink_type": display_uplink_type,

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

        return Response({"summary": summary, "rows": rows})

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
    
def normalize_operator_name(raw: str) -> str:
    if not raw:
        return "Unknown"

    name = raw.strip()
    upper = name.upper()

    # Jio
    if "JIO" in upper:
        return "Jio"

    # Airtel
    if "AIRTEL" in upper:
        return "Airtel"

    # Vi (Vodafone-Idea)
    if "VI " in upper or upper.startswith("VI") or "VODAFONE" in upper or "IDEA" in upper:
        return "Vi India"

    # BSNL
    if "BSNL" in upper:
        return "BSNL"

    # default: pretty normalized text
    return " ".join(part.capitalize() for part in name.split())


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

                # ✅ normalize operator name here
                raw_operator = mobile.get("operator_name") or "Unknown"
                operator = normalize_operator_name(raw_operator)
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
                tunnel_status = "connected" if tunnel.get("connected", "true") == "true" else "disconnected"
                
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

