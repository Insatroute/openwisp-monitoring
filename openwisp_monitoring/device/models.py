from django.contrib.contenttypes.fields import GenericRelation
from swapper import get_model_name, load_model, swappable_setting
from django.db import  models
from .base.models import (
    AbstractDeviceData,
    AbstractDeviceMonitoring,
    AbstractWifiClient,
    AbstractWifiSession,
)

BaseDevice = load_model("config", "Device", require_ready=False)


class DeviceData(AbstractDeviceData, BaseDevice):
    checks = GenericRelation(get_model_name("check", "Check"))
    metrics = GenericRelation(get_model_name("monitoring", "Metric"))

    class Meta:
        proxy = True
        swappable = swappable_setting("device_monitoring", "DeviceData")


class DeviceMonitoring(AbstractDeviceMonitoring):
    class Meta(AbstractDeviceMonitoring.Meta):
        abstract = False
        swappable = swappable_setting("device_monitoring", "DeviceMonitoring")


class WifiClient(AbstractWifiClient):
    class Meta(AbstractWifiClient.Meta):
        abstract = False
        swappable = swappable_setting("device_monitoring", "WifiClient")


class WifiSession(AbstractWifiSession):
    class Meta(AbstractWifiSession.Meta):
        abstract = False
        swappable = swappable_setting("device_monitoring", "WifiSession")

class SpokeStatus(models.Model):
    device = models.ForeignKey(
       DeviceData,
       on_delete=models.CASCADE,
       related_name='SpokeStatus_reports'
    )
    timestamp = models.DateTimeField(null=True, blank=True)
    raw = models.JSONField()
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-timestamp']
        indexes = [models.Index(fields=['device', 'timestamp'])]  

class IpsecTunnels(models.Model):
    device = models.ForeignKey(
       DeviceData,
       on_delete=models.CASCADE,
       related_name='IpsecTunnels_reports'
    )
    timestamp = models.DateTimeField(null=True, blank=True)
    raw = models.JSONField()
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-timestamp']
        indexes = [models.Index(fields=['device', 'timestamp'])]