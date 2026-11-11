from django.shortcuts import get_object_or_404
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.catalog.models import Event

from . import queries


def _own_event(request, event_id: int) -> Event:
    return get_object_or_404(Event, pk=event_id, organizer__owner=request.user)


class EventReportView(APIView):
    """Sales, revenue, refunds and attendance per ticket type for one event."""

    @extend_schema(responses={200: OpenApiTypes.OBJECT})
    def get(self, request, event_pk: int):
        event = _own_event(request, event_pk)
        return Response(
            {"event": event.pk, "currency": event.currency, **queries.event_report(event.pk)}
        )


class EventTimelineView(APIView):
    """Tickets sold and gross revenue per hour/day, including empty buckets."""

    @extend_schema(
        parameters=[OpenApiParameter("bucket", str, enum=["hour", "day"], default="hour")],
        responses={200: OpenApiTypes.OBJECT},
    )
    def get(self, request, event_pk: int):
        event = _own_event(request, event_pk)
        unit = request.query_params.get("bucket", "hour")
        if unit not in queries.BUCKETS:
            return Response(
                {"code": "invalid_bucket", "detail": "bucket must be hour or day"}, status=400
            )
        return Response(
            {"event": event.pk, "bucket": unit, "series": queries.sales_timeline(event.pk, unit)}
        )
