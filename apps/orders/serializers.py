from rest_framework import serializers

from .models import Order, OrderItem, Ticket


class OrderLineInSerializer(serializers.Serializer):
    ticket_type = serializers.IntegerField(min_value=1)
    quantity = serializers.IntegerField(min_value=1, max_value=100)


class OrderCreateSerializer(serializers.Serializer):
    items = OrderLineInSerializer(many=True, allow_empty=False)


class OrderItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = OrderItem
        fields = ["ticket_type", "quantity", "unit_price_cents"]


class OrderSerializer(serializers.ModelSerializer):
    items = OrderItemSerializer(many=True, read_only=True)

    class Meta:
        model = Order
        fields = [
            "id",
            "event",
            "status",
            "total_cents",
            "currency",
            "expires_at",
            "paid_at",
            "created_at",
            "items",
        ]


class TicketSerializer(serializers.ModelSerializer):
    class Meta:
        model = Ticket
        fields = ["id", "code", "ticket_type", "event", "status", "checked_in_at"]
