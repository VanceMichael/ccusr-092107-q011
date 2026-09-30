"""从脱敏 fixture 构建城市目录与家庭团队。

所有数据均为虚构示例，不含真实姓名、证件号或账号。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .capabilities import Capability, CityDirectory, ServiceOption
from .members import (
    AccessNeed,
    CredentialKind,
    CredentialToken,
    Diet,
    FamilyTeam,
    KinRole,
    Language,
    Member,
    TokenStatus,
)
from .units import UnitKind


def load_city(path: Path) -> CityDirectory:
    data = json.loads(path.read_text(encoding="utf-8"))
    directory = CityDirectory()
    for o in data["options"]:
        directory.register(
            ServiceOption(
                supplier_id=o["supplier_id"],
                service_ref=o["service_ref"],
                kind=UnitKind(o["kind"]),
                city=o["city"],
                timezone=o["timezone"],
                capacity=o["capacity"],
                capabilities=frozenset(Capability(c) for c in o["capabilities"]),
                display=o["display"],
                refundable=o.get("refundable", False),
                cross_border_cancel_deadline_h=o.get("cross_border_cancel_deadline_h"),
            )
        )
    return directory


def load_team(path: Path) -> FamilyTeam:
    data = json.loads(path.read_text(encoding="utf-8"))
    team = FamilyTeam(
        team_id=data["team_id"],
        home_city=data["home_city"],
        destination=data["destination"],
        _head_id=data["head_id"],
    )
    for raw in data["members"]:
        t = raw["token"]
        token = CredentialToken(
            token_id=t["token_id"],
            kind=CredentialKind(t["kind"]),
            masked_ref=t["masked_ref"],
            issuer=t["issuer"],
            issued_at=datetime.fromisoformat(t["issued_at"]),
            expires_at=datetime.fromisoformat(t["expires_at"]) if t.get("expires_at") else None,
            status=TokenStatus(t.get("status", "active")),
        )
        team.add(
            Member(
                member_id=raw["member_id"],
                member_code=raw["member_code"],
                token=token,
                age_band=raw["age_band"],
                diets=frozenset(Diet(d) for d in raw.get("diets", [])),
                languages=frozenset(Language(l) for l in raw.get("languages", [])),
                access_needs=frozenset(AccessNeed(a) for a in raw.get("access_needs", [])),
            )
        )
    for r in data.get("relations", []):
        team.relate(r["a"], r["b"], KinRole(r["role"]))
    return team
