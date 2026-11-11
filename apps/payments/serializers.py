from rest_framework import serializers

from apps.orders.models import Order
from apps.orders.serializers import OrderItemSerializer, TicketSerializer

from .models import Payment, Refund


class RefundRequestSerializer(serializers.Serializer):
    ticket_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1), allow_empty=False, max_length=200
    )


class RefundSerializer(serializers.ModelSerializer):
    tickets = serializers.PrimaryKeyRelatedField(many=True, read_only=True)

    class Meta:
        model = Refund
        fields = ["id", "amount_cents", "status", "reason", "tickets", "created_at"]


class PaymentSummarySerializer(serializers.ModelSerializer):
    class Meta:
        model = Payment
        fields = ["id", "status", "amount_cents", "refunded_cents", "currency"]


class OrganizerOrderSerializer(serializers.ModelSerializer):
    items = OrderItemSerializer(many=True, read_only=True)
    tickets = serializers.SerializerMethodField()
    payments = PaymentSummarySerializer(many=True, read_only=True)

    class Meta:
        model = Order
        fields = [
            "id",
            "event",
            "buyer",
            "status",
            "total_cents",
            "currency",
            "paid_at",
            "items",
            "tickets",
            "payments",
        ]

    def get_tickets(self, order) -> list[dict]:
        from apps.orders.models import Ticket

        qs = Ticket.objects.filter(order_item__order=order).order_by("id")
        return TicketSerializer(qs, many=True).data


class RefundResultSerializer(serializers.Serializer):
    order = OrganizerOrderSerializer()
    refund = RefundSerializer(allow_null=True)
