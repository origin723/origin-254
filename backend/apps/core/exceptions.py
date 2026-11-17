import logging

from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import exception_handler

logger = logging.getLogger("origin254")


class WorkflowError(Exception):
    """Raised by service functions when an action is not allowed in the current state."""

    def __init__(self, message, code="invalid_state"):
        super().__init__(message)
        self.message = message
        self.code = code


def api_exception_handler(exc, context):
    """Consistent error envelope: {"error": {"code", "message", "details"}}."""
    if isinstance(exc, WorkflowError):
        return Response({"error": {"code": exc.code, "message": exc.message, "details": {}}},
                        status=status.HTTP_409_CONFLICT)
    if isinstance(exc, DjangoValidationError):
        details = exc.message_dict if hasattr(exc, "error_dict") else {"non_field_errors": exc.messages}
        return Response({"error": {"code": "validation_error", "message": "Invalid input.", "details": details}},
                        status=status.HTTP_400_BAD_REQUEST)
    if isinstance(exc, DjangoPermissionDenied):
        return Response({"error": {"code": "permission_denied", "message": str(exc) or "Not allowed.", "details": {}}},
                        status=status.HTTP_403_FORBIDDEN)

    response = exception_handler(exc, context)
    if response is None:
        logger.exception("Unhandled API error", exc_info=exc)
        return Response({"error": {"code": "server_error", "message": "An unexpected error occurred.", "details": {}}},
                        status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    data = response.data
    code = getattr(exc, "default_code", "error")
    if isinstance(data, dict) and set(data.keys()) == {"detail"}:
        message, details = str(data["detail"]), {}
    else:
        message, details = ("Invalid input." if response.status_code == 400 else "Request failed."), data
    response.data = {"error": {"code": code, "message": message, "details": details}}
    return response
