from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

from bookings.metrics import metrics_view

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/", include("bookings.urls")),
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="docs"),
    path("metrics", metrics_view, name="metrics"),
]
