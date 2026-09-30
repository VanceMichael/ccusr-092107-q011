"""合作方最小信息披露（Minimum Disclosure）。

同一笔订单向不同类别合作方下发的字段不同；任何单据只含：
- 完成该服务所必需的字段；
- 令牌引用 / 化名，不含真实证件号、demo_hash 之外的身份数据；
- 计数级信息（人数、轮椅数）而非成员明细；
- 绝不附带亲属关系与其他房单成员。
"""

from __future__ import annotations

from .members import MemberRegistry
from .catalog import ServiceDef


def _pseudonym(member_index: int) -> str:
    return f"GUEST-{member_index:02d}"


def build_disclosure(
    registry: MemberRegistry,
    svc: ServiceDef,
    unit_id: str | None = None,
    booking_ref: str = "",
) -> dict:
    """为单个合作方服务生成最小披露单据。"""
    policy = registry_policies(registry)
    allowed = set(policy[svc.category]["receive"])
    active = registry.active_members()

    payload: dict = {
        "booking_ref": booking_ref,
        "supplier_id": svc.supplier_id,
        "service_id": svc.service_id,
        "category": svc.category,
    }

    # 令牌类字段：只对航班/酒店（需要办理值机与入住登记）开放。
    # 航班拿到全团令牌；酒店只拿本房单成员，且姓名一律用化名，亲属关系不出系统。
    if "token_ref" in allowed:
        if svc.category == "hotel" and unit_id is not None:
            scope_members = [m for m in registry.members_of_unit(unit_id) if m.active]
        else:
            scope_members = active
        tokens = []
        for m in scope_members:
            tok = registry.token_for(m.member_id)
            entry = {
                "guest": _pseudonym(active.index(m) + 1),
                "token_ref": tok.token_ref,
                "doc_type": tok.doc_type,
                "issuing_country": tok.issuing_country,
                "masked_name": _pseudonym(active.index(m) + 1),
            }
            if svc.category == "flight":
                entry["demo_hash"] = tok.demo_hash
            tokens.append(entry)
        payload["guests"] = tokens

    # 酒店：只暴露本房单成员与无障碍/餐饮认证需求
    if svc.category == "hotel" and unit_id is not None:
        unit_members = [m for m in registry.members_of_unit(unit_id) if m.active]
        payload["rooming_unit"] = {
            "unit_code": f"ROOM-UNIT-{unit_id}",
            "occupants": [_pseudonym(i) for i, _ in enumerate(unit_members, start=1)],
            "accessibility_need": sorted({a for m in unit_members for a in m.accessibility}),
            "diet_cert": "halal_breakfast_certified",
        }

    if "pax_count" in allowed:
        payload["pax_count"] = len(active)
    if "wheelchair_count" in allowed:
        payload["wheelchair_count"] = registry.wheelchair_count()
    if "family_brief" in allowed:
        adults = sum(1 for m in active if m.age_band == "adult")
        children = sum(1 for m in active if m.age_band == "child")
        seniors = sum(1 for m in active if m.age_band == "senior")
        payload["family_brief"] = {"adults": adults, "children": children, "seniors": seniors}
    if "diet" in allowed:
        payload["diet"] = sorted({m.diet for m in active})
    if "language" in allowed or "language_sign" in allowed:
        key = "language" if "language" in allowed else "language_sign"
        payload[key] = ["ar", "en"]

    # 安全断言：禁止字段绝不出现（递归检查键名）
    forbidden = set(policy[svc.category]["forbid"])

    def _scan(node, path=""):
        if isinstance(node, dict):
            for k, v in node.items():
                if k in forbidden:
                    raise AssertionError(f"最小披露违规：{svc.category} 单据含禁止字段 {k}")
                _scan(v, f"{path}.{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                _scan(v, f"{path}[{i}]")

    _scan(payload)
    return payload


def registry_policies(registry: MemberRegistry) -> dict:
    return getattr(registry, "disclosure_policy", None) or _DEFAULT_POLICY


_DEFAULT_POLICY = {
    "flight":       {"receive": ["token_ref", "doc_type", "issuing_country", "demo_hash", "masked_name"], "forbid": ["kinship", "roommate", "diet_other_members"]},
    "hotel":        {"receive": ["token_ref", "rooming_unit", "counts_only_outside_unit", "accessibility_need", "diet_cert"], "forbid": ["doc_number", "kinship", "other_units_occupants"]},
    "vehicle":      {"receive": ["pax_count", "wheelchair_count", "pickup_window", "language_sign"], "forbid": ["token_ref", "doc_number", "kinship", "guest_names", "demo_hash"]},
    "restaurant":   {"receive": ["pax_count", "diet", "private_room", "accessibility_need", "arrival_window"], "forbid": ["token_ref", "doc_number", "kinship", "guest_names", "demo_hash"]},
    "attraction":   {"receive": ["pax_count", "wheelchair_count", "visit_window"], "forbid": ["token_ref", "doc_number", "kinship", "guest_names", "demo_hash"]},
    "local_staff":  {"receive": ["pax_count", "family_brief", "accessibility_need", "language", "diet"], "forbid": ["doc_number", "token_hash", "demo_hash"]},
    "local_service":{"receive": ["pax_count", "wheelchair_count", "language_sign", "arrival_window"], "forbid": ["doc_number", "kinship", "guest_names", "demo_hash"]},
}
