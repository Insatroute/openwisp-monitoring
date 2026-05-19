from django.dispatch import Signal


health_status_changed = Signal()
health_status_changed.__doc__ = """
Providing arguments: ['instance', 'status']
"""
device_metrics_received = Signal()
device_metrics_received.__doc__ = """
Providing arguments: ['instance', 'request', 'time', 'current']
"""


# from .notifications.sim_state import handle_sim_state
# from openwisp_monitoring.device.signals import device_metrics_received
#
# device_metrics_received.connect(
#     handle_sim_state,
#     dispatch_uid="sim_state_notification"
# )
