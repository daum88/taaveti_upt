"""
Fixed-point money & quantity handling — the SQLite equivalent of
PostgreSQL ``NUMERIC(38,8)`` + Java ``BigDecimal``.

Storage model (Option A)
------------------------
All monetary amounts and share quantities are stored in the database as
**scaled 64-bit integers** at a fixed scale of 8 decimal places (``* 1e8``).
This makes SQL ``SUM``/``ORDER BY`` exact and fast, unlike storing floats
(``REAL``) or decimal strings (``TEXT``).

Application model
-----------------
In Python, values are manipulated as :class:`decimal.Decimal` (the direct
analogue of ``java.math.BigDecimal``) with a precision of 38 significant
digits. Rounding is applied **once**, at the DB boundary, using
``ROUND_HALF_UP``.

Range note: a signed 64-bit integer at scale 8 caps at ~9.2e10
(~92 billion). That is ample for a portfolio simulator. If true 38-digit
range is ever required, switch storage to TEXT + a sqlite3 adapter.
"""

from decimal import Decimal, getcontext, ROUND_HALF_UP

getcontext().prec = 38

SCALE = 8
_SCALE_FACTOR = Decimal(10) ** SCALE
_QUANTUM = Decimal(1).scaleb(-SCALE)  # Decimal('0.00000001')


def q(value: Decimal) -> Decimal:
    """Quantize a Decimal to the fixed scale (8 dp), rounding half-up."""
    return value.quantize(_QUANTUM, rounding=ROUND_HALF_UP)


def to_e8(value) -> int:
    """Convert a Decimal/int/str money-or-quantity value to scaled int (*1e8)."""
    d = value if isinstance(value, Decimal) else Decimal(str(value))
    return int((d * _SCALE_FACTOR).to_integral_value(rounding=ROUND_HALF_UP))


def from_e8(value: int) -> Decimal:
    """Convert a scaled int (*1e8) back to a Decimal at 8 dp."""
    return (Decimal(value) / _SCALE_FACTOR).quantize(_QUANTUM)


def dec(value) -> Decimal:
    """Coerce an arbitrary numeric input (e.g. a float price) to Decimal safely."""
    return value if isinstance(value, Decimal) else Decimal(str(value))
