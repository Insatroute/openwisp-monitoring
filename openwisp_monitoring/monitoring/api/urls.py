from django.urls import path

from . import views,views_topdevices, views_topapp
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
    path("api/v1/monitoring/global-top-apps/", views_topapp.global_top_apps_view, name="api_global_top_apps"),

    path(
 "api/v1/monitoring/traffic-summary/",
views_topapp.traffic_summary_view,
name="api_global_traffic_summary",
 ),

]
