from django.conf import settings
from django.core.mail import send_mail

from .models import Order, Ticket


def send_order_confirmation(order_id: int) -> None:
    order = Order.objects.select_related("buyer", "event").get(pk=order_id)
    codes = Ticket.objects.filter(order_item__order=order).values_list("code", flat=True)
    body = "\n".join(
        [f"Thanks for your order #{order.pk} for {order.event.title}.", "Your tickets:", *codes]
    )
    send_mail(
        f"Your tickets for {order.event.title}",
        body,
        settings.DEFAULT_FROM_EMAIL,
        [order.buyer.email],
    )
