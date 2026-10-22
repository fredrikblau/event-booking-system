from rest_framework.routers import DefaultRouter

from .views import BookingViewSet, EventViewSet

router = DefaultRouter()
router.register("events", EventViewSet, basename="event")
router.register("bookings", BookingViewSet, basename="booking")

urlpatterns = router.urls
