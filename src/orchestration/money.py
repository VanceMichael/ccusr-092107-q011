"""多币种金额与汇率换算。

供应商报价混合使用 AED（国际航班）与 CNY（境内服务），
定金统一按 AED 收取，因此每次折算都保留汇率快照。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

CENT = Decimal("0.01")


def _d(value) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


@dataclass(frozen=True)
class Money:
    """带币种的金额，按分四舍五入。"""

    amount: Decimal
    currency: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "amount", _d(self.amount).quantize(CENT, rounding=ROUND_HALF_UP))
        if len(self.currency) != 3:
            raise ValueError("币种必须为 ISO 三字母代码")

    @classmethod
    def of(cls, amount, currency: str) -> "Money":
        return cls(_d(amount), currency)

    def __add__(self, other: "Money") -> "Money":
        if other.currency != self.currency:
            raise ValueError(f"币种不一致：{self.currency} != {other.currency}")
        return Money(self.amount + other.amount, self.currency)

    def __sub__(self, other: "Money") -> "Money":
        if other.currency != self.currency:
            raise ValueError(f"币种不一致：{self.currency} != {other.currency}")
        return Money(self.amount - other.amount, self.currency)

    def scaled(self, pct: Decimal | int | float) -> "Money":
        """按百分比取值（如 30% 定金、60% 退款）。"""
        pct = _d(pct) / Decimal(100)
        return Money(self.amount * pct, self.currency)

    def __mul__(self, qty: int) -> "Money":
        return Money(self.amount * qty, self.currency)

    __rmul__ = __mul__

    def __str__(self) -> str:
        return f"{self.amount} {self.currency}"


class FX:
    """以基准币种计价的汇率表：rates[C] 表示 1 单位 C 折合多少基准币。"""

    def __init__(self, rates: dict[str, float | str | Decimal], base: str = "CNY", as_of: str = ""):
        self.base = base
        self.as_of = as_of
        self.rates = {code: _d(r) for code, r in rates.items()}
        missing = {base} - set(self.rates)
        if missing:
            raise ValueError("汇率表缺少基准币种")

    def convert(self, money: Money, to_currency: str) -> Money:
        if money.currency not in self.rates or to_currency not in self.rates:
            raise ValueError(f"缺少汇率：{money.currency}->{to_currency}")
        if money.currency == to_currency:
            return money
        base_value = money.amount * self.rates[money.currency]
        return Money(base_value / self.rates[to_currency], to_currency)

    def snapshot(self) -> dict:
        """定金/退款入账时固化的汇率快照，保证跨境取消按原汇率退回。"""
        return {"base": self.base, "as_of": self.as_of, "rates": {k: str(v) for k, v in self.rates.items()}}
