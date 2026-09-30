"""预订单元与退出释放规则。

整套安排被拆成五类“预订单元”：航班座位、酒店房间、包车车辆、
景点场次、当地服务人员。单元有两种保持策略：

- INDIVIDUAL：个人席位（如航班座位）。成员退出时只释放他自己的席位，
  其余席位不动。
- GROUP_LOCKED：整体锁定（家庭房、包车、随团服务、家庭景点包场）。
  成员退出只摘除该成员的个人占用，单元本身、容量与价格约束保持不变，
  绝不允许因一人退出而拆房、换小车或拆散家庭成员。

整单元取消（如跨境取消）走编排器的显式取消流程，退出流程无权拆散单元。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .members import AccessNeed, Diet, Language


class UnitKind(str, Enum):
    FLIGHT_SEAT = "flight_seat"
    HOTEL_ROOM = "hotel_room"
    CHARTER_VEHICLE = "charter_vehicle"
    ATTRACTION_SLOT = "attraction_slot"
    LOCAL_STAFF = "local_staff"


class Retention(str, Enum):
    INDIVIDUAL = "individual"      # 退出释放个人席位
    GROUP_LOCKED = "group_locked"  # 退出不拆散单元


class UnitStatus(str, Enum):
    HELD = "held"            # 咨询/已占位未确认
    CONFIRMED = "confirmed"  # 已确认（定金后）
    CANCELLED = "cancelled"  # 整单元已取消
    COMPLETED = "completed"  # 已履约


# 各类单元默认的保持策略
DEFAULT_RETENTION: dict[UnitKind, Retention] = {
    UnitKind.FLIGHT_SEAT: Retention.INDIVIDUAL,
    UnitKind.HOTEL_ROOM: Retention.GROUP_LOCKED,
    UnitKind.CHARTER_VEHICLE: Retention.GROUP_LOCKED,
    UnitKind.ATTRACTION_SLOT: Retention.INDIVIDUAL,
    UnitKind.LOCAL_STAFF: Retention.GROUP_LOCKED,
}


class UnitError(ValueError):
    pass


@dataclass(frozen=True)
class ExitEffect:
    """成员退出对某单元产生的效果（供结算与通知使用）。"""

    unit_id: str
    kind: UnitKind
    retention: Retention
    detached_member: str
    slots_released: int          # 真正释放回供应容量的席位数
    unit_kept: bool              # 单元是否保持原状
    remaining_occupants: tuple[str, ...]


@dataclass
class BookingUnit:
    unit_id: str
    kind: UnitKind
    supplier_id: str
    service_ref: str                     # 供应商侧服务/产品编号
    capacity: int                        # 容量（座位数/床位数/座位数）
    retention: Retention | None = None
    requirements: frozenset[str] = frozenset()  # 能力标签：halal_kitchen 等
    diets: frozenset[Diet] = frozenset()
    access_needs: frozenset[AccessNeed] = frozenset()
    languages: frozenset[Language] = frozenset()
    status: UnitStatus = UnitStatus.HELD
    occupants: set[str] = field(default_factory=set)
    # GROUP_LOCKED 单元在退出后仍需保留的付费容量（家庭房/包车不缩容）
    locked_capacity: int = 0

    def __post_init__(self) -> None:
        if self.retention is None:
            self.retention = DEFAULT_RETENTION[self.kind]
        if self.retention is Retention.GROUP_LOCKED and self.locked_capacity == 0:
            self.locked_capacity = self.capacity

    def occupy(self, member_id: str) -> None:
        if self.status is UnitStatus.CANCELLED:
            raise UnitError(f"单元 {self.unit_id} 已取消，不能占用")
        if len(self.occupants) >= self.capacity and self.retention is Retention.INDIVIDUAL:
            raise UnitError(f"单元 {self.unit_id} 容量已满")
        self.occupants.add(member_id)

    def covers_members(self, member_ids: set[str]) -> bool:
        return member_ids.issubset(self.occupants)

    @property
    def held_capacity(self) -> int:
        """对外承担费用/保留的容量：锁定单元不随退出缩减。"""
        if self.retention is Retention.GROUP_LOCKED and self.status is not UnitStatus.CANCELLED:
            return max(self.locked_capacity, len(self.occupants))
        return len(self.occupants)

    def detach_on_exit(self, member_id: str) -> ExitEffect | None:
        """成员退出时调用。

        INDIVIDUAL：移除占用并释放 1 个席位。
        GROUP_LOCKED：摘除成员个人占用，但 locked_capacity 不变，
        单元保持确认状态——家庭房不拆分、包车不换小。
        """
        if member_id not in self.occupants:
            return None
        self.occupants.remove(member_id)
        assert self.retention is not None
        if self.retention is Retention.INDIVIDUAL:
            released = 1
            kept = True
        else:
            released = 0
            kept = True
        if not self.occupants and self.retention is Retention.INDIVIDUAL:
            # 个人席位单元无人即自然释放；是否通知供应商取消由编排器决定
            pass
        return ExitEffect(
            unit_id=self.unit_id,
            kind=self.kind,
            retention=self.retention,
            detached_member=member_id,
            slots_released=released,
            unit_kept=kept,
            remaining_occupants=tuple(sorted(self.occupants)),
        )

    def cancel(self) -> None:
        """整单元取消，只允许来自编排器的跨境取消/供应商更换流程。"""
        if self.status is UnitStatus.COMPLETED:
            raise UnitError(f"单元 {self.unit_id} 已履约，不能取消")
        self.status = UnitStatus.CANCELLED
