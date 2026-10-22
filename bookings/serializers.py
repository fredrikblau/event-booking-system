from django.utils import timezone
from rest_framework import serializers

from .models import Booking, Event


class EventSerializer(serializers.ModelSerializer):
    active_bookings = serializers.IntegerField(read_only=True)
    confirmed_bookings = serializers.IntegerField(read_only=True)
    remaining_capacity = serializers.SerializerMethodField()

    class Meta:
        model = Event
        fields = [
            "id", "title", "description", "capacity", "event_date",
            "active_bookings", "confirmed_bookings", "remaining_capacity", "created_at",
        ]

    def get_remaining_capacity(self, event) -> int:
        return event.capacity - event.active_bookings

    def validate_event_date(self, value):
        if value <= timezone.now():
            raise serializers.ValidationError("Event date must be in the future.")
        return value


class BookingSerializer(serializers.ModelSerializer):
    status = serializers.ChoiceField(source="effective_status", choices=Booking.Status.choices, read_only=True)

    class Meta:
        model = Booking
        fields = ["id", "event", "status", "expires_at", "created_at", "updated_at"]
        read_only_fields = fields


class BookingCreateSerializer(serializers.Serializer):
    # the Idempotency-Key header comes in through context["idempotency_key"]
    event_id = serializers.PrimaryKeyRelatedField(queryset=Event.objects.all())

    def validate(self, attrs):
        key = self.context.get("idempotency_key") or None
        max_length = Booking._meta.get_field("idempotency_key").max_length
        if key and len(key) > max_length:
            raise serializers.ValidationError({"Idempotency-Key": [f"Must be at most {max_length} characters."]})
        if key and "\x00" in key:
            raise serializers.ValidationError({"Idempotency-Key": ["Must not contain NUL characters."]})
        attrs["idempotency_key"] = key
        return attrs
