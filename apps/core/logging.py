import logging

from .context import order_id_var, request_id_var


class ContextFilter(logging.Filter):
    """Injects request_id / order_id into every record so JSON logs can be correlated."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        record.order_id = order_id_var.get()
        return True
