from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Count, OuterRef, Q, Subquery
from django.db.models.functions import Coalesce
from django.utils import timezone


def _booking_count(bookings):
    bookings = bookings.filter(event=OuterRef("pk")).order_by().values("event")
    return Coalesce(Subquery(bookings.annotate(n=Count("pk")).values("n")), 0)


class EventQuerySet(models.QuerySet):
    def with_stats(self):
        # overdue PENDING bookings don't count even if the sweep hasn't marked them yet.
        # subqueries instead of join + GROUP BY so a paginated list only counts the
        # events on the page
        return self.annotate(
            active_bookings=_booking_count(Booking.objects.active(timezone.now())),
            confirmed_bookings=_booking_count(Booking.objects.filter(status=Booking.Status.CONFIRMED)),
        )


class Event(models.Model):
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    capacity = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    event_date = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    objects = EventQuerySet.as_manager()

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(capacity__gt=0), name="event_capacity_positive"),
        ]
        indexes = [
            models.Index(fields=["event_date", "id"], name="event_date_idx"),
        ]

    def __str__(self):
        return self.title


class BookingQuerySet(models.QuerySet):
    # overdue PENDING bookings are not active, even before the sweep marks them EXPIRED
    def active(self, now):
        return self.filter(
            Q(status=Booking.Status.CONFIRMED) | Q(status=Booking.Status.PENDING, expires_at__gt=now)
        )

    def overdue(self, now):
        return self.filter(status=Booking.Status.PENDING, expires_at__lte=now)


class Booking(models.Model):
    class Status(models.TextChoices):
        PENDING = "PENDING"
        CONFIRMED = "CONFIRMED"
        CANCELLED = "CANCELLED"
        EXPIRED = "EXPIRED"

    event = models.ForeignKey(Event, on_delete=models.PROTECT, related_name="bookings")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="bookings")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    expires_at = models.DateTimeField()
    idempotency_key = models.CharField(max_length=64, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = BookingQuerySet.as_manager()

    class Meta:
        constraints = [
            # one active booking per user per event
            models.UniqueConstraint(
                fields=["event", "user"],
                condition=Q(status__in=["PENDING", "CONFIRMED"]),
                name="one_active_booking_per_user_event",
            ),
            models.UniqueConstraint(fields=["user", "idempotency_key"], name="unique_idempotency_key_per_user"),
        ]
        indexes = [
            models.Index(fields=["event", "status"], name="booking_event_status_idx"),
            models.Index(fields=["user", "-created_at", "-id"], name="booking_user_created_idx"),
            # for the expiry sweep
            models.Index(fields=["expires_at"], condition=Q(status="PENDING"), name="booking_pending_expiry_idx"),
        ]

    def __str__(self):
        return f"Booking #{self.pk} ({self.status})"

    @property
    def effective_status(self):
        # an overdue PENDING booking is already EXPIRED, even if the sweep hasn't written it yet
        if self.status == self.Status.PENDING and not self.is_active():
            return self.Status.EXPIRED
        return self.status

    def is_active(self, now=None):
        if self.status == self.Status.CONFIRMED:
            return True
        return self.status == self.Status.PENDING and self.expires_at > (now or timezone.now())
