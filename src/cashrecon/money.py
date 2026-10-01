"""Money helpers. All amounts are stored and computed as integer cents."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

_CENT = Decimal("0.01")


class MoneyError(ValueError):
    pass


def to_cents(value: object) -> int:
    """Convert a yuan amount (str/int/float/Decimal) to integer cents.

    Floats are converted through ``str`` so that upstream REAL values such as
    ``1234.5600000001`` round to the intended two-decimal amount.
    """
    if value is None or isinstance(value, bool):
        raise MoneyError("amount is empty")
    try:
        if isinstance(value, float):
            amount = Decimal(repr(value))
        else:
            amount = Decimal(str(value).strip().replace(",", ""))
    except InvalidOperation:
        raise MoneyError(f"not a number: {value!r}") from None
    if not amount.is_finite():
        raise MoneyError("amount is not finite")
    return int((amount.quantize(_CENT, rounding=ROUND_HALF_UP) * 100).to_integral_value())


def to_cents_or_none(value: object) -> int | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return to_cents(value)


def to_yuan(cents: int) -> Decimal:
    return (Decimal(cents) / 100).quantize(_CENT)


def fmt_yuan(cents: int | None, *, sign: bool = False, empty: str = "—") -> str:
    """Format cents as ``1,234.56``. ``sign=True`` prefixes ``+`` for positives."""
    if cents is None:
        return empty
    text = f"{abs(to_yuan(cents)):,.2f}"
    if cents < 0:
        return "-" + text
    if sign and cents > 0:
        return "+" + text
    return text


def fmt_wan(cents: int | None, empty: str = "—") -> str:
    """Compact Chinese format: amounts >= 10,000 shown in 万."""
    if cents is None:
        return empty
    yuan = to_yuan(cents)
    if abs(yuan) >= 10000:
        return f"{yuan / 10000:,.2f}万"
    return f"{yuan:,.2f}"
