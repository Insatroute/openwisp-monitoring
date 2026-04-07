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
