"""The narrow interface our domain code uses to talk to a payment provider.

Every mutating call takes an idempotency key, mirroring Stripe: retrying a call with the
same key returns the original object instead of charging/refunding twice.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


class ProviderError(Exception):
    """Transient provider failure (network, 5xx). Safe to retry with the same key."""


@dataclass(frozen=True)
class ProviderPayment:
    id: str
    status: str  # processing | succeeded | failed | canceled
    amount_cents: int
    currency: str
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ProviderRefund:
    id: str
    payment_id: str
    status: str  # pending | succeeded | failed
    amount_cents: int


class PaymentProvider(Protocol):
    name: str

    def create_payment(
        self,
        *,
        amount_cents: int,
        currency: str,
        payment_method: str,
        idempotency_key: str,
        metadata: dict,
    ) -> ProviderPayment: ...

    def retrieve_payment(self, payment_id: str) -> ProviderPayment: ...

    def cancel_payment(self, payment_id: str) -> ProviderPayment: ...

    def refund(
        self, *, payment_id: str, amount_cents: int, idempotency_key: str, metadata: dict
    ) -> ProviderRefund: ...
