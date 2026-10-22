from django.apps import AppConfig
from prometheus_client import REGISTRY


class BookingsConfig(AppConfig):
    name = "bookings"

    def ready(self):
        from .metrics import OverduePendingCollector

        REGISTRY.register(OverduePendingCollector())
