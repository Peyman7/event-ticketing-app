"""A deliberately tiny explicit state machine.

Every status change in the system goes through `StateMachine.apply`, so the set of legal
transitions lives in one table that can be read, tested and discussed. Callers must hold
a row lock on the instance (select_for_update) so the from-state they read is current.
"""

from __future__ import annotations

from collections.abc import Mapping

from .audit import audit


class InvalidTransition(Exception):
    def __init__(self, machine: str, from_state: str, to_state: str):
        self.from_state, self.to_state = from_state, to_state
        super().__init__(f"{machine}: illegal transition {from_state!r} -> {to_state!r}")


class StateMachine:
    def __init__(self, name: str, transitions: Mapping[str, set[str]], field: str = "status"):
        self.name = name
        self.transitions = {k: frozenset(v) for k, v in transitions.items()}
        self.field = field

    def can(self, from_state: str, to_state: str) -> bool:
        return to_state in self.transitions.get(from_state, frozenset())

    def is_terminal(self, state: str) -> bool:
        return not self.transitions.get(state)

    def check(self, from_state: str, to_state: str) -> None:
        if not self.can(from_state, to_state):
            raise InvalidTransition(self.name, from_state, to_state)

    def apply(self, instance, to_state: str, *, actor=None, extra_fields=(), **audit_data):
        from_state = getattr(instance, self.field)
        self.check(from_state, to_state)
        setattr(instance, self.field, to_state)
        update_fields = [self.field, *extra_fields]
        if hasattr(instance, "updated_at"):
            update_fields.append("updated_at")
        instance.save(update_fields=update_fields)
        audit(
            instance,
            "transition",
            from_state=from_state,
            to_state=to_state,
            actor=actor,
            **audit_data,
        )
        return instance
