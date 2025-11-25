from collections import Counter
from datetime import timedelta
from swapper import load_model
from django.utils import timezone
from rest_framework import generics
from rest_framework.response import Response
from openwisp_users.api.mixins import (
    ProtectedAPIMixin,
    FilterByOrganizationMembership,
)
from openwisp_users.api.permissions import IsOrganizationMember, DjangoModelPermissions

from .views_realdata import fetch_device_data

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
    organization_field = "device__organization"

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


# class GlobalTopDevicesView(
#     ProtectedAPIMixin,
#     FilterByOrganizationMembership,
#     generics.GenericAPIView,
# ):
#     """
#     Top 10 devices based on total rx/tx bytes across all interfaces.
#     Uses DeviceData → Device.organization for org filtering.
#     """

#     queryset = DeviceData.objects.all()
#     organization_field = "device__organization"

#     def get(self, request, *args, **kwargs):
#         devices = []

#         for device_data in self.get_queryset():
#             data = device_data.data_user_friendly or {}
#             general = data.get("general", {})
#             interfaces = data.get("interfaces", [])

#             total_rx = total_tx = 0
#             for iface in interfaces:
#                 stats = iface.get("statistics") or {}
#                 total_rx += stats.get("rx_bytes", 0)
#                 total_tx += stats.get("tx_bytes", 0)

#             total_traffic = total_rx + total_tx
#             hostname = general.get("hostname") or "Unknown"

#             devices.append(
#                 {
#                     "device": hostname,
#                     "total_bytes": total_traffic,
#                     "total_gb": round(total_traffic / (1024 ** 3), 3),
#                 }
#             )

#         top_devices = sorted(devices, key=lambda d: d["total_bytes"], reverse=True)[:10]
#         return Response({"top_10_devices": top_devices})

class GlobalTopDevicesView(
    ProtectedAPIMixin,
    FilterByOrganizationMembership,
    generics.GenericAPIView,
):
    """
    Replacement for old top_devices_simple, but:
      - class-based
      - org + permission handled by ProtectedAPIMixin + FilterByOrganizationMembership
      - no direct use of user.organizations
    """

    queryset = Device.objects.all()
    # FilterByOrganizationMembership will use organization_field by default = "organization"
    organization_field = "organization"
    # allow members (not only managers); change to IsOrganizationManager if you want stricter
    permission_classes = (IsOrganizationMember, DjangoModelPermissions)

    def get(self, request, *args, **kwargs):
        # org param (optional now, defaults to ALL)
        org_param = (request.GET.get("org") or request.GET.get("organization_slug") or "ALL").strip()
        all_orgs = org_param.lower() in ("all", "*", "")

        # flags
        include_all = _parse_bool(request.GET.get("include_all"), False)
        include_org = _parse_bool(request.GET.get("include_org"), False)

        # time window
        time_arg, start, end = _parse_window(
            request.GET.get("time"),
            request.GET.get("start"),
            request.GET.get("end"),
        )

        # limit
        limit_raw = request.GET.get("limit", "5")
        if isinstance(limit_raw, str) and limit_raw.lower() in ("all", "*", "0"):
            limit = 10**9
            limit_label = "all"
        else:
            try:
                limit = max(1, min(int(limit_raw), 5000))
                limit_label = limit
            except Exception:
                return Response({"detail": "Invalid 'limit'."}, status=400)

        # ifnames
        ifnames_str = request.GET.get("wan_ifs") or ""
        ifnames = [x.strip() for x in ifnames_str.split(",") if x.strip()] or None

        # user-aware cache key (superuser can share, normal users isolated)
        if request.user.is_superuser:
            user_key = "su"
        else:
            user_key = f"user:{request.user.pk}"

        ck = (
            f"td:{user_key}:ALL:{all_orgs}:org={org_param}:"
            f"t={time_arg}:s={start}:e={end}:"
            f"ifs={','.join(ifnames) if ifnames else 'ALL'}:"
            f"lim={limit_label}:incall={int(include_all)}:incorg={int(include_org)}"
        )
        cached = cache.get(ck)
        if cached:
            return Response(cached)

        # --------- get Device queryset with org + user restrictions via mixin ----------
        try:
            qs = self.get_queryset()  # FilterByOrganizationMembership already applied

            # extra filter by org slug if org != ALL
            if not all_orgs:
                qs = qs.filter(organization__slug=org_param)

            fields = ["id", "name"]
            if include_org or all_orgs:
                fields.append("organization__slug")

            devices = list(qs.values(*fields))
        except Exception as e:
            return Response({"detail": f"Error reading devices: {e}"}, status=500)
        # -------------------------------------------------------------------------------

        # sum totals per device from Influx
        results: List[Dict[str, Any]] = []
        for d in devices:
            dev_id = str(d["id"])
            try:
                total = _query_total(dev_id, ifnames, time_arg, start, end)
            except Exception:
                total = 0

            item = {
                "device_id": dev_id,
                "name": d.get("name") or dev_id,
                "total_bytes": int(total or 0),
                "total_gb": round((total or 0) / (1024**3), 3),
            }
            if include_org or all_orgs:
                item["organization"] = d.get("organization__slug", None)
            results.append(item)

        # sort by total desc
        results.sort(key=lambda x: x["total_bytes"], reverse=True)

        payload = {
            "org": "ALL" if all_orgs else org_param,
            "window": {"time": time_arg, "start": start, "end": end},
            "limit": limit_label,
            "interface_scope": ",".join(ifnames) if ifnames else "ALL",
            "count_devices": len(results),
            "top": results[:limit],
            "note": (
                f'Read from InfluxDB {"v2" if INF_V2 else "v1"} '
                f'({INF_DB if not INF_V2 else INF_V2_BUCKET}; '
                f'measurement="{MEASUREMENT}"; fields="{"+".join(FIELDS)}"). '
                f'Use wan_ifs to avoid bridge double-counting.'
            ),
        }
        if include_all:
            payload["devices"] = results

        cache.set(ck, payload, 60)
        return Response(payload)
class WanUplinksAllDevicesView(
    ProtectedAPIMixin,
    FilterByOrganizationMembership,
    generics.GenericAPIView,
):
    """
    WAN uplink status for all devices in the user's organizations.
    Org filtering is on Device.organization (default organization_field).
    """

    queryset = Device.objects.all()
    # organization_field defaults to "organization" via OrgLookup

    def get(self, request, *args, **kwargs):
        devices = self.get_queryset()

        summary = {
            "total": 0,
            "connected": 0,
            "abnormal": 0,  # kept for compatibility
            "disconnected": 0,
        }
        rows = []

        for device in devices:
            try:
                data = fetch_device_data(device)
            except Exception:
                # skip devices we cannot fetch
                continue

            general = data.get("general", {}) or {}
            hostname = general.get("hostname") or getattr(device, "hostname", "")
            serialnumber = general.get("serialnumber") or getattr(device, "serialnumber", "")
            interfaces = data.get("interfaces", []) or []

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

                rows.append(
                    {
                        "device_id": device.pk,
                        "hostname": hostname,
                        "serial_number": serialnumber,
                        "model": getattr(device, "model", ""),
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
                    }
                )

        return Response({"summary": summary, "rows": rows})


class DataUsageAllDevicesView(
    ProtectedAPIMixin,
    FilterByOrganizationMembership,
    generics.GenericAPIView,
):
    """
    Total data usage for devices in user's organizations,
    split by cellular / wired / wireless.
    """

    queryset = Device.objects.all()
    # org filter on Device.organization

    def get(self, request, *args, **kwargs):
        summary = {
            "total": {"sent": 0, "received": 0, "total": 0},
            "cellular": {"sent": 0, "received": 0, "total": 0},
            "wired": {"sent": 0, "received": 0, "total": 0},
            "wireless": {"sent": 0, "received": 0, "total": 0},
        }

        for device in self.get_queryset():
            try:
                data = fetch_device_data(device)
            except Exception:
                continue

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
                else:
                    continue

        for key in ("cellular", "wired", "wireless"):
            _add_traffic(
                summary["total"],
                summary[key]["sent"],
                summary[key]["received"],
            )

        return Response(summary)


class MobileDistributionAllDevicesView(
    ProtectedAPIMixin,
    FilterByOrganizationMembership,
    generics.GenericAPIView,
):
    """
    Carrier and network-type distribution for mobile interfaces
    on devices in the user's organizations.
    """

    queryset = Device.objects.all()
    # org filter on Device.organization

    def get(self, request, *args, **kwargs):
        carrier_counter = Counter()
        network_counter = Counter()
        total_modems = 0

        for device in self.get_queryset():
            try:
                data = fetch_device_data(device)
            except Exception:
                continue

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

        return Response(
            {
                "carrier": {
                    "labels": list(carrier_counter.keys()),
                    "data": list(carrier_counter.values()),
                },
                "network": {
                    "labels": list(network_counter.keys()),
                    "data": list(network_counter.values()),
                },
                "total_modems": total_modems,
            }
        )

global_top_apps = GlobalTopAppsView.as_view()
global_top_devices = GlobalTopDevicesView.as_view()
wan_uplinks_all_devices = WanUplinksAllDevicesView.as_view()
data_usage_all_devices = DataUsageAllDevicesView.as_view()
mobile_distribution_all_devices = MobileDistributionAllDevicesView.as_view()

