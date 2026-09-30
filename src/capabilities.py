"""城市服务能力目录。

登记重庆（及未来其他入境城市）的可订服务与其能力标签。能力标签是
编排约束匹配的唯一依据，例如家庭团需要 ``halal_kitchen``、
``family_room``、``arabic_staff``、``wheelchair_access``、
``child_seat`` 时，只有同时满足的供应商才能被选中。

供应商条目只保存业务编号与能力，不保存联系人隐私；真实联系方式由
合作方网关在最小披露范围内换取。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .members import AccessNeed, Diet, Language
from .units import UnitKind


class Capability(str, Enum):
    # 餐饮
    HALAL_KITCHEN = "halal_kitchen"        # 清真厨房（非猪肉、非酒精器具）
    HALAL_CERTIFIED = "halal_certified"    # 官方清真认证
    PRIVATE_DINING = "private_dining"      # 家庭独立包间
    # 住宿
    FAMILY_ROOM = "family_room"            # 家庭房（可整户锁房）
    ADJOINING_ROOMS = "adjoining_rooms"    # 相邻连通房
    PRAYER_DIRECTION = "prayer_direction"  # 礼拜朝向标记
    RAMADAN_SERVICE = "ramadan_service"    # 封开斋时段服务
    BABY_CRIB = "baby_crib"                # 婴儿床
    # 通行
    WHEELCHAIR_ACCESS = "wheelchair_access"
    GROUND_FLOOR_ACCESS = "ground_floor_access"
    ELEVATOR = "elevator"
    CHILD_SEAT = "child_seat"
    LARGE_LUGGAGE = "large_luggage"
    # 语言
    ARABIC_STAFF = "arabic_staff"
    ENGLISH_STAFF = "english_staff"
    # 接驳
    AIRPORT_PICKUP = "airport_pickup"
    CROSS_CITY_TRANSFER = "cross_city_transfer"
    # 票务
    FAMILY_PACKAGE = "family_package"
    PRIVATE_SLOT = "private_slot"          # 家庭包场/独立场次
    REFUNDABLE = "refundable"
    # 签证/入境
    VISA_LETTER = "visa_letter"
    PORT_OF_ENTRY_SUPPORT = "port_of_entry_support"


# 团队需求 -> 必备能力标签的映射
DIET_CAPABILITY: dict[Diet, Capability] = {
    Diet.HALAL: Capability.HALAL_KITCHEN,
}
ACCESS_CAPABILITY: dict[AccessNeed, Capability] = {
    AccessNeed.WHEELCHAIR: Capability.WHEELCHAIR_ACCESS,
    AccessNeed.WALKER: Capability.WHEELCHAIR_ACCESS,
    AccessNeed.GROUND_FLOOR: Capability.GROUND_FLOOR_ACCESS,
    AccessNeed.CHILD_SEAT: Capability.CHILD_SEAT,
}
LANGUAGE_CAPABILITY: dict[Language, Capability] = {
    Language.AR: Capability.ARABIC_STAFF,
    Language.EN: Capability.ENGLISH_STAFF,
}


@dataclass(frozen=True)
class ServiceOption:
    """一个可订的供应选项。"""

    supplier_id: str
    service_ref: str
    kind: UnitKind
    city: str
    timezone: str                    # IANA，如 Asia/Shanghai / Asia/Dubai
    capacity: int
    capabilities: frozenset[Capability]
    # 多语展示名：客人看到的名称只有服务，不含供应商主体信息
    display: dict[str, str]          # {"zh": ..., "ar": ..., "en": ...}
    refundable: bool = False
    cross_border_cancel_deadline_h: int | None = None  # 跨境取消免费时限

    def has(self, cap: Capability) -> bool:
        return cap in self.capabilities

    def meets(self, required: frozenset[Capability]) -> bool:
        return required.issubset(self.capabilities)


class CapabilityError(ValueError):
    pass


@dataclass
class CityDirectory:
    """按城市登记的服务能力目录。"""

    _options: dict[str, ServiceOption] = field(default_factory=dict)

    def register(self, option: ServiceOption) -> None:
        key = f"{option.supplier_id}:{option.service_ref}"
        if key in self._options:
            raise CapabilityError(f"服务 {key} 已登记")
        self._options[key] = option

    def get(self, supplier_id: str, service_ref: str) -> ServiceOption:
        key = f"{supplier_id}:{service_ref}"
        try:
            return self._options[key]
        except KeyError:
            raise CapabilityError(f"服务 {key} 不在目录中") from None

    def find(
        self,
        *,
        city: str,
        kind: UnitKind,
        required: frozenset[Capability] = frozenset(),
        min_capacity: int = 1,
        refundable: bool | None = None,
    ) -> tuple[ServiceOption, ...]:
        """按城市、类型、必备能力、容量筛选可选项。"""
        out = []
        for o in self._options.values():
            if o.city != city or o.kind != kind:
                continue
            if o.capacity < min_capacity:
                continue
            if not o.meets(required):
                continue
            if refundable is True and not o.refundable:
                continue
            out.append(o)
        return tuple(sorted(out, key=lambda o: (o.supplier_id, o.service_ref)))

    @staticmethod
    def required_capabilities(
        diets: frozenset[Diet],
        access: frozenset[AccessNeed],
        languages: frozenset[Language],
        *,
        family_locked: bool = False,
        charter: bool = False,
        airport: bool = False,
    ) -> frozenset[Capability]:
        """把团队需求展开为必备能力集合。"""
        caps: set[Capability] = set()
        for d in diets:
            if d in DIET_CAPABILITY:
                caps.add(DIET_CAPABILITY[d])
        for a in access:
            if a in ACCESS_CAPABILITY:
                caps.add(ACCESS_CAPABILITY[a])
        for lang in languages:
            if lang in LANGUAGE_CAPABILITY:
                caps.add(LANGUAGE_CAPABILITY[lang])
        if family_locked:
            caps.add(Capability.FAMILY_ROOM)
        if charter:
            caps.add(Capability.CHILD_SEAT)
            caps.add(Capability.LARGE_LUGGAGE)
        if airport:
            caps.add(Capability.AIRPORT_PICKUP)
        return frozenset(caps)
