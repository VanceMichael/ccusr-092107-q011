"""合作方最小信息披露。

每个合作方只能看到履约所必需的字段，且随运营阶段收窄/切换：

- 咨询阶段：只有需求摘要（清真、无障碍、语言），没有任何人；
- 确认/入境：所服务单元内的脱敏成员码与脱敏证件指纹；
- 消费：核销码；
- 复访：仅偏好。

任何视图都不包含真实姓名、证件号、其他成员与其他供应商安排。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .lifecycle import Stage, STAGE_DISCLOSURE
from .members import FamilyTeam
from .units import BookingUnit


class DisclosureError(ValueError):
    pass


class PartnerRole(str, Enum):
    AIRLINE = "airline"
    HOTEL = "hotel"
    GROUND_TRANSPORT = "ground_transport"
    ATTRACTION = "attraction"
    LOCAL_SERVICE = "local_service"


@dataclass(frozen=True)
class Partner:
    partner_id: str
    role: PartnerRole
    service_ref: str  # 该合作方履约的具体服务编号


def _requirements_summary(team: FamilyTeam) -> dict:
    return {
        "headcount": len(team.members()),
        "diets": sorted(d.value for d in team.diet_requirements()),
        "access_needs": sorted(a.value for a in team.access_requirements()),
        "languages": sorted(l.value for l in team.languages()),
    }


def build_partner_view(
    partner: Partner,
    unit: BookingUnit,
    team: FamilyTeam,
    stage: Stage,
    *,
    arrival_window: str | None = None,
    redemption_code: str | None = None,
) -> dict:
    """生成某个合作方在某阶段对某单元可见的最小数据包。"""
    if unit.supplier_id != partner.partner_id or unit.service_ref != partner.service_ref:
        raise DisclosureError("合作方只能查看自己履约的单元")
    allowed = STAGE_DISCLOSURE[stage]
    view: dict = {"unit_ref": unit.service_ref, "stage": stage.value}

    if "requirements_summary" in allowed:
        view["requirements"] = _requirements_summary(team)

    if "occupant_codes" in allowed:
        codes = []
        for mid in sorted(unit.occupants):
            m = team.try_get(mid)
            if m is not None and m.active:
                codes.append(m.member_code)
        view["occupants"] = codes

    if "credential_masked" in allowed and partner.role is PartnerRole.AIRLINE:
        # 证件脱敏指纹只下发给需要乘机核验的航司；酒店/车辆/景点/导游
        # 即便在确认或入境阶段也拿不到任何证件数据
        masked = []
        for mid in sorted(unit.occupants):
            m = team.try_get(mid)
            if m is not None and m.active:
                masked.append({"code": m.member_code, "credential": m.token.masked_ref})
        view["credentials_masked"] = masked

    if "arrival_window" in allowed and arrival_window:
        view["arrival_window"] = arrival_window

    if "redemption_code" in allowed:
        view["redemption_code"] = redemption_code or "PENDING"

    if "preferences_only" in allowed:
        view["preferences"] = _requirements_summary(team)

    return view
