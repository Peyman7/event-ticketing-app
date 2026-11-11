from django.urls import path

from .views import EventReportView, EventTimelineView

urlpatterns = [
    path("organizer/events/<int:event_pk>/report/", EventReportView.as_view(), name="event-report"),
    path(
        "organizer/events/<int:event_pk>/report/timeline/",
        EventTimelineView.as_view(),
        name="event-timeline",
    ),
]
