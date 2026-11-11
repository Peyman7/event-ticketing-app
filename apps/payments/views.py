from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from . import signing, webhooks


@method_decorator(csrf_exempt, name="dispatch")
class PaymentWebhookView(APIView):
    """Provider -> us. Authenticated by HMAC signature, not by a user token."""

    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    @extend_schema(
        request=None,
        responses={200: OpenApiResponse(description="accepted or duplicate"), 400: None},
    )
    def post(self, request):
        body = request.body  # raw bytes: the signature covers exactly these
        try:
            created, event_id = webhooks.receive(body, request.headers.get(signing.HEADER, ""))
        except webhooks.BadWebhook as exc:
            return Response({"detail": str(exc)}, status=400)
        return Response({"event_id": event_id, "duplicate": not created})
