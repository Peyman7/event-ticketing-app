from django.urls import path
from rest_framework.routers import DefaultRouter

from .checkin_views import CheckinView
from .views import OrderViewSet

router = DefaultRouter()
router.register("orders", OrderViewSet, basename="order")
urlpatterns = [*router.urls, path("checkin/", CheckinView.as_view(), name="checkin")]
