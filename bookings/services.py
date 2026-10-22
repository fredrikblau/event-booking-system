from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from .metrics import BOOKING_REQUESTS, BOOKING_TRANSITIONS, LOCK_WAIT
from .models import Booking, Event


class BookingError(Exception):
    pass


class SoldOut(BookingError):
    pass


def expire_stale_bookings(queryset, now) -> int:
    # single conditional UPDATE, safe to run twice or alongside confirm/cancel
    return queryset.overdue(now).update(status=Booking.Status.EXPIRED, updated_at=now)


def sweep_expired_bookings(batch_size=1000) -> int:
    # expires in small batches so a big wave of expiries doesn't lock thousands of rows
    # in one statement. rows locked by an in-flight confirm/cancel are skipped and
    # picked up on the next run.
    total = 0
    while True:
        now = timezone.now()
        with transaction.atomic():
            batch = (
                Booking.objects.overdue(now)
                .select_for_update(skip_locked=True)
                .values("pk")[:batch_size]
            )
            count = Booking.objects.filter(pk__in=batch).update(status=Booking.Status.EXPIRED, updated_at=now)
        total += count
        if count < batch_size:
            return total


def book(*, user, event_id, idempotency_key=None) -> tuple[Booking, bool]:
    # returns (booking, created). retries get the existing booking back
    try:
        booking, created = _book(user, event_id, idempotency_key)
    except SoldOut:
        BOOKING_REQUESTS.labels("sold_out").inc()
        raise
    except BookingError:
        BOOKING_REQUESTS.labels("conflict").inc()
        raise
    BOOKING_REQUESTS.labels("created" if created else "existing").inc()
    return booking, created


def _book(user, event_id, idempotency_key):
    if idempotency_key:
        existing = Booking.objects.filter(user=user, idempotency_key=idempotency_key).first()
        if existing:
            if existing.event_id != event_id:
                raise BookingError("Idempotency-Key was already used for a different event.")
            return existing, False

    try:
        with transaction.atomic():
            # lock the event row so count + insert can't race
            with LOCK_WAIT.time():
                event = Event.objects.select_for_update().get(pk=event_id)
            now = timezone.now()
            if event.event_date <= now:
                raise BookingError("Event has already started.")

            expire_stale_bookings(event.bookings.all(), now)

            existing = event.bookings.active(now).filter(user=user).first()
            if existing:
                if idempotency_key and existing.idempotency_key != idempotency_key:
                    # a new key is a new request, not a retry of the one that made this booking
                    raise BookingError("You already have an active booking for this event.")
                return existing, False

            if event.bookings.active(now).count() >= event.capacity:
                raise SoldOut("Event is sold out.")

            booking = Booking.objects.create(
                event=event,
                user=user,
                expires_at=now + timedelta(seconds=settings.BOOKING_PENDING_TTL_SECONDS),
                idempotency_key=idempotency_key or None,
            )
    except IntegrityError:
        # same key used on two events at the same time
        raise BookingError("Duplicate request.")
    return booking, True


def _get_locked_booking(user, booking_id) -> Booking:
    return Booking.objects.select_for_update().get(pk=booking_id, user=user)


@transaction.atomic
def confirm(*, user, booking_id) -> Booking:
    booking = _get_locked_booking(user, booking_id)
    if booking.status == Booking.Status.CONFIRMED:
        return booking
    if booking.status != Booking.Status.PENDING:
        raise BookingError(f"Only PENDING bookings can be confirmed (status is {booking.status}).")
    if not booking.is_active():
        raise BookingError("Booking has expired.")
    booking.status = Booking.Status.CONFIRMED
    booking.save(update_fields=["status", "updated_at"])
    BOOKING_TRANSITIONS.labels("confirmed").inc()
    return booking


@transaction.atomic
def cancel(*, user, booking_id) -> Booking:
    booking = _get_locked_booking(user, booking_id)
    if booking.status == Booking.Status.CANCELLED:
        return booking
    if not booking.is_active():
        raise BookingError(f"Only PENDING or CONFIRMED bookings can be cancelled (status is {booking.effective_status}).")
    booking.status = Booking.Status.CANCELLED
    booking.save(update_fields=["status", "updated_at"])
    BOOKING_TRANSITIONS.labels("cancelled").inc()
    return booking
