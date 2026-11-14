"""Decimal helpers. Money is never a float. Charges round up, ceilings round down."""
from decimal import Decimal, ROUND_DOWN, ROUND_UP

Q = Decimal("0.000001")


def D(x) -> Decimal:
    return x if isinstance(x, Decimal) else Decimal(str(x))


def down(x) -> Decimal:
    return D(x).quantize(Q, rounding=ROUND_DOWN)


def up(x) -> Decimal:
    return D(x).quantize(Q, rounding=ROUND_UP)


def fmt(x, currency: str = "") -> str:
    s = f"{D(x).quantize(Q).normalize():f}"
    return f"{s} {currency}".strip()


def max_bid_for_cap(cap: Decimal, fee_rate: Decimal) -> Decimal:
    """Largest bid such that bid + fee (rounded up) never exceeds the all-in cap."""
    bid = down(D(cap) / (1 + D(fee_rate)))
    while bid > 0 and bid + up(bid * D(fee_rate)) > cap:
        bid -= Q
    return max(bid, Decimal(0))
