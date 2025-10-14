import json
import random
from collections import OrderedDict
from datetime import datetime, timedelta
from dateutil import parser as dp

import swapper
from cache_memoize import cache_memoize
from dateutil.relativedelta import relativedelta
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models.signals import post_delete
from django.dispatch import receiver
from django.utils.timezone import now as dj_now, get_current_timezone
from django.utils.translation import gettext_lazy as _
from jsonschema import draft7_format_checker, validate
from jsonschema.exceptions import ValidationError as SchemaError
from model_utils import Choices
from model_utils.fields import StatusField
from netaddr import EUI, NotRegisteredError
from pytz import timezone as tz  # still used for a few epoch conversions
from swapper import load_model

from openwisp_controller.config.validators import mac_address_validator
from openwisp_monitoring.device.settings import get_critical_device_metrics
from openwisp_utils.base import TimeStampedEditableModel

from ...db import device_data_query, timeseries_db
from ...monitoring.signals import threshold_crossed
from ...monitoring.tasks import _timeseries_write
from ...settings import CACHE_TIMEOUT
from .. import settings as app_settings
from .. import tasks
from ..schema import schema, tunnel_monitoring_schema
from ..signals import health_status_changed
from ..utils import SHORT_RP, get_device_cache_key

# --- Availability / uptime config ---
AVAILABILITY_RP = 'autogen'
UP_STATUSES = {'ok', 'problem'}

# --- Record up/down events on every health status change ---
@receiver(health_status_changed, dispatch_uid='record_device_availability_ts')
def record_device_availability_ts(sender, instance, status, **kwargs):
    """Write one point to TSDB whenever device health changes."""
    try:
        up = 1 if status in UP_STATUSES else 0
        _timeseries_write(
            name='device_status',
            values={'up': up},
            tags={'pk': instance.device_id},
            timestamp=dj_now(),                # use Django timezone-aware now()
            retention_policy=AVAILABILITY_RP,
        )
    except Exception:
        # TSDB failure must not break app flow
        pass


# ------------------------- Utilities -------------------------

def _to_dt(t):
    """
    Accept epoch seconds (int/float) or ISO strings and return aware datetime.
    We do NOT force-convert to any particular tz here; we just parse.
    """
    if isinstance(t, (int, float)):
        # Interpret as epoch seconds; create aware dt in current Django tz
        return datetime.fromtimestamp(t, tz=get_current_timezone())
    return dp.parse(t)


def _fmt_duration_short(seconds: float) -> str:
    """Return compact duration like '6h 40m', '2m', or '23s'."""
    seconds = int(max(0, round(seconds)))
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    parts = []
    if h:
        parts.append(f"{h}h")
    if m:
        parts.append(f"{m}m")
    if not h and not m:
        parts.append(f"{s}s")
    return " ".join(parts) if parts else "0s"


def _fmt_in_current_tz(dt):
    """
    Format a datetime into 'YYYY-MM-DD HH:MM:SS' using Django's current timezone.
    No hard-coded tz; respects settings.TIME_ZONE.
    """
    tzinfo = get_current_timezone()
    try:
        dt_local = dt.astimezone(tzinfo)
    except Exception:
        # if dt is naive, assume it's already in the correct tz
        dt_local = dt
    return dt_local.strftime("%Y-%m-%d %H:%M:%S")


def _build_friendly_intervals(events):
    """
    Build stitched intervals with durations from ordered events.
    NOTE: events[i]["time"] are already strings in the configured Django timezone.
    """
    intervals = []
    total_up = 0.0
    total_down = 0.0
    longest_outage = {
        "duration_seconds": 0,
        "duration_human": "0s",
        "start": None,
        "end": None,
    }

    for i in range(len(events) - 1):
        start_s = events[i]["time"]
        end_s = events[i + 1]["time"]
        status = events[i]["status"]

        start_dt_local = dp.parse(start_s)
        end_dt_local = dp.parse(end_s)
        delta = (end_dt_local - start_dt_local).total_seconds()
        delta = max(0.0, delta)

        dur_human = _fmt_duration_short(delta)
        item = {
            "start": start_s,  # display string
            "end": end_s,      # display string
            "status": status,  # 'up' / 'down'
            "duration_seconds": int(round(delta)),
            "duration_human": dur_human,
            "status_label": "Up" if status == "up" else "Down",
        }
        intervals.append(item)

        if status == "up":
            total_up += delta
        else:
            total_down += delta
            if delta > longest_outage["duration_seconds"]:
                longest_outage = {
                    "duration_seconds": int(round(delta)),
                    "duration_human": dur_human,
                    "start": start_s,
                    "end": end_s,
                }

    return {
        "intervals": intervals,
        "totals": {
            "uptime_seconds": int(round(total_up)),
            "uptime_human": _fmt_duration_short(total_up),
            "downtime_seconds": int(round(total_down)),
            "downtime_human": _fmt_duration_short(total_down),
            "longest_outage": longest_outage,
        },
    }


def _uptime_pct_from_events(device_id, start_dt, end_dt):
    """
    Compute uptime% between start_dt and end_dt using raw events.
    We keep the math consistent without forcing any specific tz conversion.
    """
    if start_dt >= end_dt:
        return 0.0
    start_iso = start_dt.isoformat()
    end_iso = end_dt.isoformat()

    q_prev = f'''
        SELECT LAST("up") AS up
        FROM "{AVAILABILITY_RP}"."device_status"
        WHERE "pk"='{device_id}' AND time <= '{start_iso}'
    '''
    prev = timeseries_db.get_list_query(q_prev) or []
    cur_up = int(prev[0]['up']) if prev and prev[0].get('up') is not None else 0

    q_events = f'''
        SELECT "up","time"
        FROM "{AVAILABILITY_RP}"."device_status"
        WHERE "pk"='{device_id}' AND time >= '{start_iso}' AND time < '{end_iso}'
        ORDER BY time ASC
    '''
    events = timeseries_db.get_list_query(q_events) or []

    t = start_dt
    up_seconds = 0.0
    for p in events:
        t1 = _to_dt(p['time'])
        if cur_up == 1:
            up_seconds += (t1 - t).total_seconds()
        cur_up = int(p['up'])
        t = t1

    if cur_up == 1:
        up_seconds += (end_dt - t).total_seconds()

    total = (end_dt - start_dt).total_seconds()
    return round((up_seconds / total) * 100.0, 2) if total > 0 else 0.0


def uptime_percentages_for_common_windows(device_id):
    end = dj_now()
    def ago(days=0, hours=0):
        return end - timedelta(days=days, hours=hours)
    return {
        '24h': _uptime_pct_from_events(device_id, ago(hours=24), end),
    }


# ---------------------- Availability API ----------------------

from django.conf import settings

def get_device_availability(
    device_id, *,
    start=None,
    end=None,
    days=0,
    hours=24,
    include_uptime=True,
    max_events=500,
    override_end_status=None,   # 'up' | 'down' | None
    display_tz=None,            # ignored; we always use Django's current tz
):
    # --- Compute window using Django's timezone-aware now() ---
    if end is None:
        end_dt = dj_now()
    else:
        end_dt = _to_dt(end)

    if start is None:
        start_dt = end_dt - timedelta(days=days, hours=hours)
    else:
        start_dt = _to_dt(start)

    tzname = getattr(get_current_timezone(), "zone", str(get_current_timezone()))

    if start_dt >= end_dt:
        return {
            "window": {
                "start": _fmt_in_current_tz(start_dt),
                "end": _fmt_in_current_tz(end_dt),
                "tz": tzname,
            },
            "events": [],
            "timeline": [],
            **({"uptime_percent": 0.0} if include_uptime else {}),
            "friendly": {
                "intervals": [],
                "summary": {
                    "total_uptime": "0s",
                    "total_downtime": "0s",
                    "longest_outage": {"duration_seconds": 0, "duration_human": "0s", "start": None, "end": None},
                    "uptime_percent": 0.0 if include_uptime else None,
                }
            }
        }

    start_iso = start_dt.isoformat()
    end_iso = end_dt.isoformat()
    device_id = str(device_id)

    # --- State at window start ---
    q_prev = f'''
        SELECT LAST("up") AS up
        FROM "{AVAILABILITY_RP}"."device_status"
        WHERE "pk"='{device_id}' AND time <= '{start_iso}'
    '''
    prev = timeseries_db.get_list_query(q_prev) or []
    cur_up = int(prev[0]['up']) if prev and prev[0].get('up') is not None else 0

    # --- Fetch the LAST `max_events` rows in the window (newest-first), then reverse ---
    q_events = f'''
        SELECT "up","time"
        FROM "{AVAILABILITY_RP}"."device_status"
        WHERE "pk"='{device_id}' AND time >= '{start_iso}' AND time < '{end_iso}'
        ORDER BY time DESC
        LIMIT {int(max_events)}
    '''
    rows_desc = timeseries_db.get_list_query(q_events) or []
    rows = list(reversed(rows_desc))  # process ASC

    # --- Latest known state up to 'end' ---
    q_latest = f'''
        SELECT LAST("up") AS up
        FROM "{AVAILABILITY_RP}"."device_status"
        WHERE "pk"='{device_id}' AND time < '{end_iso}'
    '''
    latest = timeseries_db.get_list_query(q_latest) or []
    end_up_tsdb = int(latest[0]['up']) if latest and latest[0].get('up') is not None else cur_up

    # --- Start boundary (synthetic) using current tz formatting ---
    events = [{
        "time": _fmt_in_current_tz(start_dt),
        "status": "up" if cur_up == 1 else "down",
        "synthetic": True,
        "type": "Boundary",
    }]

    # --- Real flips (display in current tz without hard-coded conversions) ---
    for r in rows:
        nxt = int(r['up'])
        if nxt != cur_up:
            flip_dt = _to_dt(r['time'])
            events.append({
                "time": _fmt_in_current_tz(flip_dt),
                "status": "up" if nxt == 1 else "down",
                "synthetic": False,
                "type": "Flip",
            })
            cur_up = nxt

    # --- End boundary (synthetic) ---
    if override_end_status in ("up", "down"):
        end_status = override_end_status
    else:
        end_status = "up" if end_up_tsdb == 1 else "down"

    events.append({
        "time": _fmt_in_current_tz(end_dt),
        "status": end_status,
        "synthetic": True,
        "type": "Boundary",
    })

    # --- Timeline (rendered as strings in current tz) ---
    timeline = []
    for i in range(len(events) - 1):
        timeline.append({
            "start": events[i]["time"],
            "end": events[i+1]["time"],
            "status": events[i]["status"],
        })

    # --- Friendly intervals & totals ---
    friendly_built = _build_friendly_intervals(events)

    result = {
        "window": {"start": _fmt_in_current_tz(start_dt), "end": _fmt_in_current_tz(end_dt), "tz": tzname},
        "events": events,         # detailed events with type: Boundary/Flip
        "timeline": timeline,     # raw stitched intervals (legacy)
        "friendly": {             # user-friendly data for UI
            "intervals": friendly_built["intervals"],
            "summary": {
                "total_uptime": friendly_built["totals"]["uptime_human"],
                "total_downtime": friendly_built["totals"]["downtime_human"],
                "longest_outage": friendly_built["totals"]["longest_outage"],
            },
        },
    }

    if include_uptime:
        try:
            result["uptime_percent"] = _uptime_pct_from_events(device_id, start_dt, end_dt)
            result["friendly"]["summary"]["uptime_percent"] = result["uptime_percent"]
        except Exception:
            result["uptime_percent"] = None
            result["friendly"]["summary"]["uptime_percent"] = None

    return result


def uptime_pct_for_window(device_id, *, days=0, hours=0):
    end = dj_now()
    start = end - timedelta(days=days, hours=hours)
    return _uptime_pct_from_events(device_id, start, end)


def mac_lookup_cache_timeout():
    """Returns a random number of hours between 48 and 96."""
    return 60 * 60 * random.randint(48, 96)


# -------------------------- Models --------------------------

class AbstractDeviceData(object):
    schema = schema
    __data = None
    __key = 'device_data'
    __data_timestamp = None

    def __init__(self, *args, **kwargs):
        from ..writer import DeviceDataWriter
        self.data = kwargs.pop('data', None)
        self.writer = DeviceDataWriter(self)
        super().__init__(*args, **kwargs)

    @classmethod
    @cache_memoize(CACHE_TIMEOUT)
    def get_devicedata(cls, pk):
        obj = (
            cls.objects.select_related('devicelocation')
            .only(
                'id',
                'organization_id',
                'devicelocation__location_id',
                'devicelocation__floorplan_id',
            )
            .get(id=pk)
        )
        return obj

    @classmethod
    def invalidate_cache(cls, instance, *args, **kwargs):
        if isinstance(instance, load_model('geo', 'DeviceLocation')):
            pk = instance.content_object_id
        else:
            if kwargs.get('created'):
                return
            pk = instance.pk
        cls.get_devicedata.invalidate(cls, str(pk))

    def can_be_updated(self):
        """Do not attempt to push the conf if the device is not reachable."""
        can_be_updated = super().can_be_updated()
        return can_be_updated and self.monitoring.status not in ['critical', 'unknown']

    def _get_wifi_version(self, htmode):
        wifi_version_htmode = f'{_("Other")}: {htmode}'
        if 'NOHT' in htmode:
            wifi_version_htmode = f'{_("Legacy Mode")}: {htmode}'
        elif 'HE' in htmode:
            wifi_version_htmode = f'WiFi 6 (802.11ax): {htmode}'
        elif 'VHT' in htmode:
            wifi_version_htmode = f'WiFi 5 (802.11ac): {htmode}'
        elif 'HT' in htmode:
            wifi_version_htmode = f'WiFi 4 (802.11n): {htmode}'
        return wifi_version_htmode

    @property
    def data_user_friendly(self):
        if not self.data:
            return None
        data = self.data

        # slicing to eliminate the nanoseconds from timestamp
        measured_at = datetime.strptime(self.data_timestamp[0:19], '%Y-%m-%dT%H:%M:%S')
        time_elapsed = int((datetime.utcnow() - measured_at).total_seconds())

        if 'general' in data and 'local_time' in data['general']:
            local_time = data['general']['local_time']
            # Keep timezone as configured in Django for display
            data['general']['local_time'] = datetime.fromtimestamp(
                local_time + time_elapsed, tz=get_current_timezone()
            )

        if 'general' in data and 'uptime' in data['general']:
            uptime = '{0.days} days, {0.hours} hours and {0.minutes} minutes'
            data['general']['uptime'] = uptime.format(
                relativedelta(seconds=data['general']['uptime'] + time_elapsed)
            )

        # used for reordering interfaces
        interface_dict = OrderedDict()
        for interface in data.get('interfaces', []):
            if len(interface.keys()) <= 2:
                continue
            if 'wireless' in interface and 'mode' in interface['wireless']:
                interface['wireless']['mode'] = interface['wireless']['mode'].replace('_', ' ')
            if 'wireless' in interface and 'frequency' in interface['wireless']:
                interface['wireless']['frequency'] /= 1000  # MHz -> GHz
            if 'wireless' in interface and 'htmode' in interface['wireless']:
                interface['wireless']['htmode'] = self._get_wifi_version(
                    interface['wireless']['htmode']
                )
            interface_dict[interface['name']] = interface
        interface_dict = OrderedDict(sorted(interface_dict.items()))
        data['interfaces'] = list(interface_dict.values())

        # reformat expiry in dhcp leases
        for lease in data.get('dhcp_leases', []):
            lease['expiry'] = datetime.fromtimestamp(lease['expiry'], tz=get_current_timezone())

        # --- Availability: percentages + detailed report (events/timeline/friendly)
        try:
            data.setdefault('availability', {})

            # quick % for common windows
            data['availability']['uptime_percent'] = uptime_percentages_for_common_windows(str(self.pk))

            # live end-state override based on current monitoring status
            live_is_up = self.monitoring.status in UP_STATUSES
            override = 'up' if live_is_up else 'down'

            availability_report = get_device_availability(
                str(self.pk),
                hours=24,
                override_end_status=override,
                # display_tz ignored; we use Django current tz
            )
            # raw
            data['availability']['events'] = availability_report["events"]
            data['availability']['timeline'] = availability_report["timeline"]
            data['availability']['uptime_percent_24h'] = availability_report.get("uptime_percent")

            # user-friendly
            data['availability']['intervals'] = availability_report.get("friendly", {}).get("intervals", [])
            data['availability']['summary'] = availability_report.get("friendly", {}).get("summary", {})

        except Exception:
            # keep UI resilient if TSDB is down or no RP/measurement yet
            pass

        return data

    @property
    def data(self):
        """Retrieves last data snapshot from Timeseries Database."""
        if self.__data:
            return self.__data
        q = device_data_query.format(SHORT_RP, self.__key, self.pk)
        cache_key = get_device_cache_key(device=self, context='current-data')
        points = cache.get(cache_key)
        if not points:
            points = timeseries_db.get_list_query(q, precision=None)
        if not points:
            return None
        self.data_timestamp = points[0]['time']
        return json.loads(points[0]['data'])

    @data.setter
    def data(self, data):
        """Sets data."""
        self.__data = data

    @property
    def data_timestamp(self):
        """Retrieves timestamp at which the data was recorded."""
        return self.__data_timestamp

    @data_timestamp.setter
    def data_timestamp(self, value):
        """Sets the timestamp related to the data."""
        self.__data_timestamp = value

    def validate_data(self):
        """Validates data according to NetJSON DeviceMonitoring schema."""
        try:
            validate(self.data, self.schema, format_checker=draft7_format_checker)
        except SchemaError as e:
            path = [str(el) for el in e.path]
            trigger = '/'.join(path)
            message = 'Invalid data in "#/{0}", validator says:\n\n{1}'.format(
                trigger, e.message
            )
            raise ValidationError(message)

    def _transform_data(self):
        """Performs corrections or additions to the device data."""
        mac_detection = app_settings.MAC_VENDOR_DETECTION
        for interface in self.data.get('interfaces', []):
            # loop over mobile signal values to convert them to float
            if 'mobile' in interface and 'signal' in interface['mobile']:
                for signal_key, signal_values in interface['mobile']['signal'].items():
                    for key, value in signal_values.items():
                        signal_values[key] = float(value)

            wireless = interface.get('wireless')
            if wireless and all(key in wireless for key in ('htmode', 'clients')):
                for client in wireless['clients']:
                    htmode = wireless['htmode']
                    ht_enabled = htmode.startswith('HT')
                    vht_enabled = htmode.startswith('VHT')
                    noht_enabled = htmode == 'NOHT'
                    if noht_enabled:
                        client['ht'] = client['vht'] = None
                        if 'he' in client:
                            client['he'] = None
                    elif ht_enabled:
                        if client['vht'] is False:
                            client['vht'] = None
                        if client.get('he') is False:
                            client['he'] = None
                    elif vht_enabled and client.get('he') is False:
                        client['he'] = None

            # Convert bitrate from KBits/s to MBits/s
            if wireless and 'bitrate' in wireless:
                interface['wireless']['bitrate'] = round(
                    interface['wireless']['bitrate'] / 1000.0, 1
                )

            # add mac vendor to wireless clients if present
            if (
                not mac_detection
                or 'wireless' not in interface
                or 'clients' not in interface['wireless']
            ):
                continue
            for client in interface['wireless']['clients']:
                client['vendor'] = self._mac_lookup(client['mac'])

        if not mac_detection:
            return

        # add mac vendor to neighbors
        for neighbor in self.data.get('neighbors', []):
            neighbor['vendor'] = self._mac_lookup(neighbor.get('mac'))

        # add mac vendor to DHCP leases
        for lease in self.data.get('dhcp_leases', []):
            lease['vendor'] = self._mac_lookup(lease['mac'])

    @cache_memoize(mac_lookup_cache_timeout())
    def _mac_lookup(self, value):
        if not value:
            return ''
        try:
            return EUI(value).oui.registration().org
        except NotRegisteredError:
            return ''

    def save_data(self, time=None):
        """Validates and saves data to Timeseries Database."""
        self.validate_data()
        self._transform_data()
        time = time or dj_now()
        options = dict(tags={'pk': self.pk}, timestamp=time, retention_policy=SHORT_RP)
        _timeseries_write(name=self.__key, values={'data': self.json()}, **options)
        cache_key = get_device_cache_key(device=self, context='current-data')
        cache.set(
            cache_key,
            [
                {
                    'data': self.json(),
                    'time': time.isoformat(timespec='seconds'),
                }
            ],
            timeout=CACHE_TIMEOUT,
        )
        if app_settings.WIFI_SESSIONS_ENABLED:
            self.save_wifi_clients_and_sessions()

    def json(self, *args, **kwargs):
        return json.dumps(self.data, *args, **kwargs)

    def save_wifi_clients_and_sessions(self):
        _WIFICLIENT_FIELDS = ['vendor', 'ht', 'vht', 'he', 'wmm', 'wds', 'wps']
        WifiClient = load_model('device_monitoring', 'WifiClient')
        WifiSession = load_model('device_monitoring', 'WifiSession')

        active_sessions = []
        interfaces = self.data.get('interfaces', [])
        for interface in interfaces:
            if interface.get('type') != 'wireless':
                continue
            interface_name = interface.get('name')
            wireless = interface.get('wireless', {})
            if not wireless or wireless['mode'] != 'access_point':
                continue
            ssid = wireless.get('ssid')
            clients = wireless.get('clients', [])
            for client in clients:
                # Save WifiClient
                client_obj = WifiClient.get_wifi_client(client.get('mac'))
                update_fields = []
                for field in _WIFICLIENT_FIELDS:
                    if getattr(client_obj, field) != client.get(field):
                        setattr(client_obj, field, client.get(field))
                        update_fields.append(field)
                if update_fields:
                    client_obj.full_clean()
                    client_obj.save(update_fields=update_fields)

                # Save WifiSession
                session_obj, _ = WifiSession.objects.get_or_create(
                    device_id=self.id,
                    interface_name=interface_name,
                    ssid=ssid,
                    wifi_client=client_obj,
                    stop_time=None,
                )
                active_sessions.append(session_obj.pk)

        # Close open WifiSession
        WifiSession.objects.filter(
            device_id=self.id,
            stop_time=None,
        ).exclude(
            pk__in=active_sessions
        ).update(stop_time=dj_now())


class AbstractDeviceMonitoring(TimeStampedEditableModel):
    device = models.OneToOneField(
        swapper.get_model_name('config', 'Device'),
        on_delete=models.CASCADE,
        related_name='monitoring',
    )
    STATUS = Choices(
        ('unknown', _(app_settings.HEALTH_STATUS_LABELS['unknown'])),
        ('ok', _(app_settings.HEALTH_STATUS_LABELS['ok'])),
        ('problem', _(app_settings.HEALTH_STATUS_LABELS['problem'])),
        ('critical', _(app_settings.HEALTH_STATUS_LABELS['critical'])),
        ('deactivated', _(app_settings.HEALTH_STATUS_LABELS['deactivated'])),
    )
    status = StatusField(
        _('health status'),
        db_index=True,
        help_text=_(
            '"{0}" means the device has been recently added;\n'
            '"{1}" means the device is operating normally;\n'
            '"{2}" means the device is having issues but it\'s still reachable;\n'
            '"{3}" means the device is not reachable or in critical conditions;\n'
            '"{4}" means the device is deactivated;'
        ).format(
            app_settings.HEALTH_STATUS_LABELS['unknown'],
            app_settings.HEALTH_STATUS_LABELS['ok'],
            app_settings.HEALTH_STATUS_LABELS['problem'],
            app_settings.HEALTH_STATUS_LABELS['critical'],
            app_settings.HEALTH_STATUS_LABELS['deactivated'],
        ),
    )

    class Meta:
        abstract = True

    def update_status(self, value):
        # don't trigger save nor emit signal if status is not changing
        if self.status == value:
            return
        self.status = value
        self.full_clean()
        self.save()
        # clear device management_ip when device is offline
        if self.status == 'critical' and app_settings.AUTO_CLEAR_MANAGEMENT_IP:
            self.device.management_ip = None
            self.device.save(update_fields=['management_ip'])

        health_status_changed.send(sender=self.__class__, instance=self, status=value)

    @property
    def related_metrics(self):
        Metric = load_model('monitoring', 'Metric')
        return Metric.objects.select_related('content_type').filter(
            object_id=self.device_id,
            content_type__model='device',
            content_type__app_label='config',
        )

    @staticmethod
    @receiver(threshold_crossed, dispatch_uid='threshold_crossed_receiver')
    def threshold_crossed(sender, metric, alert_settings, target, first_time, **kwargs):
        """Executed when a threshold is crossed."""
        DeviceMonitoring = load_model('device_monitoring', 'DeviceMonitoring')
        if not isinstance(target, DeviceMonitoring.device.field.related_model):
            return
        try:
            monitoring = target.monitoring
        except DeviceMonitoring.DoesNotExist:
            monitoring = DeviceMonitoring.objects.create(device=target)
        status = 'ok' if metric.is_healthy else 'problem'
        related_status = 'ok'
        for related_metric in monitoring.related_metrics.filter(is_healthy=False):
            if monitoring.is_metric_critical(related_metric):
                related_status = 'critical'
                break
            related_status = 'problem'
        if metric.is_healthy and related_status == 'problem':
            status = 'problem'
        elif metric.is_healthy and related_status == 'critical':
            status = 'critical'
        elif not metric.is_healthy and any(
            [monitoring.is_metric_critical(metric), related_status == 'critical']
        ):
            status = 'critical'
        monitoring.update_status(status)

    @staticmethod
    def is_metric_critical(metric):
        for critical in app_settings.CRITICAL_DEVICE_METRICS:
            if all(
                [
                    metric.key == critical['key'],
                    metric.field_name == critical['field_name'],
                ]
            ):
                return True
        return False

    @classmethod
    def handle_disabled_organization(cls, organization_id):
        """Handles the disabling of an organization."""
        load_model('config', 'Device').objects.filter(
            organization_id=organization_id
        ).update(management_ip='')
        cls.objects.filter(device__organization_id=organization_id).update(
            status='unknown'
        )

    @classmethod
    def handle_deactivated_device(cls, instance, **kwargs):
        """Handles the deactivation of a device."""
        cls.objects.filter(device_id=instance.id).update(status='deactivated')

    @classmethod
    def handle_activated_device(cls, instance, **kwargs):
        """Handles the activation of a deactivated device."""
        cls.objects.filter(device_id=instance.id).update(status='unknown')

    @classmethod
    def _get_critical_metric_keys(cls):
        return [metric['key'] for metric in get_critical_device_metrics()]

    @classmethod
    def handle_critical_metric(cls, instance, **kwargs):
        critical_metrics = cls._get_critical_metric_keys()
        if instance.check_type in critical_metrics:
            try:
                device_monitoring = cls.objects.get(device=instance.content_object)
                if not instance.is_active or kwargs.get('signal') == post_delete:
                    device_monitoring.update_status('unknown')
            except cls.DoesNotExist:
                pass


class AbstractWifiClient(TimeStampedEditableModel):
    id = None
    mac_address = models.CharField(
        max_length=17,
        db_index=True,
        primary_key=True,
        validators=[mac_address_validator],
        help_text=_('MAC address'),
    )
    vendor = models.CharField(max_length=200, blank=True, null=True)
    he = models.BooleanField(null=True, blank=True, default=None, verbose_name='HE')
    vht = models.BooleanField(null=True, blank=True, default=None, verbose_name='VHT')
    ht = models.BooleanField(null=True, blank=True, default=None, verbose_name='HT')
    wmm = models.BooleanField(default=False, verbose_name='WMM')
    wds = models.BooleanField(default=False, verbose_name='WDS')
    wps = models.BooleanField(default=False, verbose_name='WPS')

    class Meta:
        abstract = True
        verbose_name = _('WiFi Client')
        ordering = ('-created',)

    @classmethod
    @cache_memoize(CACHE_TIMEOUT)
    def get_wifi_client(cls, mac_address):
        wifi_client, _ = cls.objects.get_or_create(mac_address=mac_address)
        return wifi_client

    @classmethod
    def invalidate_cache(cls, instance, *args, **kwargs):
        if kwargs.get('created'):
            return
        cls.get_wifi_client.invalidate(cls, instance.mac_address)


class AbstractWifiSession(TimeStampedEditableModel):
    created = None

    device = models.ForeignKey(
        swapper.get_model_name('config', 'Device'),
        on_delete=models.CASCADE,
    )
    wifi_client = models.ForeignKey(
        swapper.get_model_name('device_monitoring', 'WifiClient'),
        on_delete=models.CASCADE,
    )
    ssid = models.CharField(
        max_length=32, blank=True, null=True, verbose_name=_('SSID')
    )
    interface_name = models.CharField(max_length=15)
    start_time = models.DateTimeField(
        verbose_name=_('start time'),
        db_index=True,
        auto_now=True,
    )
    stop_time = models.DateTimeField(
        verbose_name=_('stop time'),
        db_index=True,
        null=True,
        blank=True,
    )

    class Meta:
        abstract = True
        verbose_name = _('WiFi Session')
        ordering = ('-start_time',)

    def __str__(self):
        return self.mac_address

    @property
    def mac_address(self):
        return self.wifi_client.mac_address

    @property
    def vendor(self):
        return self.wifi_client.vendor

    @classmethod
    def offline_device_close_session(
        cls, metric, tolerance_crossed, first_time, target, **kwargs
    ):
        if (
            not first_time
            and tolerance_crossed
            and not metric.is_healthy_tolerant
            and AbstractDeviceMonitoring.is_metric_critical(metric)
        ):
            tasks.offline_device_close_session.delay(device_id=target.pk)

class AbstractTunnelData(object):
    schema = tunnel_monitoring_schema
    __data = None
    __key = "tunnel_data"
    __data_timestamp = None
    # type = "TunnelMonitoring"

    # class Meta:
    #     abstract = True

    def __init__(self, *args, **kwargs):
        from ..writer import TunnelDataWriter  # writer like DeviceDataWriter

        self.data = kwargs.pop("data", None)
        self.writer = TunnelDataWriter(self)
        super().__init__(*args, **kwargs)

    # ---------------------------------------------------------
    # Retrieve TunnelData object (cached)
    # ---------------------------------------------------------
    @classmethod
    @cache_memoize(CACHE_TIMEOUT)
    def get_tunneldata(cls, pk):
        obj = (
            cls.objects.select_related("devicelocation")
            .only(
                "id",
                "organization_id",
                "devicelocation__location_id",
                "devicelocation__floorplan_id",
            )
            .get(id=pk)
        )
        return obj

    @classmethod
    def invalidate_cache(cls, instance, *args, **kwargs):
        if isinstance(instance, load_model("geo", "DeviceLocation")):
            pk = instance.content_object_id
        else:
            if kwargs.get("created"):
                return
            pk = instance.pk
        cls.get_tunneldata.invalidate(cls, str(pk))

    # ---------------------------------------------------------
    # Data helpers
    # ---------------------------------------------------------
    @property
    def data(self):
        """Retrieve last tunnel data snapshot from InfluxDB / cache."""
        if self.__data:
            return self.__data
        q = f'SELECT * FROM "{SHORT_RP}"."{self.__key}" WHERE "pk" = \'{self.pk}\' ORDER BY time DESC LIMIT 1'
        cache_key = get_device_cache_key(device=self, context="current-tunnel-data")
        points = cache.get(cache_key)
        if not points:
            points = timeseries_db.get_list_query(q, precision=None)
        if not points:
            return None
        self.data_timestamp = points[0]["time"]
        return json.loads(points[0]["data"])

    @data.setter
    def data(self, data):
        self.__data = data

    @property
    def data_timestamp(self):
        return self.__data_timestamp

    @data_timestamp.setter
    def data_timestamp(self, value):
        self.__data_timestamp = value

    # ---------------------------------------------------------
    # Data transformation for user-friendly display
    # ---------------------------------------------------------
    @property
    def data_user_friendly(self):
        if not self.data:
            return None
        data = self.data
        tunnel_health = data.get("tunnel_health", {})
        measured_at = datetime.strptime(self.data_timestamp[0:19], "%Y-%m-%dT%H:%M:%S")
        time_elapsed = int((datetime.utcnow() - measured_at).total_seconds())

        # Convert timestamp to readable format
        if "timestamp" in tunnel_health:
            tunnel_health["timestamp"] = datetime.fromisoformat(
                tunnel_health["timestamp"]
            ).astimezone(tz("UTC"))

        data["tunnel_health"] = tunnel_health
        return data

    # ---------------------------------------------------------
    # Validation and Saving
    # ---------------------------------------------------------
    def validate_data(self):
        """Validates TunnelMonitoring data schema."""
        try:
            validate(self.data, self.schema, format_checker=draft7_format_checker)
        except SchemaError as e:
            path = [str(el) for el in e.path]
            trigger = "/".join(path)
            message = f'Invalid data in "#/{trigger}", validator says:\n\n{e.message}'
            raise ValidationError(message)

    def save_data(self, time=None):
        """Validate and write data to timeseries DB (InfluxDB)."""
        self.validate_data()
        time = time or dj_now()
        options = dict(tags={"pk": self.pk}, timestamp=time, retention_policy=SHORT_RP)
        _timeseries_write(name=self.__key, values={"data": self.json()}, **options)

        cache_key = get_device_cache_key(device=self, context="current-tunnel-data")
        app_settings.CACHE.set(
            cache_key,
            [
                {
                    "data": self.json(),
                    "time": time.astimezone(tz("UTC")).isoformat(timespec="seconds"),
                }
            ],
            timeout=CACHE_TIMEOUT,
        )

    # ---------------------------------------------------------
    # Helpers
    # ---------------------------------------------------------
    def json(self, *args, **kwargs):
        return json.dumps(self.data, *args, **kwargs)
    