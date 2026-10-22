from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.pagination import CursorPagination
from rest_framework.response import Response

from . import services
from .models import Booking, Event
from .serializers import BookingCreateSerializer, BookingSerializer, EventSerializer


class EventPagination(CursorPagination):
    page_size = 20
    ordering = ("event_date", "id")


class BookingPagination(CursorPagination):
    page_size = 20
    ordering = ("-created_at", "-id")


class EventViewSet(mixins.CreateModelMixin, mixins.RetrieveModelMixin, mixins.ListModelMixin, viewsets.GenericViewSet):
    serializer_class = EventSerializer
    pagination_class = EventPagination
    lookup_value_regex = r"\d+"

    def get_queryset(self):
        return Event.objects.with_stats()

    def perform_create(self, serializer):
        event = serializer.save()
        event.active_bookings = event.confirmed_bookings = 0  # nothing booked yet


class BookingViewSet(mixins.RetrieveModelMixin, mixins.ListModelMixin, viewsets.GenericViewSet):
    serializer_class = BookingSerializer
    pagination_class = BookingPagination
    lookup_value_regex = r"\d+"

    def get_queryset(self):
        return Booking.objects.filter(user_id=self.request.user.id)

    @extend_schema(
        request=BookingCreateSerializer,
        parameters=[
            OpenApiParameter(
                "Idempotency-Key", str, OpenApiParameter.HEADER,
                description="Optional. Same key returns the original booking.",
            )
        ],
        responses={
            201: OpenApiResponse(BookingSerializer, "Booking created"),
            200: OpenApiResponse(BookingSerializer, "Existing booking returned (retry)"),
            400: OpenApiResponse(description="Invalid event_id or Idempotency-Key too long"),
            409: OpenApiResponse(description="Sold out, event started, key reused, or already booked under another key"),
        },
    )
    def create(self, request):
        serializer = BookingCreateSerializer(
            data=request.data, context={"idempotency_key": request.headers.get("Idempotency-Key")}
        )
        serializer.is_valid(raise_exception=True)
        booking, created = services.book(
            user=request.user,
            event_id=serializer.validated_data["event_id"].pk,
            idempotency_key=serializer.validated_data["idempotency_key"],
        )
        return Response(
            BookingSerializer(booking).data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )

    @extend_schema(request=None, responses={200: BookingSerializer, 409: OpenApiResponse(description="Not pending or expired")})
    @action(detail=True, methods=["post"])
    def confirm(self, request, pk=None):
        return Response(BookingSerializer(services.confirm(user=request.user, booking_id=pk)).data)

    @extend_schema(request=None, responses={200: BookingSerializer, 409: OpenApiResponse(description="Booking is not active")})
    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        return Response(BookingSerializer(services.cancel(user=request.user, booking_id=pk)).data)
