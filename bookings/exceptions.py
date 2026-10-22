from django.core.exceptions import ObjectDoesNotExist
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

from .services import BookingError


def exception_handler(exc, context):
    if isinstance(exc, BookingError):
        return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
    if isinstance(exc, ObjectDoesNotExist):
        return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
    return drf_exception_handler(exc, context)
