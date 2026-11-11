from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import mixins, permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.errors_api import DomainError
from apps.core.idempotency import idempotent
from apps.orders.models import Order

from . import signing, webhooks
from .refunds import RefundRejected, refund_tickets
from .serializers import (
    OrganizerOrderSerializer,
    RefundRequestSerializer,
    RefundResultSerializer,
    RefundSerializer,
)


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


class OrganizerOrderViewSet(mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """Orders for events the caller organizes. Others' orders 404."""

    serializer_class = OrganizerOrderSerializer

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Order.objects.none()
        return Order.objects.filter(event__organizer__owner=self.request.user).prefetch_related(
            "items", "payments"
        )

    @extend_schema(
        request=RefundRequestSerializer,
        responses={201: RefundResultSerializer},
        parameters=[
            OpenApiParameter("Idempotency-Key", str, OpenApiParameter.HEADER, required=True)
        ],
        description="Refund specific tickets. Amount = their purchase prices.",
    )
    @action(detail=True, methods=["post"])
    def refunds(self, request, pk=None):
        order = self.get_object()
        return self._refund(request, order.pk)

    @idempotent(scope="orders.refund")
    def _refund(self, request, order_id):
        s = RefundRequestSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        try:
            refund = refund_tickets(order_id, s.validated_data["ticket_ids"], request.user)
        except RefundRejected as exc:
            raise DomainError(exc.code, str(exc), status_code=exc.status_code) from exc
        order = Order.objects.prefetch_related("items", "payments").get(pk=order_id)
        return Response(
            {
                "order": OrganizerOrderSerializer(order).data,
                "refund": RefundSerializer(refund).data if refund else None,
            },
            status=status.HTTP_201_CREATED,
        )
