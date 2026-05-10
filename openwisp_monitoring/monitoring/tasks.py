from celery import shared_task
from django.core.exceptions import ObjectDoesNotExist
from swapper import load_model

from openwisp_utils.tasks import OpenwispCeleryTask

from ..db import timeseries_db
from ..db.exceptions import TimeseriesWriteException
from .settings import RETRY_OPTIONS
from .signals import post_metric_write


def _metric_post_write(name, values, metric, check_threshold_kwargs=None, **kwargs):
    if not metric or not check_threshold_kwargs:
        return
    try:
        Metric = load_model("monitoring", "Metric")
        if not isinstance(metric, Metric):
            metric = Metric.objects.select_related("alertsettings").get(pk=metric)
    except ObjectDoesNotExist:
        # The metric can be deleted by the time threshold is being checked.
        # This can happen as the task is being run async.
        pass
    else:
        metric.check_threshold(**check_threshold_kwargs)
        signal_kwargs = dict(
            sender=metric.__class__,
            metric=metric,
            values=values,
            time=kwargs.get("timestamp"),
            current=kwargs.get("current", "False"),
        )
        post_metric_write.send(**signal_kwargs)


@shared_task(
    base=OpenwispCeleryTask,
    bind=True,
    autoretry_for=(TimeseriesWriteException,),
    **RETRY_OPTIONS
)
def timeseries_write(
    self, name, values, metric=None, check_threshold_kwargs=None, **kwargs
):
    """Writes and retries with exponential backoff on failures."""
    timeseries_db.write(name, values, **kwargs)
    _metric_post_write(name, values, metric, check_threshold_kwargs, **kwargs)


def _timeseries_write(name, values, metric=None, check_threshold_kwargs=None, **kwargs):
    """Handles writes synchronously when using UDP mode."""
    if timeseries_db.use_udp:
        func = timeseries_write
    else:
        func = timeseries_write.delay
        metric = metric.pk if metric else None
    func(
        name=name,
        values=values,
        metric=metric,
        check_threshold_kwargs=check_threshold_kwargs,
        **kwargs
    )


@shared_task(
    base=OpenwispCeleryTask,
    bind=True,
    autoretry_for=(TimeseriesWriteException,),
    **RETRY_OPTIONS
)
def timeseries_batch_write(self, data):
    """Writes data in batches.

    Similar to timeseries_write function above, but operates on list of
    metric data (batch operation)
    """
    timeseries_db.batch_write(data)
    for metric_data in data:
        _metric_post_write(**metric_data)


def _timeseries_batch_write(data):
    """If the timeseries database is using UDP to write data, then write data synchronously."""
    if timeseries_db.use_udp:
        timeseries_batch_write(data=data)
    else:
        for item in data:
            item["metric"] = item["metric"].pk
        timeseries_batch_write.delay(data=data)


@shared_task(base=OpenwispCeleryTask)
def delete_timeseries(key, tags):
    timeseries_db.delete_series(key=key, tags=tags)


@shared_task
def migrate_timeseries_database():
    """Performs migrations of timeseries datab.

    Performed asynchronously, due to changes introduced in
    https://github.com/openwisp/openwisp-monitoring/pull/368

    To be removed in a future release.
    """
    from .migrations.influxdb.influxdb_alter_structure_0006 import (
        migrate_influxdb_structure,
    )

    migrate_influxdb_structure()


@shared_task(bind=True, ignore_result=True)
def delayed_alert_check(self, metric_id, notification_type, device_id, alert_config_id):
    """Re-check metric status after delay period.

    Called by _notify_users when email_timing="after_retries".
    After interval*retry minutes, checks if the metric is STILL in the same state.
    If yes → fires notification. If recovered → cancels silently.
    """
    import logging
    from django.core.cache import cache

    logger = logging.getLogger(__name__)
    pending_key = f"alert_pending:{notification_type}:{device_id}"

    try:
        Metric = load_model("monitoring", "Metric")
        metric = Metric.objects.select_related("alertsettings").get(pk=metric_id)
    except ObjectDoesNotExist:
        cache.delete(pending_key)
        return

    # Check current health state
    is_problem_type = notification_type.endswith("_problem") or notification_type in (
        "connection_is_not_working", "interface_is_down", "tunnel_down",
    )
    is_recovery_type = notification_type.endswith("_recovery") or notification_type in (
        "connection_is_working", "interface_is_up", "tunnel_up",
    )

    # Determine if the condition still holds
    still_in_state = False
    if is_problem_type and metric.is_healthy is False:
        still_in_state = True
    elif is_recovery_type and metric.is_healthy is True:
        still_in_state = True

    cache.delete(pending_key)

    if not still_in_state:
        logger.info(
            "Delayed alert cancelled: %s for device %s — state changed",
            notification_type, device_id
        )
        return

    # State still holds after delay — fire notification
    logger.info(
        "Delayed alert firing: %s for device %s — state confirmed after retry period",
        notification_type, device_id
    )

    try:
        from email_templates.models import AlertConfiguration
        alert_config = AlertConfiguration.objects.filter(pk=alert_config_id).first()
    except Exception:
        alert_config = None

    try:
        alert_settings = metric.alertsettings
    except ObjectDoesNotExist:
        alert_settings = None

    metric._send_notification(notification_type, alert_settings, metric.content_object, alert_config)



@shared_task(bind=True, ignore_result=True, max_retries=0, time_limit=120)
def fix_stuck_recovery_notifications(self):
    """Detect and fix metrics where is_healthy and is_healthy_tolerant are mismatched.

    Two stuck cases:
    1. is_healthy=True, is_healthy_tolerant=False → recovery notification missed
    2. is_healthy=False, is_healthy_tolerant=True → problem notification missed

    Runs every 5 minutes via celery beat to auto-heal stuck metrics.
    """
    import logging
    logger = logging.getLogger(__name__)
    Metric = load_model("monitoring", "Metric")

    fixed = 0

    # Case 1: Device recovered but recovery notification never fired
    stuck_recovery = Metric.objects.filter(
        is_healthy=True,
        is_healthy_tolerant=False,
    ).select_related("alertsettings", "content_type")

    for m in stuck_recovery[:100]:
        try:
            m.is_healthy_tolerant = True
            m.save(update_fields=["is_healthy_tolerant"])
            notification_type = f"{m.configuration}_recovery"

            # Pair-guard: only fire *_recovery when a matching *_problem
            # actually fired in the last 2h. Otherwise it's a phantom
            # recovery from a sub-tolerance spike that never crossed
            # the threshold long enough to trigger a problem.
            try:
                from openwisp_notifications.models import (
                    Notification as _Notif,
                )
                from django.utils import timezone as _tz
                from datetime import timedelta as _td
                problem_type = f"{m.configuration}_problem"
                target_pk = str(m.object_id) if m.object_id else None
                pair_cutoff = _tz.now() - _td(hours=2)
                has_pair = False
                if target_pk:
                    has_pair = _Notif.objects.filter(
                        target_object_id=target_pk,
                        type=problem_type,
                        timestamp__gte=pair_cutoff,
                    ).exists()
                if not has_pair:
                    logger.info(
                        "Skipped phantom recovery: %s for target=%s "
                        "(no recent %s in last 2h)",
                        notification_type, target_pk, problem_type,
                    )
                    continue
            except Exception as _exc:
                logger.warning(
                    "Pair-guard check failed for %s: %s; "
                    "falling through to fire",
                    notification_type, _exc,
                )

            try:
                alert_settings = m.alertsettings
                if alert_settings.is_active:
                    m._notify_users(notification_type, alert_settings)
                    fixed += 1
                    logger.info(
                        "Fixed stuck recovery: config=%s target=%s",
                        m.configuration, m.content_object,
                    )
            except ObjectDoesNotExist:
                fixed += 1
        except Exception as exc:
            logger.warning("Failed to fix stuck recovery metric %s: %s", m.pk, exc)

    # Case 2: Device went down but problem notification never fired
    stuck_problem = Metric.objects.filter(
        is_healthy=False,
        is_healthy_tolerant=True,
    ).select_related("alertsettings", "content_type")

    for m in stuck_problem[:100]:
        try:
            m.is_healthy_tolerant = False
            m.save(update_fields=["is_healthy_tolerant"])
            notification_type = f"{m.configuration}_problem"
            try:
                alert_settings = m.alertsettings
                if alert_settings.is_active:
                    m._notify_users(notification_type, alert_settings)
                    fixed += 1
                    logger.info(
                        "Fixed stuck problem: config=%s target=%s",
                        m.configuration, m.content_object,
                    )
            except ObjectDoesNotExist:
                fixed += 1
        except Exception as exc:
            logger.warning("Failed to fix stuck problem metric %s: %s", m.pk, exc)

    if fixed:
        logger.info("fix_stuck_notifications: healed %d stuck metrics", fixed)


@shared_task(bind=True, ignore_result=True)
def delayed_iface_alert_check(self, device_id, ifname, notification_type, alert_config_id):
    """Re-check interface status after delay period.

    Called by check_interface_state_and_notify when email_timing="after_retries".
    After interval*retry minutes, checks if interface is STILL down/up.
    If yes → fires notification. If changed → cancels.
    """
    import logging
    from django.core.cache import cache

    logger = logging.getLogger(__name__)
    # Build correct pending key prefix based on notification type
    _prefix_map = {
        'interface_is_down': 'iface_down',
        'interface_is_up': 'iface_up',
        'wan_internet_down': 'wan_down',
        'wan_internet_up': 'wan_up',
    }
    pending_key = f"alert_pending:{_prefix_map.get(notification_type, 'iface_down')}:{device_id}:{ifname}"
    cache.delete(pending_key)

    try:
        Device = load_model('config', 'Device')
        device = Device.objects.get(pk=device_id)
    except Exception:
        return

    # Get current interface state from stored device data
    try:
        from swapper import load_model as _lm
        DeviceData = _lm('device_monitoring', 'DeviceData')
        dd = DeviceData.objects.get(pk=device_id)
        interfaces = (dd.data or {}).get('interfaces', [])
        iface_up = None
        wan_status = None
        for i in interfaces:
            if i.get('name') == ifname:
                iface_up = i.get('up', False)
                wan_status = i.get('wan_status')
                break
    except Exception:
        return

    if iface_up is None:
        logger.info("Delayed iface check: %s not found on %s — cancelled", ifname, device.name)
        return

    # Check if state still matches
    # For WAN notifications, use wan_status; for interface, use up field
    is_wan_type = notification_type in ('wan_internet_down', 'wan_internet_up')
    if is_wan_type:
        wan_is_online = (wan_status == 'online')
        if notification_type == 'wan_internet_down' and wan_is_online:
            logger.info("Delayed %s cancelled: %s on %s — recovered", notification_type, ifname, device.name)
            return
        if notification_type == 'wan_internet_up' and not wan_is_online:
            logger.info("Delayed %s cancelled: %s on %s — still down", notification_type, ifname, device.name)
            return
    else:
        is_down_type = notification_type == 'interface_is_down'
        is_up_type = notification_type == 'interface_is_up'
        if is_down_type and iface_up:
            logger.info("Delayed %s cancelled: %s on %s — recovered", notification_type, ifname, device.name)
            return
        if is_up_type and not iface_up:
            logger.info("Delayed %s cancelled: %s on %s — went down again", notification_type, ifname, device.name)
            return

    # State confirmed — fire notification
    logger.info("Delayed %s confirmed: %s on %s", notification_type, ifname, device.name)

    try:
        from email_templates.models import AlertConfiguration
        alert_config = AlertConfiguration.objects.filter(pk=alert_config_id).first()
    except Exception:
        alert_config = None

    try:
        from openwisp_notifications.signals import notify
        opts = dict(sender=device, type=notification_type, target=device, ifname=ifname)
        if alert_config:
            opts['data'] = {'_alert_config_id': str(alert_config.pk), '_email_timing': alert_config.email_timing}
        notify.send(**opts)
    except Exception as e:
        logger.warning("Delayed iface notification failed: %s", e)

