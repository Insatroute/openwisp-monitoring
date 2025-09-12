from django.urls import include, path
from .views import map_view,devices_geojson

urlpatterns = [
    path("", include("openwisp_monitoring.device.api.urls", namespace="monitoring")),
    # The following endpoint was developed after "openwisp_monitoring.device.api"
    # which already used the "monitoring" namespace. The "monitoring_general"
    # namespace is used below to avoid changing the old naming scheme and
    # maintain backward compatibility.
    path(
        "",
        include(
            "openwisp_monitoring.monitoring.api.urls", namespace="monitoring_general"
        ),
    ),
    path("map/", map_view, name="map_view"),
    path("map/devices.geojson",devices_geojson, name="devices_geojson"),

]
