import time

from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.core.errors_api import DomainError
from apps.core.idempotency import HEADER, REPLAY_HEADER, idempotent
from apps.core.metrics import CHECKOUT_FAILURES, CHECKOUT_LATENCY
from apps.core.state_machine import InvalidTransition

from . import services
from .inventory import SoldOut
from .models import Order, Ticket
from .pricing import LineRequest, PricingError
from .serializers import OrderCreateSerializer, OrderSerializer, TicketSerializer

IDEMPOTENCY_PARAM = OpenApiParameter(HEADER, str, OpenApiParameter.HEADER, required=True)


def _outcome(response) -> str:
    if response.headers.get(REPLAY_HEADER):
        return "replayed"
    code = response.status_code
    if code == 201:
        return "created"
    if isinstance(response.data, dict) and response.data.get("code"):
        return str(response.data["code"])
    return f"http_{code}"


class OrderViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """A buyer's own orders. Other buyers' orders 404."""

    serializer_class = OrderSerializer

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Order.objects.none()
        return (
            Order.objects.filter(buyer=self.request.user)
            .prefetch_related("items")
            .order_by("-created_at")
        )

    @extend_schema(
        request=OrderCreateSerializer,
        responses={201: OrderSerializer},
        parameters=[IDEMPOTENCY_PARAM],
        description="Hold tickets for HOLD_MINUTES. Requires an Idempotency-Key header.",
    )
    def create(self, request, *args, **kwargs):
        start = time.perf_counter()
        response = self._create(request)
        outcome = _outcome(response)
        CHECKOUT_LATENCY.labels(outcome=outcome).observe(time.perf_counter() - start)
        if outcome not in ("created", "replayed"):
            CHECKOUT_FAILURES.labels(reason=outcome).inc()
        return response

    @idempotent(scope="orders.create")
    def _create(self, request):
        s = OrderCreateSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        lines = [LineRequest(i["ticket_type"], i["quantity"]) for i in s.validated_data["items"]]
        try:
            order = services.place_order(request.user, lines)
        except SoldOut as exc:
            raise DomainError(
                "sold_out", str(exc), ticket_type=exc.ticket_type_id, available=exc.available
            ) from exc
        except PricingError as exc:
            raise DomainError(exc.code, str(exc), status_code=400) from exc
        return Response(OrderSerializer(order).data, status=status.HTTP_201_CREATED)

    @extend_schema(request=None, responses={200: OrderSerializer})
    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        order = self.get_object()  # 404 unless it's the caller's order
        try:
            order = services.cancel_order(order.pk, actor=request.user)
        except InvalidTransition as exc:
            raise DomainError("invalid_state", str(exc)) from exc
        return Response(OrderSerializer(order).data)

    @extend_schema(responses={200: TicketSerializer(many=True)})
    @action(detail=True, methods=["get"])
    def tickets(self, request, pk=None):
        order = self.get_object()
        tickets = Ticket.objects.filter(order_item__order=order).order_by("id")
        return Response(TicketSerializer(tickets, many=True).data)
