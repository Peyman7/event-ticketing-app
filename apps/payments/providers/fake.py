"""A Stripe-like fake provider.

Outcome is chosen by the payment method token:
    pm_ok       -> succeeds shortly after creation
    pm_declined -> fails ("card_declined")
    pm_delayed  -> succeeds after FAKE_PROVIDER_DELAY_SECONDS (bank-transfer style)

Like a real provider it (1) honours idempotency keys, (2) keeps its own ledger
(FakeProviderObject), and (3) reports outcomes asynchronously via signed webhooks,
delivered over HTTP by a background job (see apps/payments/tasks.py).
"""

from __future__ import annotations

import secrets
import time

from django.conf import settings
from django.db import IntegrityError, transaction

from ..models import FakeProviderObject
from .base import ProviderError, ProviderPayment, ProviderRefund

OUTCOMES = {"pm_ok": "succeeded", "pm_declined": "failed", "pm_delayed": "succeeded"}


def _id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(12)}"


def _payment(obj: FakeProviderObject) -> ProviderPayment:
    d = obj.data
    return ProviderPayment(obj.id, obj.status, d["amount"], d["currency"], d.get("metadata", {}))


def build_event(event_type: str, obj: dict) -> dict:
    return {
        "id": _id("evt"),
        "type": event_type,
        "created": int(time.time()),
        "data": {"object": obj},
    }


def payment_object(obj: FakeProviderObject) -> dict:
    return {"id": obj.id, "object": "payment", "status": obj.status, **obj.data}


class FakeProvider:
    name = "fake"

    def create_payment(self, *, amount_cents, currency, payment_method, idempotency_key, metadata):
        if payment_method == "pm_provider_error":
            raise ProviderError("simulated provider outage")
        data = {
            "amount": amount_cents,
            "currency": currency,
            "payment_method": payment_method,
            "metadata": metadata,
        }
        try:
            with transaction.atomic():
                obj = FakeProviderObject.objects.create(
                    id=_id("pay"),
                    kind="payment",
                    idempotency_key=idempotency_key,
                    status="processing",
                    data=data,
                )
        except IntegrityError:
            # Same idempotency key -> same object, no second charge (Stripe semantics).
            return _payment(FakeProviderObject.objects.get(idempotency_key=idempotency_key))

        from ..tasks import fake_provider_settle

        delay = settings.FAKE_PROVIDER_DELAY_SECONDS if payment_method == "pm_delayed" else 0
        fake_provider_settle.configure(schedule_in={"seconds": delay}).defer(payment_id=obj.id)
        return _payment(obj)

    def retrieve_payment(self, payment_id):
        return _payment(FakeProviderObject.objects.get(pk=payment_id, kind="payment"))

    def cancel_payment(self, payment_id):
        from ..tasks import fake_provider_deliver

        with transaction.atomic():
            obj = FakeProviderObject.objects.select_for_update().get(pk=payment_id)
            if obj.status == "processing":
                obj.status = "canceled"
                obj.save(update_fields=["status"])
                fake_provider_deliver.defer(
                    event=build_event("payment.canceled", payment_object(obj))
                )
        return _payment(obj)

    def refund(self, *, payment_id, amount_cents, idempotency_key, metadata):
        from ..tasks import fake_provider_deliver

        with transaction.atomic():
            charge = FakeProviderObject.objects.select_for_update().get(pk=payment_id)
            existing = FakeProviderObject.objects.filter(idempotency_key=idempotency_key).first()
            if existing:
                return ProviderRefund(
                    existing.id, payment_id, existing.status, existing.data["amount"]
                )
            refunded = charge.data.get("refunded", 0)
            if charge.status != "succeeded" or refunded + amount_cents > charge.data["amount"]:
                raise ProviderError("refund exceeds captured amount")
            charge.data["refunded"] = refunded + amount_cents
            charge.save(update_fields=["data"])
            ref = FakeProviderObject.objects.create(
                id=_id("re"),
                kind="refund",
                idempotency_key=idempotency_key,
                status="succeeded",
                data={"amount": amount_cents, "payment": payment_id, "metadata": metadata},
            )
            fake_provider_deliver.defer(
                event=build_event(
                    "refund.succeeded",
                    {"id": ref.id, "object": "refund", "status": "succeeded", **ref.data},
                )
            )
        return ProviderRefund(ref.id, payment_id, "pending", amount_cents)

    # --- provider-internal: called by its own jobs ------------------------------------
    def settle(self, payment_id: str) -> dict | None:
        """Move a processing charge to its final state; returns the webhook event."""
        with transaction.atomic():
            obj = FakeProviderObject.objects.select_for_update().get(pk=payment_id)
            if obj.status != "processing":
                return None  # canceled in the meantime
            obj.status = OUTCOMES.get(obj.data["payment_method"], "failed")
            if obj.status == "failed":
                obj.data["failure_code"] = "card_declined"
            obj.save(update_fields=["status", "data"])
            kind = "payment.succeeded" if obj.status == "succeeded" else "payment.failed"
            return build_event(kind, payment_object(obj))
