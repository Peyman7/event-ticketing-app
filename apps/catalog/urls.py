from django.urls import path
from rest_framework.routers import DefaultRouter

from . import views

router = DefaultRouter(trailing_slash=True)
router.register("events", views.PublicEventViewSet, basename="event")
router.register("organizers", views.OrganizerViewSet, basename="organizer")
router.register("organizer/events", views.OrganizerEventViewSet, basename="organizer-event")

tt_list = views.OrganizerTicketTypeViewSet.as_view({"get": "list", "post": "create"})
tt_detail = views.OrganizerTicketTypeViewSet.as_view(
    {"get": "retrieve", "put": "update", "patch": "partial_update"}
)

urlpatterns = [
    *router.urls,
    path("organizer/events/<int:event_pk>/ticket-types/", tt_list, name="ticket-type-list"),
    path(
        "organizer/events/<int:event_pk>/ticket-types/<int:pk>/",
        tt_detail,
        name="ticket-type-detail",
    ),
]
