"""海湾入境团队编排引擎。

一条主线：咨询(inquiry) → 确认(confirmed) → 入境(inbound) → 消费(in_destination) → 复访(revisit)。

核心不变量：
1. 家庭房按房单整体锁定，包车按团队整体锁定——任何成员退出只释放个人座位/餐位/门票，
   绝不拆分锁；
2. 供应商更换是原子操作：新供应商必须同时满足该服务上的全部团队约束，
   任一约束不满足则整单保持原供应商；
3. 航班为锚点事件：延误按绝对时差传播到接机、接驳与首晚餐饮；
4. 定金/退款只走客人付款币种 AED，折算冻结汇率；
5. 对合作方只下发最小披露单据，证件以令牌引用，亲属关系不出系统。
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from .catalog import ServiceCatalog, ServiceDef
from .disclosure import build_disclosure
from .itinerary import Event, Itinerary
from .ledger import Ledger
from .members import MemberRegistry
from .money import Money

STAGES = ["inquiry", "confirmed", "inbound", "in_destination", "revisit"]
STAGE_NAMES = {
    "inquiry": {"zh": "咨询", "ar": "استفسار", "en": "Inquiry"},
    "confirmed": {"zh": "确认", "ar": "تأكيد", "en": "Confirmed"},
    "inbound": {"zh": "入境", "ar": "دخول", "en": "Inbound"},
    "in_destination": {"zh": "消费", "ar": "الاستهلاك", "en": "In-destination"},
    "revisit": {"zh": "复访", "ar": "زيارة مجددة", "en": "Revisit"},
}
CST = "Asia/Shanghai"
DXT = "Asia/Dubai"


class ArrangementError(Exception):
    """约束不满足或阶段操作非法。"""

    def __init__(self, message: str, violations: list[str] | None = None):
        super().__init__(message)
        self.violations = violations or []


@dataclass
class Unit:
    """一个被锁定的供应单元。"""

    lock_id: str
    category: str
    service_id: str
    supplier_id: str
    scope: str  # TEAM / ROOM:<unit_id> / PERSON
    indivisible: bool
    participants: list[str]
    start: datetime
    end: datetime
    unit_price: Money
    qty: int  # 房间晚数 / 包车天数 / 人数 / 次数
    event_id: str
    room_type: str | None = None
    room_unit: str | None = None
    status: str = "held"  # held / confirmed / cancelled / replaced
    released_participants: list[str] = field(default_factory=list)
    extra: dict = field(default_factory=dict)

    def line_total(self, active_ids: set[str]) -> Money:
        if self.scope == "PERSON":
            pax = len([m for m in self.participants if m in active_ids])
            return self.unit_price * (pax * self.qty)
        return self.unit_price * self.qty


class TeamOrchestration:
    def __init__(self, registry: MemberRegistry, catalog: ServiceCatalog, fx, policies: dict):
        self.registry = registry
        self.catalog = catalog
        self.fx = fx
        self.policies = policies
        self.stage = "inquiry"
        self.units: dict[str, Unit] = {}
        self.itinerary = Itinerary()
        self.ledger = Ledger(
            deposit_currency=policies["deposit_policy"]["deposit_currency"],
            fx=fx,
            deposit_pct=policies["deposit_policy"]["deposit_pct"],
        )
        self.audit: list[dict] = []
        self._seq = 0
        self._flights: dict[str, str] = {}  # leg -> lock_id
        self.disclosures: dict[str, dict] = {}
        self.registry.disclosure_policy = policies["minimum_disclosure"]["by_category"]

    # ------------------------------------------------------------------ 工具
    def _log(self, action: str, **detail) -> None:
        self.audit.append({"ts": datetime.now(timezone.utc).isoformat(), "action": action, "detail": detail})

    def _next(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}-{self._seq:03d}"

    def _active_ids(self) -> set[str]:
        return {m.member_id for m in self.registry.active_members()}

    def _require_features(self, svc: ServiceDef, required: list[str], where: str) -> None:
        missing = self.catalog.missing_features(svc.service_id, required)
        if missing:
            raise ArrangementError(
                f"{svc.service_id} 不满足 {where} 的团队约束", missing
            )

    def _event_for(self, unit: Unit, svc: ServiceDef, start, end, tz: str,
                   ends_tz: str | None = None, detail: dict | None = None) -> Event:
        return Event(
            event_id=unit.event_id,
            category=unit.category,
            service_id=svc.service_id,
            supplier_id=svc.supplier_id,
            title=svc.name,
            start=start,
            end=end,
            tz=tz,
            ends_tz=ends_tz,
            participants=list(unit.participants),
            lock_id=unit.lock_id,
            detail=detail or {},
        )

    # ---------------------------------------------------------- 团队约束推导
    def _flight_requirements(self) -> list[str]:
        req = ["halal_meal"]
        if self.registry.wheelchair_count() > 0:
            req.append("wheelchair_assist")
        if "ar" in self.registry.preferences.get("guide_languages", []):
            req.append("arabic_crew")
        return req

    def _vehicle_requirements(self) -> list[str]:
        return list(self.registry.vehicle_request["requirements"])

    def _meal_requirements(self) -> list[str]:
        req = ["halal_certified"]
        if self.registry.wheelchair_count() > 0:
            req.append("step_free_entry")
        return req

    def _visit_requirements(self) -> list[str]:
        return ["step_free_route"] if self.registry.wheelchair_count() > 0 else []

    def _guide_requirements(self) -> list[str]:
        req = ["language:ar", "language:en"]
        if self.registry.preferences.get("guide_gender_preference") == "female":
            req.append("female_guide")
        return req

    def _meet_requirements(self) -> list[str]:
        req = ["arabic_speaker"]
        if self.registry.wheelchair_count() > 0:
            req.append("wheelchair_assist")
        return req

    # ------------------------------------------------------------- 咨询：锁单
    def add_flight(self, service_id: str, leg: str) -> str:
        if self.stage not in ("inquiry", "confirmed", "inbound"):
            raise ArrangementError(f"阶段 {self.stage} 不可再调整航班")
        svc = self.catalog.get(service_id)
        if svc.category != "flight":
            raise ArrangementError(f"{service_id} 不是航班")
        self._require_features(svc, self._flight_requirements(), "航班")
        active = self._active_ids()
        if (svc.seats_available or 0) < len(active):
            raise ArrangementError(f"{service_id} 座位不足：需 {len(active)}，余 {svc.seats_available}")
        if leg in self._flights:
            raise ArrangementError(f"{leg} 航段已锁定，请使用供应商更换")
        lock_id = self._next("LOCK-FLT")
        event_id = self._next("EVT-FLT")
        unit = Unit(
            lock_id=lock_id, category="flight", service_id=service_id, supplier_id=svc.supplier_id,
            scope="PERSON", indivisible=False, participants=sorted(active),
            start=svc.departs_at, end=svc.arrives_at, unit_price=svc.price, qty=1, event_id=event_id,
            extra={"leg": leg},
        )
        tz = DXT if leg == "outbound" else CST
        ends_tz = CST if leg == "outbound" else DXT
        self.units[lock_id] = unit
        self.itinerary.add(self._event_for(unit, svc, svc.departs_at, svc.arrives_at, tz, ends_tz,
                                           {"leg": leg, "flight_no": service_id}))
        self._flights[leg] = lock_id
        self._log("hold_flight", lock_id=lock_id, service_id=service_id, leg=leg, pax=len(active))
        return lock_id

    def add_hotel(self, service_id: str, checkin: datetime, checkout: datetime, nights: int) -> list[str]:
        if self.stage != "inquiry":
            raise ArrangementError("只能在咨询阶段整体锁定酒店")
        svc = self.catalog.get(service_id)
        if svc.category != "hotel":
            raise ArrangementError(f"{service_id} 不是酒店")
        lock_ids = []
        for req in self.registry.rooming_request:
            room = svc.room(req["room_type"])
            required = list(req.get("requirements", []))
            union = set(svc.features) | set(room.features)
            missing = [f for f in required if f not in union]
            if missing:
                raise ArrangementError(
                    f"{service_id} 房型 {req['room_type']} 不满足房单 {req['unit_id']}", missing
                )
            occupants = [m for m in req["occupants"] if m in self._active_ids()]
            if room.occupancy < len(req["occupants"]):
                raise ArrangementError(
                    f"{service_id} {req['room_type']} 容量不足：住 {len(req['occupants'])}，限 {room.occupancy}"
                )
            if room.inventory < 1:
                raise ArrangementError(f"{service_id} {req['room_type']} 无库存")
            lock_id = self._next("LOCK-HTL")
            event_id = self._next("EVT-HTL")
            unit = Unit(
                lock_id=lock_id, category="hotel", service_id=service_id, supplier_id=svc.supplier_id,
                scope=f"ROOM:{req['unit_id']}", indivisible=True, participants=occupants,
                start=checkin, end=checkout, unit_price=room.price, qty=nights, event_id=event_id,
                room_type=req["room_type"], room_unit=req["unit_id"],
            )
            ev = self._event_for(unit, svc, checkin, checkout, CST,
                                 detail={"room_type": req["room_type"], "room_unit": req["unit_id"], "nights": nights})
            self.units[lock_id] = unit
            self.itinerary.add(ev)
            lock_ids.append(lock_id)
        self._log("hold_hotel", service_id=service_id, rooms=len(lock_ids), nights=nights)
        return lock_ids

    def add_charter(self, service_id: str, start: datetime, end: datetime, days: int,
                    anchor_flight: bool = False) -> str:
        if self.stage != "inquiry":
            raise ArrangementError("只能在咨询阶段锁定包车")
        svc = self.catalog.get(service_id)
        if svc.category != "vehicle":
            raise ArrangementError(f"{service_id} 不是车辆服务")
        self._require_features(svc, self._vehicle_requirements(), "包车")
        active = self._active_ids()
        if (svc.capacity or 0) < len(active):
            raise ArrangementError(f"{service_id} 座位不足：需 {len(active)}，{svc.capacity} 座")
        lock_id = self._next("LOCK-VAN")
        event_id = self._next("EVT-VAN")
        unit = Unit(
            lock_id=lock_id, category="vehicle", service_id=service_id, supplier_id=svc.supplier_id,
            scope="TEAM", indivisible=True, participants=sorted(active),
            start=start, end=end, unit_price=svc.price, qty=days, event_id=event_id,
        )
        self.units[lock_id] = unit
        ev = self._event_for(unit, svc, start, end, CST, detail={"days": days, "charter": True})
        if anchor_flight:
            outbound = self.units[self._flights["outbound"]]
            ev.depends_on = outbound.event_id
            ev.offset_from_parent = start - outbound.end
        self.itinerary.add(ev)
        self._log("hold_charter", lock_id=lock_id, service_id=service_id, days=days)
        return lock_id

    def _add_per_person(self, category: str, service_id: str, start: datetime, end: datetime,
                        required: list[str], anchor_flight: bool = False, detail: dict | None = None) -> str:
        svc = self.catalog.get(service_id)
        if svc.category != category:
            raise ArrangementError(f"{service_id} 不是 {category}")
        self._require_features(svc, required, category)
        active = sorted(self._active_ids())
        if svc.seats_available is not None and svc.seats_available < len(active):
            raise ArrangementError(f"{service_id} 容量不足")
        prefix = {"restaurant": "RST", "attraction": "ATT", "local_service": "SVC"}[category]
        lock_id = self._next(f"LOCK-{prefix}")
        event_id = self._next(f"EVT-{prefix}")
        unit = Unit(
            lock_id=lock_id, category=category, service_id=service_id, supplier_id=svc.supplier_id,
            scope="PERSON", indivisible=False, participants=active,
            start=start, end=end, unit_price=svc.price, qty=1, event_id=event_id,
        )
        ev = self._event_for(unit, svc, start, end, CST, detail=detail or {})
        if anchor_flight:
            outbound = self.units[self._flights["outbound"]]
            ev.depends_on = outbound.event_id
            ev.offset_from_parent = start - outbound.end
        self.units[lock_id] = unit
        self.itinerary.add(ev)
        self._log(f"hold_{category}", lock_id=lock_id, service_id=service_id, pax=len(active))
        return lock_id

    def add_meal(self, service_id: str, start: datetime, end: datetime, anchor_flight: bool = False) -> str:
        return self._add_per_person("restaurant", service_id, start, end, self._meal_requirements(),
                                    anchor_flight=anchor_flight, detail={"meal": True})

    def add_attraction(self, service_id: str, start: datetime, end: datetime) -> str:
        return self._add_per_person("attraction", service_id, start, end, self._visit_requirements())

    def add_local_service(self, service_id: str, start: datetime, end: datetime,
                          anchor_flight: bool = False, per_team: bool = False) -> str:
        svc = self.catalog.get(service_id)
        required = self._meet_requirements() if svc.category == "local_service" else []
        if per_team:
            self._require_features(svc, required, "local_service")
            lock_id = self._next("LOCK-SVC")
            event_id = self._next("EVT-SVC")
            unit = Unit(
                lock_id=lock_id, category="local_service", service_id=service_id, supplier_id=svc.supplier_id,
                scope="TEAM", indivisible=True, participants=sorted(self._active_ids()),
                start=start, end=end, unit_price=svc.price, qty=1, event_id=event_id,
            )
            self.units[lock_id] = unit
            ev = self._event_for(unit, svc, start, end, CST)
            if anchor_flight:
                outbound = self.units[self._flights["outbound"]]
                ev.depends_on = outbound.event_id
                ev.offset_from_parent = start - outbound.end
            self.itinerary.add(ev)
            self._log("hold_local_service", lock_id=lock_id, service_id=service_id, scope="TEAM")
            return lock_id
        return self._add_per_person("local_service", service_id, start, end, required,
                                    anchor_flight=anchor_flight)

    def add_guide(self, service_id: str, start: datetime, end: datetime, days: int) -> str:
        if self.stage != "inquiry":
            raise ArrangementError("只能在咨询阶段锁定向导")
        svc = self.catalog.get(service_id)
        if svc.category != "local_staff":
            raise ArrangementError(f"{service_id} 不是当地服务人员")
        self._require_features(svc, self._guide_requirements(), "向导")
        lock_id = self._next("LOCK-GD")
        event_id = self._next("EVT-GD")
        unit = Unit(
            lock_id=lock_id, category="local_staff", service_id=service_id, supplier_id=svc.supplier_id,
            scope="TEAM", indivisible=True, participants=sorted(self._active_ids()),
            start=start, end=end, unit_price=svc.price, qty=days, event_id=event_id,
        )
        self.units[lock_id] = unit
        self.itinerary.add(self._event_for(unit, svc, start, end, CST, detail={"days": days}))
        self._log("hold_guide", lock_id=lock_id, service_id=service_id, days=days)
        return lock_id

    # ------------------------------------------------------------- 总额/校验
    def active_units(self) -> list[Unit]:
        return [u for u in self.units.values() if u.status != "cancelled"]

    def total_in(self, currency: str) -> Money:
        total = Money.of(0, currency)
        active = self._active_ids()
        for u in self.active_units():
            total += self.fx.convert(u.line_total(active), currency)
        return total

    def _completeness_violations(self) -> list[str]:
        problems = []
        if "outbound" not in self._flights or "return" not in self._flights:
            problems.append("缺少去程或返程航班")
        if not any(u.category == "hotel" and u.status != "cancelled" for u in self.units.values()):
            problems.append("缺少酒店锁定")
        else:
            locked_rooms = {u.room_unit for u in self.active_units() if u.category == "hotel"}
            for req in self.registry.rooming_request:
                if req["unit_id"] not in locked_rooms:
                    problems.append(f"家庭房 {req['unit_id']} 未锁定")
        if not any(u.category == "vehicle" for u in self.active_units()):
            problems.append("缺少团队包车")
        if not any(u.category == "restaurant" for u in self.active_units()):
            problems.append("缺少清真餐饮安排")
        if not any(u.category == "local_staff" for u in self.active_units()):
            problems.append("缺少阿拉伯语/英语向导")
        return problems

    # ----------------------------------------------------------------- 确认
    def confirm(self) -> dict:
        if self.stage != "inquiry":
            raise ArrangementError(f"阶段 {self.stage} 不能重复确认")
        problems = self._completeness_violations()
        if problems:
            raise ArrangementError("编排不完整，无法确认", problems)
        active = self._active_ids()
        items = [(u.service_id, u.line_total(active)) for u in self.units.values()]
        deposit = self.ledger.deposit_for(items, reason="团队确认定金（多币种折算 AED）")
        for u in self.units.values():
            u.status = "confirmed"
        # 为每个合作方生成最小披露单据
        self.disclosures.clear()
        for u in self.units.values():
            svc = self.catalog.get(u.service_id)
            self.disclosures[u.lock_id] = build_disclosure(
                self.registry, svc, unit_id=u.room_unit, booking_ref=u.lock_id
            )
        self.stage = "confirmed"
        self._log("confirm", deposit=str(deposit), units=len(self.units))
        return {"stage": self.stage, "deposit": str(deposit), "locks": len(self.units)}

    # --------------------------------------------------------- 供应商原子更换
    def replace_supplier(self, old_service_id: str, new_service_id: str) -> dict:
        """把引用旧服务的全部锁整体换到新服务；任何一个锁约束失败则全部不动。"""
        affected = [u for u in self.active_units() if u.service_id == old_service_id]
        if not affected:
            raise ArrangementError(f"没有锁定 {old_service_id}")
        new_svc = self.catalog.get(new_service_id)
        snapshot = copy.deepcopy({"units": self.units, "events": {e.event_id: e for e in self.itinerary.events.values()}})
        try:
            for u in affected:
                if new_svc.category != u.category:
                    raise ArrangementError("新旧服务类别不一致")
                active = self._active_ids()
                if u.category == "flight":
                    self._require_features(new_svc, self._flight_requirements(), "航班")
                    if (new_svc.seats_available or 0) < len([m for m in u.participants if m in active]):
                        raise ArrangementError("替代航班座位不足")
                    u.unit_price = new_svc.price
                    u.start, u.end = new_svc.departs_at, new_svc.arrives_at
                elif u.category == "hotel":
                    try:
                        room = new_svc.room(u.room_type)
                    except KeyError:
                        raise ArrangementError(
                            f"替代酒店 {new_service_id} 无房型 {u.room_type}", [u.room_type]
                        )
                    required = []
                    for req in self.registry.rooming_request:
                        if req["unit_id"] == u.room_unit:
                            required = req.get("requirements", [])
                    union = set(new_svc.features) | set(room.features)
                    missing = [f for f in required if f not in union]
                    if missing:
                        raise ArrangementError(f"替代酒店 {u.room_type} 不满足房单 {u.room_unit}", missing)
                    if room.occupancy < len(self.registry.members_of_unit(u.room_unit)):
                        raise ArrangementError("替代酒店房型容量不足")
                    u.unit_price = room.price
                elif u.category == "vehicle":
                    self._require_features(new_svc, self._vehicle_requirements(), "包车")
                    if (new_svc.capacity or 0) < len(active):
                        raise ArrangementError("替代包车座位不足")
                    u.unit_price = new_svc.price
                elif u.category == "restaurant":
                    self._require_features(new_svc, self._meal_requirements(), "餐饮")
                    u.unit_price = new_svc.price
                elif u.category == "attraction":
                    self._require_features(new_svc, self._visit_requirements(), "景点")
                    u.unit_price = new_svc.price
                elif u.category == "local_staff":
                    self._require_features(new_svc, self._guide_requirements(), "向导")
                    u.unit_price = new_svc.price
                elif u.category == "local_service":
                    self._require_features(new_svc, self._meet_requirements(), "当地服务")
                    u.unit_price = new_svc.price
                # 通过：提交该锁
                u.service_id = new_service_id
                u.supplier_id = new_svc.supplier_id
                ev = self.itinerary.get(u.event_id)
                ev.service_id = new_service_id
                ev.supplier_id = new_svc.supplier_id
                ev.title = new_svc.name
                if u.category == "flight":
                    ev.start, ev.end = new_svc.departs_at, new_svc.arrives_at
                self.disclosures[u.lock_id] = build_disclosure(
                    self.registry, new_svc, unit_id=u.room_unit, booking_ref=u.lock_id
                )
        except ArrangementError:
            # 原子回滚
            self.units = snapshot["units"]
            for eid, ev in snapshot["events"].items():
                self.itinerary.events[eid] = ev
            self._log("replace_rejected", old=old_service_id, new=new_service_id)
            raise
        self._log("replace_supplier", old=old_service_id, new=new_service_id, locks=len(affected))
        return {"replaced_locks": len(affected), "new_service": new_service_id}

    # ------------------------------------------------------------- 成员退出
    def withdraw_member(self, member_id: str, on_date: datetime) -> dict:
        """成员退出：只释放本人个人占用；家庭房/包车/向导等团队锁原样保留。"""
        if self.stage not in ("confirmed", "inbound"):
            raise ArrangementError(f"阶段 {self.stage} 不允许成员退出")
        member = self.registry.get(member_id)
        if not member.active:
            raise ArrangementError(f"成员 {member_id} 已退出，不能重复办理")
        released_value = Money.of(0, self.ledger.deposit_currency)
        released: list[str] = []
        kept_locks: list[str] = []
        days_ahead = (self.units[self._flights["outbound"]].start.astimezone(timezone.utc)
                      - on_date.astimezone(timezone.utc)).total_seconds() / 86400
        refund_pct = self._tier_refund_pct(days_ahead)
        for u in self.active_units():
            ev = self.itinerary.get(u.event_id)
            if u.scope == "PERSON" and member_id in u.participants:
                # 个人占用：释放本人座位/餐位/门票
                before = u.line_total(self._active_ids())
                u.participants.remove(member_id)
                u.released_participants.append(member_id)
                if ev.participants != ["TEAM"]:
                    ev.participants = [p for p in ev.participants if p != member_id]
                after = u.line_total(self._active_ids() - {member_id})
                released_value += self.fx.convert(before - after, self.ledger.deposit_currency)
                released.append(u.lock_id)
            else:
                kept_locks.append(u.lock_id)
        self.registry.withdraw(member_id)
        refund_entry = self.ledger.refund(released_value.scaled(refund_pct),
                                          reason=f"成员 {member_id} 退出，个人占用释放，跨境取消档 {refund_pct}%",
                                          ref=member_id)
        # 团队人数变化：全部未取消锁的合作方单据按最小披露重新生成
        for u in list(self.units.values()):
            if u.status == "cancelled":
                continue
            self.disclosures[u.lock_id] = build_disclosure(
                self.registry, self.catalog.get(u.service_id),
                unit_id=u.room_unit, booking_ref=u.lock_id)
        self._log("withdraw", member_id=member_id, released=released,
                  kept_locks=kept_locks, refund=str(refund_entry))
        return {
            "member": member_id,
            "released_personal": released,
            "family_locks_intact": kept_locks,
            "refund": str(Money(refund_entry.amount, refund_entry.currency)),
        }

    def _tier_refund_pct(self, days_ahead: float) -> int:
        tiers = sorted(self.policies["cancellation_policy"]["tiers"], key=lambda t: -t["before_days"])
        for t in tiers:
            if days_ahead >= t["before_days"]:
                return t["refund_pct"]
        return tiers[-1]["refund_pct"]

    # ------------------------------------------------------------- 航班延误
    def report_flight_delay(self, service_id: str, new_departs: datetime, new_arrives: datetime,
                            reason: str = "delay") -> dict:
        """更新航班时刻并把时差传播到锚定该航班的接机/接驳/餐饮事件。"""
        target = [u for u in self.active_units() if u.service_id == service_id and u.category == "flight"]
        if not target:
            raise ArrangementError(f"未锁定航班 {service_id}")
        flight_unit = target[0]
        ev = self.itinerary.get(flight_unit.event_id)
        delta_dep = new_departs - flight_unit.start
        delta_arr = new_arrives - flight_unit.end
        if delta_dep <= timedelta(0) and delta_arr <= timedelta(0):
            raise ArrangementError("时刻未推迟，不构成延误")
        flight_unit.start, flight_unit.end = new_departs, new_arrives
        ev.start, ev.end = new_departs, new_arrives
        ev.status = "delayed"
        shifted, converted = [], []
        threshold = timedelta(minutes=self.policies["delay_policy"]["meet_adjustment_minutes"])
        if delta_arr < threshold:
            self._log("flight_delay_within_threshold", service_id=service_id,
                      arrival_delay_minutes=int(delta_arr.total_seconds() / 60))
            ev.status = "delayed"
            return {"shifted": [], "converted_meal": [],
                    "arrival_delay_minutes": int(delta_arr.total_seconds() / 60),
                    "note": "延误低于阈值，接驳安排保持不变"}
        for other in list(self.active_units()):
            if other.lock_id == flight_unit.lock_id:
                continue
            oev = self.itinerary.get(other.event_id)
            if oev.depends_on != ev.event_id:
                continue
            new_start = other.start + delta_arr
            new_end = other.end + delta_arr
            if other.category == "restaurant":
                svc = self.catalog.get(other.service_id)
                last_order = svc.raw.get("last_order_local", "23:59")
                hh, mm = map(int, last_order.split(":"))
                cutoff = new_start.replace(hour=hh, minute=mm, second=0, microsecond=0)
                buffer = timedelta(minutes=self.policies["delay_policy"]["restaurant_last_order_buffer_minutes"])
                if new_start > cutoff - buffer:
                    # 餐厅已无法接待：转为深夜清真餐盒；已退出成员在退出环节已结算，不重复退款
                    refund_ccy = self.fx.convert(
                        other.unit_price * len(other.participants), self.ledger.deposit_currency)
                    converted.append(self._convert_meal_to_box(other, reason))
                    self.ledger.refund(refund_ccy, reason=f"航班延误({reason})导致首晚餐饮无法履约，全额退回",
                                       ref=other.lock_id)
                    continue
            other.start, other.end = new_start, new_end
            oev.start, oev.end = new_start, new_end
            oev.status = "shifted"
            shifted.append(other.lock_id)
        self._log("flight_delay", service_id=service_id, reason=reason,
                  arrival_delay_minutes=int(delta_arr.total_seconds() / 60),
                  shifted=shifted, converted=converted)
        return {"shifted": shifted, "converted_meal": converted,
                "arrival_delay_minutes": int(delta_arr.total_seconds() / 60)}

    def _convert_meal_to_box(self, meal_unit: Unit, reason: str) -> str:
        box_svc = self.catalog.get("SVC-HALALBOX")
        pax = len(meal_unit.participants)
        meal_unit.status = "cancelled"
        old_ev = self.itinerary.get(meal_unit.event_id)
        old_ev.status = "cancelled"
        lock_id = self._next("LOCK-SVC")
        event_id = self._next("EVT-SVC")
        box = Unit(
            lock_id=lock_id, category="local_service", service_id=box_svc.service_id,
            supplier_id=box_svc.supplier_id, scope="PERSON", indivisible=False,
            participants=list(meal_unit.participants),
            start=meal_unit.start, end=meal_unit.end, unit_price=box_svc.price, qty=1,
            event_id=event_id, extra={"replaces": meal_unit.lock_id, "cause": reason},
        )
        ev = self._event_for(box, box_svc, box.start, box.end, CST,
                             detail={"replaces": meal_unit.lock_id, "late_night": True})
        ev.status = "shifted"
        self.units[lock_id] = box
        self.itinerary.add(ev)
        self.disclosures[lock_id] = build_disclosure(self.registry, box_svc, booking_ref=lock_id)
        return lock_id

    # ------------------------------------------------------------- 跨境取消
    def cancel_team(self, on_date: datetime, reason: str = "guest_cancel") -> dict:
        if self.stage not in ("confirmed", "inbound"):
            raise ArrangementError(f"阶段 {self.stage} 不可整体取消")
        outbound_start = self.units[self._flights["outbound"]].start
        days_ahead = (outbound_start.astimezone(timezone.utc) - on_date.astimezone(timezone.utc)).total_seconds() / 86400
        carrier_fault = reason in ("carrier_cancel",)
        refund_pct = 100 if carrier_fault else self._tier_refund_pct(days_ahead)
        deposits = [e for e in self.ledger.entries if e.kind == "deposit"]
        prior_refunds = [e for e in self.ledger.entries if e.kind == "refund"]
        gross = sum((e.amount for e in deposits), Decimal(0))
        gross -= sum((e.amount for e in prior_refunds), Decimal(0))
        paid = Money.of(gross, self.ledger.deposit_currency)
        refund_entry = self.ledger.refund(paid.scaled(refund_pct),
                                          reason=f"跨境整体取消（{reason}），退款按原收款币种 AED，扣除已退款项",
                                          ref="TEAM")
        for u in self.active_units():
            u.status = "cancelled"
            self.itinerary.get(u.event_id).status = "cancelled"
        for m in self.registry.active_members():
            self.registry.withdraw(m.member_id)
        self._log("cross_border_cancel", reason=reason, refund_pct=refund_pct, refund=str(refund_entry))
        return {"reason": reason, "refund_pct": refund_pct,
                "refund": str(Money(refund_entry.amount, refund_entry.currency))}

    # ----------------------------------------------------------------- 阶段
    def mark_border_entry(self, at: datetime) -> dict:
        """入境：边检只扫描令牌引用，登记哪些令牌完成了入境核验。"""
        if self.stage != "confirmed":
            raise ArrangementError(f"阶段 {self.stage} 不能办理入境")
        presented = [self.registry.token_for(m.member_id).token_ref for m in self.registry.active_members()]
        self.stage = "inbound"
        self._log("border_entry", at=at.isoformat(), tokens_presented=presented)
        return {"stage": self.stage, "tokens_presented": presented}

    def start_in_destination(self) -> dict:
        if self.stage != "inbound":
            raise ArrangementError(f"阶段 {self.stage} 不能进入消费")
        self.stage = "in_destination"
        self._log("stage", to="in_destination")
        return {"stage": self.stage}

    def mark_revisit(self, interest: str = "") -> dict:
        if self.stage != "in_destination":
            raise ArrangementError(f"阶段 {self.stage} 不能转入复访")
        self.stage = "revisit"
        self._log("stage", to="revisit", interest=interest)
        return {"stage": self.stage, "interest": interest}

    # ----------------------------------------------------------------- 视图
    def guest_itinerary(self, lang: str) -> list[dict]:
        return self.itinerary.guest_view(lang)

    def operator_view(self) -> dict:
        return {
            "team_id": self.registry.team_id,
            "stage": self.stage,
            "stage_name": STAGE_NAMES[self.stage],
            "active_pax": len(self.registry.active_members()),
            "locks": [
                {
                    "lock_id": u.lock_id, "category": u.category, "service_id": u.service_id,
                    "scope": u.scope, "indivisible": u.indivisible,
                    "participants": u.participants, "released": u.released_participants,
                    "line_total": str(u.line_total(self._active_ids())),
                    "status": u.status,
                }
                for u in self.units.values()
            ],
            "money": {
                "total_cny": str(self.total_in("CNY")),
                "total_aed": str(self.total_in("AED")),
                "ledger_totals": {k: str(v.quantize(Decimal("0.01"))) for k, v in self.ledger.totals().items()},
            },
            "audit_tail": self.audit[-5:],
        }
