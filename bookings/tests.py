import threading
from unittest import mock
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import DatabaseError, connection, transaction
from django.test import TestCase, TransactionTestCase
from django.utils import timezone
from prometheus_client import REGISTRY
from rest_framework.test import APIClient

from . import services
from .models import Booking, Event
from .tasks import expire_pending_bookings

User = get_user_model()


def make_event(capacity=2):
    return Event.objects.create(title="Concert", capacity=capacity, event_date=timezone.now() + timedelta(days=1))


class BookingApiTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("alice")
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.event = make_event(capacity=1)

    def book(self, key=None, event=None):
        headers = {"HTTP_IDEMPOTENCY_KEY": key} if key else {}
        return self.client.post("/api/bookings/", {"event_id": (event or self.event).pk}, format="json", **headers)

    def test_create_event_rejects_non_positive_capacity(self):
        data = {"title": "x", "capacity": 0, "event_date": (timezone.now() + timedelta(days=1)).isoformat()}
        self.assertEqual(self.client.post("/api/events/", data, format="json").status_code, 400)

    def test_create_event_returns_stats(self):
        data = {"title": "x", "capacity": 5, "event_date": (timezone.now() + timedelta(days=1)).isoformat()}
        res = self.client.post("/api/events/", data, format="json")
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.data["remaining_capacity"], 5)

    def test_booking_is_pending_with_deadline(self):
        res = self.book()
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.data["status"], "PENDING")

    def test_retry_returns_same_booking(self):
        first = self.book(key="k1")
        retry = self.book(key="k1")
        no_key_retry = self.book()
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(first.data["id"], retry.data["id"])
        self.assertEqual(first.data["id"], no_key_retry.data["id"])
        self.assertEqual(Booking.objects.count(), 1)

    def test_too_long_idempotency_key_is_rejected(self):
        self.assertEqual(self.book(key="x" * 65).status_code, 400)
        self.assertEqual(Booking.objects.count(), 0)

    def test_nul_in_idempotency_key_is_rejected(self):
        self.assertEqual(self.book(key="a\x00b").status_code, 400)

    def test_new_key_with_active_booking_conflicts(self):
        self.book(key="k1")
        self.assertEqual(self.book(key="k2").status_code, 409)
        self.assertEqual(Booking.objects.count(), 1)

    def test_non_numeric_booking_id_is_404(self):
        self.assertEqual(self.client.post("/api/bookings/abc/confirm/").status_code, 404)
        self.assertEqual(self.client.post("/api/bookings/abc/cancel/").status_code, 404)

    def test_capacity_out_of_db_range_is_rejected(self):
        data = {"title": "x", "capacity": 3_000_000_000, "event_date": (timezone.now() + timedelta(days=1)).isoformat()}
        self.assertEqual(self.client.post("/api/events/", data, format="json").status_code, 400)

    def test_metrics_survive_db_errors(self):
        with mock.patch.object(Booking.objects, "overdue", side_effect=DatabaseError):
            res = self.client.get("/metrics")
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"booking_requests_total", res.content)
        self.assertNotIn(b"bookings_overdue_pending ", res.content)

    def test_idempotency_key_reused_for_other_event_conflicts(self):
        self.book(key="k1")
        self.assertEqual(self.book(key="k1", event=make_event()).status_code, 409)

    def test_sold_out(self):
        self.book()
        other = APIClient()
        other.force_authenticate(User.objects.create_user("bob"))
        res = other.post("/api/bookings/", {"event_id": self.event.pk}, format="json")
        self.assertEqual(res.status_code, 409)

    def test_confirm_then_cancel_frees_capacity(self):
        booking_id = self.book().data["id"]
        self.assertEqual(self.client.post(f"/api/bookings/{booking_id}/confirm/").data["status"], "CONFIRMED")
        detail = self.client.get(f"/api/events/{self.event.pk}/").data
        self.assertEqual((detail["active_bookings"], detail["confirmed_bookings"], detail["remaining_capacity"]), (1, 1, 0))

        self.assertEqual(self.client.post(f"/api/bookings/{booking_id}/cancel/").data["status"], "CANCELLED")
        self.assertEqual(self.client.get(f"/api/events/{self.event.pk}/").data["remaining_capacity"], 1)
        self.assertEqual(self.client.post(f"/api/bookings/{booking_id}/confirm/").status_code, 409)
        self.assertEqual(self.book().status_code, 201)  # can rebook after cancelling

    def test_cannot_touch_other_users_booking(self):
        booking_id = self.book().data["id"]
        other = APIClient()
        other.force_authenticate(User.objects.create_user("bob"))
        self.assertEqual(other.post(f"/api/bookings/{booking_id}/cancel/").status_code, 404)

    def test_expired_booking_cannot_be_confirmed_and_frees_capacity(self):
        booking_id = self.book().data["id"]
        Booking.objects.filter(pk=booking_id).update(expires_at=timezone.now() - timedelta(seconds=1))

        # seat is free and the booking reads as EXPIRED before the worker runs
        self.assertEqual(self.client.get(f"/api/events/{self.event.pk}/").data["remaining_capacity"], 1)
        self.assertEqual(self.client.get(f"/api/bookings/{booking_id}/").data["status"], "EXPIRED")
        self.assertEqual(self.client.post(f"/api/bookings/{booking_id}/confirm/").status_code, 409)

        self.assertEqual(expire_pending_bookings(), 1)
        self.assertEqual(expire_pending_bookings(), 0)  # retry-safe
        self.assertEqual(Booking.objects.get(pk=booking_id).status, "EXPIRED")

    def test_sweep_runs_in_batches_until_done(self):
        users = [User.objects.create_user(f"u{i}") for i in range(5)]
        event = make_event(capacity=5)
        for user in users:
            services.book(user=user, event_id=event.pk)
        Booking.objects.update(expires_at=timezone.now() - timedelta(seconds=1))

        self.assertEqual(services.sweep_expired_bookings(batch_size=2), 5)
        self.assertEqual(Booking.objects.filter(status="EXPIRED").count(), 5)

    def test_lists_are_paginated(self):
        for _ in range(25):
            make_event()
        first = self.client.get("/api/events/").data
        self.assertEqual(len(first["results"]), 20)
        second = self.client.get(first["next"]).data
        self.assertEqual(len(second["results"]), 6)
        self.assertIsNone(second["next"])
        ids = [e["id"] for e in first["results"] + second["results"]]
        self.assertEqual(len(set(ids)), 26)

    def test_metrics(self):
        def sample(name, **labels):
            return REGISTRY.get_sample_value(name, labels) or 0

        before = {r: sample("booking_requests_total", result=r) for r in ("created", "existing", "sold_out")}
        booking_id = self.book().data["id"]
        self.book()
        other = APIClient()
        other.force_authenticate(User.objects.create_user("bob"))
        other.post("/api/bookings/", {"event_id": self.event.pk}, format="json")
        Booking.objects.filter(pk=booking_id).update(expires_at=timezone.now() - timedelta(seconds=1))

        self.assertEqual(sample("booking_requests_total", result="created") - before["created"], 1)
        self.assertEqual(sample("booking_requests_total", result="existing") - before["existing"], 1)
        self.assertEqual(sample("booking_requests_total", result="sold_out") - before["sold_out"], 1)

        res = self.client.get("/metrics")
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"bookings_overdue_pending 1.0", res.content)
        self.assertIn(b"booking_lock_wait_seconds_count", res.content)

    def test_expiry_does_not_touch_confirmed(self):
        booking_id = self.book().data["id"]
        self.client.post(f"/api/bookings/{booking_id}/confirm/")
        Booking.objects.filter(pk=booking_id).update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(expire_pending_bookings(), 0)


class ConcurrencyTests(TransactionTestCase):
    def run_concurrently(self, fn, args_list):
        barrier = threading.Barrier(len(args_list))
        results = []

        def worker(*args):
            try:
                barrier.wait()
                results.append(fn(*args))
            except services.BookingError as exc:
                results.append(exc)
            finally:
                connection.close()

        threads = [threading.Thread(target=worker, args=args) for args in args_list]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return results

    def test_no_overselling(self):
        event = make_event(capacity=5)
        users = [User.objects.create_user(f"u{i}") for i in range(30)]
        results = self.run_concurrently(lambda u: services.book(user=u, event_id=event.pk), [(u,) for u in users])

        self.assertEqual(sum(1 for r in results if isinstance(r, tuple)), 5)
        self.assertEqual(Booking.objects.active(timezone.now()).filter(event=event).count(), 5)

    def test_same_user_parallel_retries_create_one_booking(self):
        event = make_event(capacity=5)
        user = User.objects.create_user("alice")
        results = self.run_concurrently(
            lambda: services.book(user=user, event_id=event.pk, idempotency_key="k"), [()] * 10
        )
        self.assertEqual(Booking.objects.filter(event=event).count(), 1)
        # every retry gets the same booking back, none of them a 409
        self.assertTrue(all(isinstance(r, tuple) for r in results), results)
        self.assertEqual({booking.pk for booking, _ in results}, {Booking.objects.get(event=event).pk})

    def test_sweep_skips_rows_locked_by_confirm(self):
        event = make_event(capacity=1)
        user = User.objects.create_user("alice")
        booking, _ = services.book(user=user, event_id=event.pk)
        Booking.objects.filter(pk=booking.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
        locked, release = threading.Event(), threading.Event()

        def hold_lock():
            with transaction.atomic():
                Booking.objects.select_for_update().get(pk=booking.pk)
                locked.set()
                release.wait(5)
            connection.close()

        thread = threading.Thread(target=hold_lock)
        thread.start()
        locked.wait(5)
        self.assertEqual(services.sweep_expired_bookings(), 0)  # doesn't block, doesn't touch it
        release.set()
        thread.join()
        self.assertEqual(services.sweep_expired_bookings(), 1)
