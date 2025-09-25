from django.urls import path

from . import views,views_topdevices
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
]
