from django.urls import path

from .views import PaymentWebhookView

urlpatterns = [path("webhooks/payments/", PaymentWebhookView.as_view(), name="payment-webhook")]
