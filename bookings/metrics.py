from django.db import DatabaseError
from django.http import HttpResponse
from django.utils import timezone
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from prometheus_client.core import GaugeMetricFamily

BOOKING_REQUESTS = Counter("booking_requests", "Booking attempts by result", ["result"])
BOOKING_TRANSITIONS = Counter("booking_transitions", "Bookings confirmed or cancelled", ["to"])
LOCK_WAIT = Histogram(
    "booking_lock_wait_seconds",
    "Time spent waiting for the event row lock",
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5),
)

# show every series at 0 from startup instead of only after the first hit
for result in ("created", "existing", "sold_out", "conflict"):
    BOOKING_REQUESTS.labels(result)
for status in ("confirmed", "cancelled"):
    BOOKING_TRANSITIONS.labels(status)


class OverduePendingCollector:
    # read from the db on every scrape, so it works no matter which process runs the sweep
    def describe(self):
        yield GaugeMetricFamily("bookings_overdue_pending", "PENDING bookings past expires_at")

    def collect(self):
        from .models import Booking

        try:
            count = Booking.objects.overdue(timezone.now()).count()
        except DatabaseError:
            return  # skip the gauge, keep serving the other metrics
        yield GaugeMetricFamily("bookings_overdue_pending", "PENDING bookings past expires_at", value=count)


def metrics_view(request):
    return HttpResponse(generate_latest(), content_type=CONTENT_TYPE_LATEST)
