from django.shortcuts import get_object_or_404
from rest_framework import mixins, permissions, viewsets

from .models import Event, Organizer, TicketType
from .serializers import (
    EventSerializer,
    OrganizerSerializer,
    PublicEventSerializer,
    TicketTypeSerializer,
)


class PublicEventViewSet(viewsets.ReadOnlyModelViewSet):
    """Published events with live availability. Readable without auth."""

    permission_classes = [permissions.AllowAny]
    serializer_class = PublicEventSerializer
    queryset = (
        Event.objects.filter(is_published=True)
        .select_related("organizer")
        .prefetch_related("ticket_types")
        .order_by("starts_at", "id")
    )


class OrganizerViewSet(
    mixins.CreateModelMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    serializer_class = OrganizerSerializer

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):  # schema generation
            return Organizer.objects.none()
        return Organizer.objects.filter(owner=self.request.user).order_by("id")

    def perform_create(self, serializer):
        serializer.save(owner=self.request.user)


class OrganizerEventViewSet(
    mixins.CreateModelMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    """An organizer's own events. Someone else's event is a 404, not a 403."""

    serializer_class = EventSerializer

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Event.objects.none()
        return Event.objects.filter(organizer__owner=self.request.user).order_by("-starts_at", "id")


class OrganizerTicketTypeViewSet(
    mixins.CreateModelMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    serializer_class = TicketTypeSerializer

    def _event(self):
        return get_object_or_404(
            Event, pk=self.kwargs["event_pk"], organizer__owner=self.request.user
        )

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return TicketType.objects.none()
        return TicketType.objects.filter(
            event_id=self.kwargs["event_pk"], event__organizer__owner=self.request.user
        ).order_by("id")

    def perform_create(self, serializer):
        serializer.save(event=self._event())
