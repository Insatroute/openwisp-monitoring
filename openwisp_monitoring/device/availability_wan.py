from datetime import datetime
from django.utils.timezone import now as dj_now

from django.utils.timezone import get_current_timezone
from dateutil import parser as dp

from ..monitoring.tasks import _timeseries_write
from db import timeseries_db
from django.utils.timezone import now as dj_now


AVAILABILITY_RP = "autogen"     # same as device_status
WAN_MEASUREMENT = "wan_link_status"


def record_wan_link_status(device_id, ifname, is_up, timestamp=None):
    """
    Store a single WAN link status event in InfluxDB.

    measurement: wan_link_status
    tags:
        pk      -> device_id
        ifname  -> interface name (e.g. 'eth0', 'pppoe-wan1')
    fields:
        up      -> 1 or 0
    """
    ts = timestamp or dj_now()
    try:
        _timeseries_write(
            name=WAN_MEASUREMENT,
            values={"up": 1 if is_up else 0},
            tags={"pk": str(device_id), "ifname": str(ifname)},
            timestamp=ts,
            retention_policy=AVAILABILITY_RP,
        )
    except Exception:
        # MUST NOT break flow if Influx is down
        pass
