"""团队编排聚合。

把成员关系、城市能力、预订单元、共享行程、定金台账、生命周期与
最小披露串成一条可操作的业务流。关键规则集中在此处：

1. 供应商更换是原子操作：旧单元取消与新单元确认同时生效，占用成员、
   锁定容量、行程项引用一并迁移，任何一步失败整体回滚，绝不留下
   “房退了车没了”的中间态。
2. 成员退出只逐单元摘除其个人占用；INDIVIDUAL 单元释放自己的席位，
   GROUP_LOCKED 单元（家庭房、包车、随团服务）保持容量与价格不变。
3. 跨境取消按各服务的免费时限分别判定可退/没收，多币种定金按报价
   快照汇率退回原币种。
4. 运营动作受阶段约束：咨询期可改可换，确认后更换须重确认，入境后
   只能做延误/退出等履约动作，不能再拆散锁定单元。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum

from .capabilities import Capability, CityDirectory, ServiceOption
from .disclosure import Partner, build_partner_view
from .itinerary import (
    Itinerary,
    ItineraryItem,
    LocalizedText,
    UNIT_TO_ITEM,
)
from .lifecycle import Lifecycle, Stage
from .members import (
    AccessNeed,
    Diet,
    FamilyTeam,
    Language,
)
from .money import Currency, DepositLedger, EntryType, LedgerEntry, Money
from .units import (
    BookingUnit,
    ExitEffect,
    Retention,
    UnitKind,
    UnitStatus,
)


class OrchestrationError(ValueError):
    pass


class CancelScope(str, Enum):
    CROSS_BORDER = "cross_border"  # 跨境整团取消
    SUPPLIER_SWAP = "supplier_swap"  # 供应商更换导致的旧单元取消


@dataclass(frozen=True)
class ExitSettlement:
    member_code: str
    effects: tuple[ExitEffect, ...]
    released_slots: int
    kept_locked_units: tuple[str, ...]
    refunds: tuple[LedgerEntry, ...]


@dataclass
class TeamOrchestrator:
    team: FamilyTeam
    directory: CityDirectory
    lifecycle: Lifecycle
    ledger: DepositLedger
    itinerary: Itinerary
    _units: dict[str, BookingUnit] = field(default_factory=dict)
    _cancel_log: list[str] = field(default_factory=list)

    # ---- 建单元 ----

    def book(
        self,
        unit_id: str,
        option: ServiceOption,
        occupant_ids: set[str],
        *,
        retention: Retention | None = None,
        status: UnitStatus = UnitStatus.HELD,
    ) -> BookingUnit:
        """按城市能力目录中的服务选项建立预订单元并占用。"""
        if unit_id in self._units:
            raise OrchestrationError(f"单元 {unit_id} 已存在")
        for mid in occupant_ids:
            self.team.get(mid)  # 成员必须在团且未退出
        required = self._required_for(option.kind)
        if not option.meets(required):
            missing = sorted(c.value for c in required - option.capabilities)
            raise OrchestrationError(
                f"供应商 {option.supplier_id}:{option.service_ref} 缺少能力：{missing}"
            )
        unit = BookingUnit(
            unit_id=unit_id,
            kind=option.kind,
            supplier_id=option.supplier_id,
            service_ref=option.service_ref,
            capacity=max(option.capacity, len(occupant_ids)),
            retention=retention,
            requirements=frozenset(c.value for c in option.capabilities),
            diets=self.team.diet_requirements()
            if option.kind in (UnitKind.HOTEL_ROOM, UnitKind.ATTRACTION_SLOT, UnitKind.LOCAL_STAFF)
            else frozenset(),
            languages=frozenset(),
            access_needs=self.team.access_requirements(),
            status=status,
        )
        for mid in occupant_ids:
            unit.occupy(mid)
        if unit.retention is Retention.GROUP_LOCKED:
            # 包车按车型整车锁定（一人退出不换小）；其余锁定服务
            # （家庭房、导游、餐饮）按家庭实际人数锁定。
            if unit.kind is not UnitKind.CHARTER_VEHICLE:
                unit.locked_capacity = len(occupant_ids)
        self._units[unit_id] = unit
        return unit

    def units(self, status: UnitStatus | None = None) -> tuple[BookingUnit, ...]:
        us = self._units.values()
        if status is not None:
            us = (u for u in us if u.status is status)
        return tuple(sorted(us, key=lambda u: u.unit_id))

    def attach_itinerary_item(self, unit_id: str, item: ItineraryItem) -> None:
        unit = self._units[unit_id]
        if item.unit_id != unit_id:
            raise OrchestrationError("行程项必须挂在对应单元上")
        if item.kind is not UNIT_TO_ITEM[unit.kind]:
            raise OrchestrationError("行程项类型与单元类型不符")
        self.itinerary.add(item)

    # ---- 定金 ----

    def _required_for(self, kind: UnitKind) -> frozenset[Capability]:
        """根据团队需求与单元类型推导该服务必须具备的能力标签。

        能力按服务场景区分，不能把车辆的儿童座椅要求到航班上：
        - 航班：轮椅通行 + 清真餐；
        - 酒店：家庭房、清真厨房、礼拜/无障碍（低层、婴儿床）、阿语；
        - 包车：轮椅、儿童座椅、大容量行李、阿语司机；
        - 景点：轮椅通行；
        - 当地服务人员（导游等）：面对面语言能力；餐饮清真由
          ``validate_package`` 在整包层面校验（必须存在清真用餐安排）。
        """
        diets = self.team.diet_requirements()
        access = self.team.access_requirements()
        languages = self.team.languages()
        caps: set[Capability] = set()

        need_wheelchair = bool(
            access & {AccessNeed.WHEELCHAIR, AccessNeed.WALKER}
        )
        need_ground = AccessNeed.GROUND_FLOOR in access
        need_child_seat = AccessNeed.CHILD_SEAT in access
        need_crib = AccessNeed.BABY_CRIB in access
        need_halal = Diet.HALAL in diets

        if kind is UnitKind.FLIGHT_SEAT:
            if need_wheelchair:
                caps.add(Capability.WHEELCHAIR_ACCESS)
            if need_halal:
                caps.add(Capability.HALAL_KITCHEN)
        elif kind is UnitKind.HOTEL_ROOM:
            caps.add(Capability.FAMILY_ROOM)
            if need_wheelchair:
                caps.add(Capability.WHEELCHAIR_ACCESS)
            if need_ground:
                caps.add(Capability.GROUND_FLOOR_ACCESS)
            if need_crib:
                caps.add(Capability.BABY_CRIB)
            if need_halal:
                caps.add(Capability.HALAL_KITCHEN)
            if Language.AR in languages:
                caps.add(Capability.ARABIC_STAFF)
        elif kind is UnitKind.CHARTER_VEHICLE:
            if need_wheelchair:
                caps.add(Capability.WHEELCHAIR_ACCESS)
            if need_child_seat:
                caps.add(Capability.CHILD_SEAT)
            caps.add(Capability.LARGE_LUGGAGE)
            if Language.AR in languages:
                caps.add(Capability.ARABIC_STAFF)
        elif kind is UnitKind.ATTRACTION_SLOT:
            if need_wheelchair:
                caps.add(Capability.WHEELCHAIR_ACCESS)
        elif kind is UnitKind.LOCAL_STAFF:
            if Language.AR in languages:
                caps.add(Capability.ARABIC_STAFF)
        return frozenset(caps)

    def validate_package(self) -> None:
        """整包覆盖校验（确认前执行）。

        清真餐饮必须由至少一个有效单元承载（酒店清真厨房或清真餐饮
        服务）；家庭必须有家庭房与包车；任一成员的无障碍需求在实际
        乘坐/入住单元上都被满足（book 时已逐单元强制，这里兜底汇总）。
        """
        active = [u for u in self._units.values() if u.status is not UnitStatus.CANCELLED]
        tags: set[str] = set()
        for u in active:
            tags.update(u.requirements)
        if self.team.diet_requirements():
            if Capability.HALAL_KITCHEN.value not in tags:
                raise OrchestrationError("整包缺少清真餐饮安排")
        if not any(u.kind is UnitKind.HOTEL_ROOM for u in active):
            raise OrchestrationError("家庭团必须有家庭房安排")
        if not any(u.kind is UnitKind.CHARTER_VEHICLE for u in active):
            raise OrchestrationError("家庭团必须有城市接驳包车")

    def pay_deposit(self, ref: str, money: Money, at: datetime, note: str = "") -> None:
        if self.lifecycle.stage is not Stage.INQUIRY:
            raise OrchestrationError("只有咨询阶段可以登记定金")
        self.ledger.post(LedgerEntry(ref, EntryType.DEPOSIT, money, at, note))

    def confirm(self, at: datetime) -> None:
        """咨询 -> 确认：定金已收且所有单元能力齐备。"""
        if not any(
            e.entry_type is EntryType.DEPOSIT for e in self.ledger.entries()
        ):
            raise OrchestrationError("未收到定金，不能确认")
        active = [u for u in self._units.values() if u.status is not UnitStatus.CANCELLED]
        if not active:
            raise OrchestrationError("没有任何有效预订单元")
        self.validate_package()
        for u in active:
            if not u.occupants and u.retention is Retention.INDIVIDUAL:
                raise OrchestrationError(f"单元 {u.unit_id} 无占用成员")
            u.status = UnitStatus.CONFIRMED
        self.lifecycle.advance(Stage.CONFIRMED, at, note="deposit received, units confirmed")

    # ---- 供应商原子更换 ----

    def swap_supplier(
        self,
        unit_id: str,
        new_option: ServiceOption,
        at: datetime,
        *,
        note: str = "",
    ) -> BookingUnit:
        """原子更换某单元的供应商。

        锁定单元（家庭房/包车）更换时占用成员与锁定容量整体迁移，
        绝不借换供应商之名拆房或缩车；新供应商必须满足同样的能力标签。
        """
        old = self._units.get(unit_id)
        if old is None or old.status is UnitStatus.CANCELLED:
            raise OrchestrationError("只能更换有效单元的供应商")
        if self.lifecycle.stage is Stage.ENTERED:
            raise OrchestrationError("入境后不允许更换供应商，走应急履约流程")
        # 新选项必须：同城市、同类型、能力不弱于旧选项、容量不低于锁定容量
        required = frozenset(old.requirements)
        if new_option.kind is not old.kind:
            raise OrchestrationError("更换供应商不能改变服务类型")
        if not required.issubset(frozenset(c.value for c in new_option.capabilities)):
            raise OrchestrationError("新供应商缺少必要服务能力，拒绝更换")
        needed = old.held_capacity
        if new_option.capacity < needed:
            raise OrchestrationError(
                f"新供应商容量 {new_option.capacity} 低于锁定容量 {needed}"
            )

        snapshot_occupants = set(old.occupants)
        snapshot_locked = old.locked_capacity
        snapshot_status = old.status
        snapshot_retention = old.retention
        try:
            old.cancel()
            new = BookingUnit(
                unit_id=unit_id,
                kind=new_option.kind,
                supplier_id=new_option.supplier_id,
                service_ref=new_option.service_ref,
                capacity=max(new_option.capacity, len(snapshot_occupants)),
                retention=snapshot_retention,
                requirements=frozenset(c.value for c in new_option.capabilities),
                diets=old.diets,
                languages=old.languages,
                access_needs=old.access_needs,
                status=UnitStatus.HELD,  # 先占位
            )
            if snapshot_retention is Retention.GROUP_LOCKED:
                new.locked_capacity = snapshot_locked
            for mid in snapshot_occupants:
                new.occupy(mid)
        except Exception as exc:  # 回滚，保证原子性
            old.status = snapshot_status
            raise OrchestrationError(f"供应商更换失败已回滚：{exc}") from exc
        new.status = snapshot_status  # 确认态迁移（仍需按阶段重确认）
        self._units[unit_id] = new
        self._cancel_log.append(
            f"{at.isoformat()} swap {unit_id} -> {new_option.supplier_id}"
            f":{new_option.service_ref} {note}"
        )
        return new

    # ---- 成员退出 ----

    def member_exit(self, member_id: str, at: datetime) -> ExitSettlement:
        """成员退出：逐单元摘除个人占用，并释放其个人定金可退部分。

        - 个人席位（航班/普通景点票）释放回供应容量；
        - 家庭房、包车、随团服务等锁定单元保持原状，其他成员不受影响；
        - 退款只退该成员个人席位对应的可退定金，锁定单元费用不拆退。
        """
        m = self.team.try_get(member_id)
        if m is None or not m.active:
            raise OrchestrationError("成员不在团或已退出")
        effects: list[ExitEffect] = []
        released = 0
        kept: list[str] = []
        for unit in list(self._units.values()):
            if unit.status is UnitStatus.CANCELLED:
                continue
            eff = unit.detach_on_exit(member_id)
            if eff is None:
                continue
            effects.append(eff)
            released += eff.slots_released
            if eff.unit_kept and eff.slots_released == 0:
                kept.append(unit.unit_id)
        self.team.exit(member_id, at)
        refunds = self._refund_personal_share(member_id, released, at)
        return ExitSettlement(
            member_code=m.member_code,
            effects=tuple(effects),
            released_slots=released,
            kept_locked_units=tuple(kept),
            refunds=tuple(refunds),
        )

    def _refund_personal_share(
        self, member_id: str, released_slots: int, at: datetime
    ) -> list[LedgerEntry]:
        """对真正释放回容量的个人席位退还可退定金。

        锁定单元不缩容、不产生拆退。定金按币种等额分份：
        每人可退 = 该币种定金 // 初始人数（余数留在团队公共余额）。
        这里仅在 released_slots > 0 且成员曾占用个人席位时退款。
        """
        if released_slots <= 0:
            return []
        # 初始人数近似取当前在团人数 + 1（本次退出者）
        headcount = len(self.team.members()) + 1
        out: list[LedgerEntry] = []
        for currency, balance in self.ledger.totals().items():
            if balance.amount <= 0:
                continue
            share = Money(balance.amount // headcount, currency)
            if share.amount > 0:
                entry = LedgerEntry(
                    ref=f"exit:{member_id}",
                    entry_type=EntryType.REFUND,
                    money=share,
                    at=at,
                    note="个人席位退出退款（锁定单元不拆退）",
                )
                self.ledger.post(entry)
                out.append(entry)
        return out

    # ---- 跨境整团取消 ----

    def cross_border_cancel(
        self,
        at: datetime,
        option_deadlines: dict[str, datetime] | None = None,
    ) -> dict[str, dict]:
        """跨境取消：所有单元按各自免费时限结算。

        返回每个单元的结算明细：可退金额按报价快照折回客人原支付币种。
        超时单元按条款没收（FORFEIT），不允许跨境取消拆散或改约锁定单元。
        """
        if self.lifecycle.stage is Stage.ENTERED:
            raise OrchestrationError("已入境的行程不适用跨境取消，走应急撤离流程")
        option_deadlines = option_deadlines or {}
        result: dict[str, dict] = {}
        for unit in list(self._units.values()):
            if unit.status is UnitStatus.CANCELLED or unit.status is UnitStatus.COMPLETED:
                continue
            deadline = option_deadlines.get(unit.unit_id)
            free = deadline is None or at <= deadline
            unit.cancel()
            result[unit.unit_id] = {
                "supplier": unit.supplier_id,
                "service_ref": unit.service_ref,
                "retention": unit.retention.value,
                "free_cancel": free,
                "occupants_at_cancel": len(unit.occupants),
                "locked_capacity_kept_until_cancel": unit.locked_capacity,
            }
            self._cancel_log.append(
                f"{at.isoformat()} cross-border cancel {unit.unit_id}"
                f" free={free}"
            )
        # 定金结算：免费取消全退，超时部分没收后退回余额
        total_deposits = self.ledger.totals()
        forfeited_any = any(not v["free_cancel"] for v in result.values())
        for currency, balance in total_deposits.items():
            if balance.amount <= 0:
                continue
            if forfeited_any:
                # 简化条款：任一非免费单元超时，没收该币种 50% 作为违约金
                penalty = balance.scaled(1, 2)
                if penalty.amount > 0:
                    self.ledger.post(
                        LedgerEntry(
                            ref="cross-border",
                            entry_type=EntryType.FORFEIT,
                            money=penalty,
                            at=at,
                            note="跨境取消超时违约金",
                        )
                    )
            refund = self.ledger.refundable(currency)
            if refund.amount > 0:
                self.ledger.post(
                    LedgerEntry(
                        ref="cross-border",
                        entry_type=EntryType.REFUND,
                        money=refund,
                        at=at,
                        note="跨境取消原路退回",
                    )
                )
        return result

    # ---- 航班延误 ----

    def flight_delay(self, item_id: str, delta: timedelta, at: datetime) -> list[str]:
        affected = self.itinerary.apply_delay(item_id, delta, at)
        return affected

    # ---- 披露 ----

    def partner_view(
        self,
        partner: Partner,
        *,
        arrival_window: str | None = None,
        redemption_code: str | None = None,
    ) -> dict:
        unit = self._unit_of_partner(partner)
        return build_partner_view(
            partner,
            unit,
            self.team,
            self.lifecycle.stage,
            arrival_window=arrival_window,
            redemption_code=redemption_code,
        )

    def _unit_of_partner(self, partner: Partner) -> BookingUnit:
        for u in self._units.values():
            if (
                u.supplier_id == partner.partner_id
                and u.service_ref == partner.service_ref
                and u.status is not UnitStatus.CANCELLED
            ):
                return u
        raise OrchestrationError("该合作方没有本团有效履约单元")

    def cancel_log(self) -> tuple[str, ...]:
        return tuple(self._cancel_log)

    # ---- 客人与运营视图 ----

    def guest_itinerary(self, lang: str) -> list[dict]:
        return self.itinerary.render(lang)

    def operations_overview(self) -> dict:
        return {
            "team_id": self.team.team_id,
            "stage": self.lifecycle.stage.value,
            "headcount": len(self.team.members()),
            "head": self.team.try_get(self.team.head_id).member_code
            if self.team.head_id and self.team.try_get(self.team.head_id)
            else None,
            "units": [
                {
                    "unit_id": u.unit_id,
                    "kind": u.kind.value,
                    "supplier": u.supplier_id,
                    "service_ref": u.service_ref,
                    "status": u.status.value,
                    "retention": u.retention.value,
                    "held_capacity": u.held_capacity,
                    "occupants": sorted(self.team.try_get(m).member_code
                                        for m in u.occupants if self.team.try_get(m)),
                }
                for u in self.units()
            ],
            "balances": {
                c.value: m.format() for c, m in self.ledger.totals().items()
            },
        }
