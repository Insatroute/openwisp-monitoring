from django.urls import path,re_path

from . import views,views_topdevices, views_topapp, views_realdata, views_dashboard
# from .views_topdevices import *


app_name = "monitoring_general"

urlpatterns = [
    path(
        "api/v1/monitoring/dashboard/",
        views.dashboard_timeseries,
        name="api_dashboard_timeseries",
    ),
    # path(
    #     "api/v1/monitoring/top-devices-simple/",
    #     views_topdevices.top_devices_simple,
    #     name="monitoring-top-devices-simple"
    # ),
    # path(
    #     "api/v1/monitoring/global-top-devices/",
    #     views_topapp.global_top_devices,
    #     name="api_global_top_devices"
    # ),
    # path(
    #     "api/v1/monitoring/global-top-apps/",
    #     views_topapp.global_top_apps,
    #     name="api_global_top_apps"
    # ),
    # path(
    #     "api/v1/monitoring/wan-uplinks/",
    #     views_topapp.wan_uplinks_all_devices,
    #     name="api_wan_uplinks_all_devices"
    # ),
    # path(
    #     "api/v1/monitoring/data-usage/",
    #     views_topapp.data_usage_all_devices,
    #     name="api_data_usage_all_devices"
    # ),
    # path(
    #     "api/v1/monitoring/mobile-distribution/",
    #     views_topapp.mobile_distribution_all_devices,
    #     name="api_mobile_distribution_all_devices"
    # ),
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
        r'^api/v1/monitoring/device/(?P<device_id>[^/]+)/wan-uplink-summary/$',
        views_realdata.wan_uplink_summary_data,
        name="api_device_wan_uplink_summary"
    ),
    re_path(
        r'^api/v1/monitoring/device/(?P<device_id>[^/]+)/cellular-summary/$',
        views_realdata.cellular_summary_data,
        name="api_device_cellular_summary"
    ),
    re_path(
        r'^api/v1/monitoring/device/(?P<device_id>[^/]+)/device-info-summary/$',
        views_realdata.device_info_summary_data,
        name="api_device_info_summary"
    ),
    re_path(
        r'^api/v1/monitoring/device/(?P<device_id>[^/]+)/interfaces-summary/$',
        views_realdata.interfaces_summary_data,
        name="api_device_interfaces_summary"
    ),
    
    path("api/v1/monitoring/global-top-apps/", views_dashboard.global_top_apps),
    path("api/v1/monitoring/global-top-devices/", views_dashboard.global_top_devices),
    path("api/v1/monitoring/wan-uplinks/", views_dashboard.wan_uplinks_all_devices),
    path("api/v1/monitoring/data-usage/", views_dashboard.data_usage_all_devices),
    path("api/v1/monitoring/mobile-distribution/", views_dashboard.mobile_distribution_all_devices),
    path("api/v1/monitoring/ipsec-tunnels-status/", views_dashboard.ipsec_tunnels_status),
]
