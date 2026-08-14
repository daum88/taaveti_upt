"""Shared JSON serialization for web adapters."""

from decimal import Decimal


def json_default(value: object) -> object:
    """Serialize Decimal money and quantity values as JSON numbers."""
    if isinstance(value, Decimal):
        return float(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")
