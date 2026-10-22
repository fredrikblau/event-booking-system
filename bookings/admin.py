from django.contrib import admin

from .models import Booking, Event


@admin.register(Event)
class EventAdmin(admin.ModelAdmin):
    list_display = ["id", "title", "capacity", "event_date"]


@admin.register(Booking)
class BookingAdmin(admin.ModelAdmin):
    list_display = ["id", "event", "user", "status", "expires_at"]
    list_filter = ["status"]
