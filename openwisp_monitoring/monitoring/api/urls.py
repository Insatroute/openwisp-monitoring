from django.urls import path

from . import views
from .views_topdevices import *


app_name = "monitoring_general"

urlpatterns = [
    path(
        "api/v1/monitoring/dashboard/",
        views.dashboard_timeseries,
        name="api_dashboard_timeseries",
    ),
    path("top-devices-simple/", top_devices_simple, name="monitoring-top-devices-simple"),
]
