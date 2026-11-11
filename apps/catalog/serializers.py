from django.db import transaction
from rest_framework import serializers

from .models import Event, Organizer, TicketType


class OrganizerSerializer(serializers.ModelSerializer):
    class Meta:
        model = Organizer
        fields = ["id", "name", "created_at"]


class TicketTypeSerializer(serializers.ModelSerializer):
    available = serializers.IntegerField(read_only=True)

    class Meta:
        model = TicketType
        fields = [
            "id",
            "name",
            "price_cents",
            "capacity",
            "reserved_count",
            "available",
            "sale_starts_at",
            "sale_ends_at",
            "max_per_order",
        ]
        read_only_fields = ["reserved_count"]

    def validate(self, attrs):
        starts = attrs.get("sale_starts_at", getattr(self.instance, "sale_starts_at", None))
        ends = attrs.get("sale_ends_at", getattr(self.instance, "sale_ends_at", None))
        if starts and ends and ends <= starts:
            raise serializers.ValidationError({"sale_ends_at": "Must be after sale_starts_at."})
        if attrs.get("price_cents", 0) < 0:
            raise serializers.ValidationError({"price_cents": "Must be >= 0."})
        return attrs

    def update(self, instance, validated_data):
        # Same row lock as checkout: capacity can't be lowered below what's already held
        # or sold by a concurrent buyer between our read and our write.
        with transaction.atomic():
            locked = TicketType.objects.select_for_update().get(pk=instance.pk)
            capacity = validated_data.get("capacity", locked.capacity)
            if capacity < locked.reserved_count:
                raise serializers.ValidationError(
                    {"capacity": f"Cannot go below {locked.reserved_count} held/sold."}
                )
            return super().update(locked, validated_data)


class PublicTicketTypeSerializer(serializers.ModelSerializer):
    available = serializers.IntegerField(read_only=True)

    class Meta:
        model = TicketType
        fields = [
            "id",
            "name",
            "price_cents",
            "available",
            "sale_starts_at",
            "sale_ends_at",
            "max_per_order",
        ]


class EventSerializer(serializers.ModelSerializer):
    organizer = serializers.PrimaryKeyRelatedField(queryset=Organizer.objects.none())

    class Meta:
        model = Event
        fields = [
            "id",
            "organizer",
            "title",
            "venue",
            "starts_at",
            "currency",
            "is_published",
            "created_at",
        ]

    def get_fields(self):
        fields = super().get_fields()
        request = self.context.get("request")
        if request is not None and request.user.is_authenticated:
            # Only your own organizers are valid choices (else 400, not a silent hijack).
            fields["organizer"].queryset = Organizer.objects.filter(owner=request.user)
        if self.instance is not None:
            fields["organizer"].read_only = True
            fields["currency"].read_only = True  # prices/orders already denominated in it
        return fields

    def validate_currency(self, value):
        if len(value) != 3 or not value.isalpha() or not value.isupper():
            raise serializers.ValidationError("ISO 4217 code, e.g. USD.")
        return value


class PublicEventSerializer(serializers.ModelSerializer):
    organizer = serializers.StringRelatedField()
    ticket_types = PublicTicketTypeSerializer(many=True, read_only=True)

    class Meta:
        model = Event
        fields = ["id", "organizer", "title", "venue", "starts_at", "currency", "ticket_types"]
