"""多币种定金与退款账本。

规则（来自 policies.json）：
- 定金按统一币种 AED 收取，金额 = 各服务本币金额按付款日汇率折算后的 30%；
- 入账冻结汇率快照；跨境取消退款一律按 AED（收款币种）与原汇率退回；
- 每笔分录记录原因（定金、尾款、成员退出释放、跨境取消、承运人取消、延误补偿）。
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from decimal import Decimal

from .money import Money, FX


@dataclass
class LedgerEntry:
    seq: int
    kind: str  # deposit / balance / refund / release / compensation
    currency: str
    amount: Decimal
    reason: str
    ref: str  # service_id 或 lock_id
    fx_snapshot: dict | None = None


@dataclass
class Ledger:
    deposit_currency: str
    fx: FX
    deposit_pct: int
    _seq: itertools.count = field(default_factory=lambda: itertools.count(1))
    entries: list[LedgerEntry] = field(default_factory=list)

    def post(self, kind: str, money: Money, reason: str, ref: str, lock_fx: bool = True) -> LedgerEntry:
        entry = LedgerEntry(
            seq=next(self._seq),
            kind=kind,
            currency=money.currency,
            amount=money.amount,
            reason=reason,
            ref=ref,
            fx_snapshot=self.fx.snapshot() if lock_fx else None,
        )
        self.entries.append(entry)
        return entry

    def deposit_for(self, items: list[tuple[str, Money]], reason: str) -> Money:
        """按各服务金额折算到定金币种，合计后取定金比例，记一笔定金。"""
        total = Money.of(0, self.deposit_currency)
        for ref, price in items:
            total += self.fx.convert(price, self.deposit_currency)
        deposit = total.scaled(self.deposit_pct)
        self.post("deposit", deposit, reason, "TEAM")
        return deposit

    def refund(self, amount_in_deposit_ccy: Money, reason: str, ref: str) -> LedgerEntry:
        if amount_in_deposit_ccy.currency != self.deposit_currency:
            raise ValueError("退款必须按定金币种（收款币种）支付")
        return self.post("refund", amount_in_deposit_ccy, reason, ref, lock_fx=True)

    def totals(self) -> dict[str, Decimal]:
        out: dict[str, Decimal] = {}
        for e in self.entries:
            sign = Decimal(-1) if e.kind in ("refund", "release", "compensation") else Decimal(1)
            out[e.currency] = out.get(e.currency, Decimal(0)) + sign * e.amount
        return out
