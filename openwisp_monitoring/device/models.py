from django.contrib.contenttypes.fields import GenericRelation
from swapper import get_model_name, load_model, swappable_setting
# from openwisp_controller.config.models import Config as DeviceData
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone

from .base.models import (
    AbstractDeviceData,
    AbstractDeviceMonitoring,
    AbstractWifiClient,
    AbstractWifiSession,
)
from django.db import  models

BaseDevice = load_model('config', 'Device', require_ready=False)


class DeviceData(AbstractDeviceData, BaseDevice):
    checks = GenericRelation(get_model_name('check', 'Check'))
    metrics = GenericRelation(get_model_name('monitoring', 'Metric'))

    class Meta:
        proxy = True
        swappable = swappable_setting('device_monitoring', 'DeviceData')


class DeviceMonitoring(AbstractDeviceMonitoring):
    class Meta(AbstractDeviceMonitoring.Meta):
        abstract = False
        swappable = swappable_setting('device_monitoring', 'DeviceMonitoring')


class WifiClient(AbstractWifiClient):
    class Meta(AbstractWifiClient.Meta):
        abstract = False
        swappable = swappable_setting('device_monitoring', 'WifiClient')


class WifiSession(AbstractWifiSession):
    class Meta(AbstractWifiSession.Meta):
        abstract = False
        swappable = swappable_setting('device_monitoring', 'WifiSession')


class DPIRecord(models.Model):
    device = models.ForeignKey(
       DeviceData,
       on_delete=models.CASCADE,
       related_name='dpi_records'
    )
    timestamp = models.DateTimeField(null=True, blank=True)
    raw = models.JSONField()
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-timestamp']
        indexes = [models.Index(fields=['device', 'timestamp'])]


class ClientSummary(models.Model):
    device = models.ForeignKey(
       DeviceData,
       on_delete=models.CASCADE,
       related_name='client_summaries'
    )
    timestamp = models.DateTimeField(null=True, blank=True)
    raw = models.JSONField()
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-timestamp']
        indexes = [models.Index(fields=['device', 'timestamp'])]

class RealTraffic(models.Model):
    device = models.ForeignKey(
       DeviceData,
       on_delete=models.CASCADE,
       related_name='real_traffic_records'
    )
    timestamp = models.DateTimeField(null=True, blank=True)
    raw = models.JSONField()
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-timestamp']
        indexes = [models.Index(fields=['device', 'timestamp'])]



class TSIPReport(models.Model):
    device = models.ForeignKey(
       DeviceData,
       on_delete=models.CASCADE,
       related_name='tsip_reports'
    )
    timestamp = models.DateTimeField(null=True, blank=True)
    raw = models.JSONField()
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-timestamp']
        indexes = [models.Index(fields=['device', 'timestamp'])]

class InerfaceEvents(models.Model):
    device = models.ForeignKey(
       DeviceData,
       on_delete=models.CASCADE,
       related_name='interface_events_reports'
    )
    timestamp = models.DateTimeField(null=True, blank=True)
    raw = models.JSONField()
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-timestamp']
        indexes = [models.Index(fields=['device', 'timestamp'])]

class InterfaceTraffic(models.Model):
    device = models.ForeignKey(
       DeviceData,
       on_delete=models.CASCADE,
       related_name='interface_traffic_reports'
    )
    timestamp = models.DateTimeField(null=True, blank=True)
    raw = models.JSONField()
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-timestamp']
        indexes = [models.Index(fields=['device', 'timestamp'])]

class InterfaceList(models.Model):
    device = models.ForeignKey(
       DeviceData,
       on_delete=models.CASCADE,
       related_name='interface_list_reports'
    )
    timestamp = models.DateTimeField(null=True, blank=True)
    raw = models.JSONField()
    created = models.DateTimeField(auto_now_add=True)
    class Meta:
        ordering = ['-timestamp']
        indexes = [models.Index(fields=['device', 'timestamp'])]


class LatquaList(models.Model):
    device = models.ForeignKey(
       DeviceData,
       on_delete=models.CASCADE,
       related_name='latqua_reports'
    )
    timestamp = models.DateTimeField(null=True, blank=True)
    raw = models.JSONField()
    created = models.DateTimeField(auto_now_add=True)
    class Meta:
        ordering = ['-timestamp']
        indexes = [models.Index(fields=['device', 'timestamp'])]

class WanStatus(models.Model):
    device    = models.ForeignKey(
        DeviceData,
        on_delete=models.CASCADE,
        related_name='wanstatus_reports',
    )
    timestamp = models.DateTimeField(null=True, blank=True)
    raw       = models.JSONField()
    created   = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        # For each entry in the raw JSON, update or create exactly one interface record
        from .models import WanInterfaceStatus
        for entry in (self.raw or []):
            WanInterfaceStatus.objects.update_or_create(
                device    = self.device,
                interface = entry.get('interface', ''),
                defaults={
                    'wan_status': self,
                    'nic_device': entry.get('device', ''),
                    'up':         entry.get('up', False),
                    'timestamp':  self.timestamp,
                }
            )

    class Meta:
        ordering = ['-timestamp']
        indexes  = [models.Index(fields=['device', 'timestamp'])]


class WanInterfaceStatus(models.Model):
    # Link directly to the physical device
    device     = models.ForeignKey(
        DeviceData,
        on_delete=models.CASCADE,
        related_name='interface_statuses',
    )
    # Name of the interface (e.g. 'wan1', 'eth2')
    interface  = models.CharField(max_length=64)
    # Store the raw NIC identifier separately
    nic_device = models.CharField(max_length=64, blank=True, null=True)
    # Reference back to the specific snapshot (optional)
    wan_status = models.ForeignKey(
        WanStatus,
        on_delete=models.CASCADE,
        related_name='interfaces',
    )
    # Up/down boolean pulled from the JSON
    up         = models.BooleanField(db_index=True)
    # Timestamp of the snapshot
    timestamp  = models.DateTimeField(default=timezone.now)

    class Meta:
        # Ensure exactly one record per (device, interface) pair
        unique_together = ('device', 'interface')
        ordering        = ['-timestamp', 'interface']
        indexes = [
            models.Index(fields=['device', 'interface']),
            models.Index(fields=['wan_status', 'up']),
        ]

   

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

class ConfigPush(models.Model):
    device = models.ForeignKey(
       DeviceData,
       on_delete=models.CASCADE,
       related_name='config_push_reports'
    )
    timestamp = models.DateTimeField(null=True, blank=True)
    raw = models.JSONField()
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-timestamp']
        indexes = [models.Index(fields=['device', 'timestamp'])]



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