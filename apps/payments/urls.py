from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import OrganizerOrderViewSet, PaymentWebhookView

router = DefaultRouter()
router.register("organizer/orders", OrganizerOrderViewSet, basename="organizer-order")

urlpatterns = [
    path("webhooks/payments/", PaymentWebhookView.as_view(), name="payment-webhook"),
    *router.urls,
]
