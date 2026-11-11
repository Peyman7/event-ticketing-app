from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.catalog.models import Event

from .checkin import check_in


class CheckinRequestSerializer(serializers.Serializer):
    event = serializers.IntegerField(min_value=1)
    code = serializers.CharField(max_length=32)


class CheckinResponseSerializer(serializers.Serializer):
    result = serializers.CharField()
    ticket = serializers.IntegerField(allow_null=True)
    ticket_type = serializers.IntegerField(allow_null=True)
    checked_in_at = serializers.DateTimeField(allow_null=True)


STATUS = {"admitted": 200, "already_checked_in": 409, "ticket_refunded": 409, "unknown_ticket": 404}


class CheckinView(APIView):
    """Scan a ticket at the door. Only the event's organizer may scan."""

    @extend_schema(request=CheckinRequestSerializer, responses={200: CheckinResponseSerializer})
    def post(self, request):
        s = CheckinRequestSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        event = get_object_or_404(
            Event, pk=s.validated_data["event"], organizer__owner=request.user
        )
        res = check_in(event.pk, s.validated_data["code"], request.user)
        return Response(
            {
                "result": res.code,
                "ticket": res.ticket.pk if res.ticket else None,
                "ticket_type": res.ticket.ticket_type_id if res.ticket else None,
                "checked_in_at": res.checked_in_at,
            },
            status=STATUS[res.code],
        )
