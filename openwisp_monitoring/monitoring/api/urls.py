from django.urls import path,re_path

from . import views,views_topdevices, views_topapp, views_realdata
# from .views_topdevices import *


app_name = "monitoring_general"

urlpatterns = [
    path(
        "api/v1/monitoring/dashboard/",
        views.dashboard_timeseries,
        name="api_dashboard_timeseries",
    ),
    path(
        "api/v1/monitoring/top-devices-simple/",
        views_topdevices.top_devices_simple,
        name="monitoring-top-devices-simple"
    ),
    path(
        "api/v1/monitoring/global-top-devices/",
        views_topapp.global_top_devices,
        name="api_global_top_devices"
    ),
    path(
        "api/v1/monitoring/global-top-apps/",
        views_topapp.global_top_apps,
        name="api_global_top_apps"
    ),
    re_path(
        r'^api/v1/monitoring/device/(?P<device_id>[^/]+)/traffic-summary/$',
        views_realdata.traffic_summary_data,
        name='api_device_traffic_summary'
    ),
    re_path(
        r'^api/v1/monitoring/device/(?P<device_id>[^/]+)/security-summary/$',
        views_realdata.security_summary_data,
        name="api_device_security_summary"
        ),
    re_path(
        r'^api/v1/monitoring/device/(?P<device_id>[^/]+)/real-time-traffic-summary/$',
        views_realdata.real_time_traffic_summary_data,
        name="api_device_real_time_traffic_summary"
    ),
    re_path(
        r'^api/v1/monitoring/device/(?P<device_id>[^/]+)/interface_summary/$',
        views_realdata.interface_summary_data,
        name="api_device_interface_summary"
    ),

]
