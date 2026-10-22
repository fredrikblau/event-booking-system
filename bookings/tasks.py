from celery import shared_task

from . import services


@shared_task
def expire_pending_bookings() -> int:
    return services.sweep_expired_bookings()
