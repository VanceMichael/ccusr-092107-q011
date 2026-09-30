"""多币种金额、汇率快照与定金台账。

金额一律使用十进制整数的最小货币单位（如 AED 的 fils、CNY 的分），
避免浮点误差。汇率以报价时刻的快照固定，之后航班延误、跨境取消等
事件均按快照汇率结算，杜绝事后汇率争议。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class Currency(str, Enum):
    AED = "AED"
    CNY = "CNY"
    USD = "USD"


# 各币种 1 个主单位对应的最小单位倍数
_MINOR_UNITS: dict[str, int] = {
    Currency.AED: 100,  # 1 dirham = 100 fils
    Currency.CNY: 100,  # 1 yuan = 100 fen
    Currency.USD: 100,
}


@dataclass(frozen=True)
class Money:
    """不可变金额，amount 为最小单位整数。"""

    amount: int
    currency: Currency

    def __post_init__(self) -> None:
        if not isinstance(self.amount, int):
            raise TypeError("金额必须为最小单位整数")
        if not isinstance(self.currency, Currency):
            raise TypeError("币种不受支持")

    @classmethod
    def of(cls, major: float | int, currency: Currency | str) -> "Money":
        """以主单位（如 1500.00 AED）构造金额。"""
        currency = Currency(currency)
        minor = _MINOR_UNITS[currency]
        return cls(amount=int(round(float(major) * minor)), currency=currency)

    @property
    def major(self) -> float:
        return self.amount / _MINOR_UNITS[self.currency]

    def _check_same(self, other: "Money") -> None:
        if self.currency != other.currency:
            raise ArithmeticError(f"币种不一致：{self.currency} 与 {other.currency}")

    def __add__(self, other: "Money") -> "Money":
        self._check_same(other)
        return Money(self.amount + other.amount, self.currency)

    def __sub__(self, other: "Money") -> "Money":
        self._check_same(other)
        return Money(self.amount - other.amount, self.currency)

    def scaled(self, numerator: int, denominator: int) -> "Money":
        """按分数比例缩放（用于定金比例、退款比例），向下取整。"""
        return Money(self.amount * numerator // denominator, self.currency)

    def format(self) -> str:
        return f"{self.major:,.2f} {self.currency.value}"

    def to_dict(self) -> dict:
        return {"amount_minor": self.amount, "currency": self.currency.value}


@dataclass(frozen=True)
class FxQuote:
    """某一时刻的汇率快照：1 单位 base 兑换 rate 单位 quote。"""

    base: Currency
    quote: Currency
    rate: float
    quoted_at: datetime

    def convert(self, money: Money) -> Money:
        if money.currency != self.base:
            raise ArithmeticError(f"汇率快照只接受 {self.base.value}")
        target_minor = round(money.amount * self.rate)
        return Money(target_minor, self.quote)


class LedgerError(ValueError):
    pass


class EntryType(str, Enum):
    DEPOSIT = "deposit"        # 客人支付定金
    REFUND = "refund"          # 取消/退出退款
    CHARGE = "charge"          # 尾款或消费扣款
    FORFEIT = "forfeit"        # 依据条款没收
    FX_ADJUST = "fx_adjust"    # 汇率快照重估


@dataclass(frozen=True)
class LedgerEntry:
    ref: str                 # 业务引用，如预订单元号
    entry_type: EntryType
    money: Money
    at: datetime
    note: str = ""


@dataclass
class DepositLedger:
    """单团队多币种定金台账。

    余额按币种分别累计；退款只能从对应币种余额中释放，
    禁止在未取价的情况下跨币种轧差。
    """

    team_id: str
    fx: FxQuote | None = None
    _entries: list[LedgerEntry] = field(default_factory=list)

    def post(self, entry: LedgerEntry) -> None:
        self._entries.append(entry)

    def entries(self) -> tuple[LedgerEntry, ...]:
        return tuple(self._entries)

    def balance(self, currency: Currency) -> Money:
        total = 0
        for e in self._entries:
            if e.money.currency != currency:
                continue
            if e.entry_type in (EntryType.DEPOSIT, EntryType.FX_ADJUST):
                total += e.money.amount
            else:  # REFUND / CHARGE / FORFEIT 均为流出
                total -= e.money.amount
        if total < 0:
            raise LedgerError(f"{currency.value} 余额为负，退款超过定金")
        return Money(total, currency)

    def refundable(self, currency: Currency) -> Money:
        """可退余额与当前余额相同（没收部分已以 FORFEIT 流出）。"""
        return self.balance(currency)

    def totals(self) -> dict[Currency, Money]:
        used = {e.money.currency for e in self._entries}
        return {c: self.balance(c) for c in sorted(used, key=lambda x: x.value)}

