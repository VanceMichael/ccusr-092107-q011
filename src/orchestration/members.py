"""团队成员、证件令牌与亲属关系。

对外一律使用 ``token_ref`` 令牌引用，真实证件号从不进入编排系统；
亲属关系用于家庭房分组与向导服务简报，禁止随服务单据下发给合作方。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Member:
    member_id: str
    token_ref: str
    family_unit: str
    age_band: str  # adult / child / senior
    diet: str
    kinship_to_head: str
    kinship_label: dict[str, str] = field(default_factory=dict)
    accessibility: list[str] = field(default_factory=list)
    active: bool = True

    @property
    def needs_wheelchair(self) -> bool:
        return "wheelchair" in self.accessibility

    def label(self, lang: str) -> str:
        return self.kinship_label.get(lang, self.kinship_to_head)


@dataclass
class CredentialToken:
    token_ref: str
    member_id: str
    doc_type: str
    issuing_country: str
    doc_number_masked: str
    demo_hash: str
    scopes: list[str]
    status: str = "active"
    expires_on: str = ""

    @property
    def active(self) -> bool:
        return self.status == "active"


class MemberRegistry:
    def __init__(self, team: dict, credentials: dict):
        self.team_id = team["team_id"]
        self.languages = list(team["languages"])
        self.tz_source = team["source_timezone"]
        self.tz_dest = team["destination_timezone"]
        self.head_member_id = team["head_member_id"]
        self.preferences = team.get("preferences", {})
        self.rooming_request = team["rooming_request"]
        self.vehicle_request = team["vehicle_request"]

        self.members: dict[str, Member] = {}
        for raw in team["members"]:
            self.members[raw["member_id"]] = Member(
                member_id=raw["member_id"],
                token_ref=raw["token_ref"],
                family_unit=raw["family_unit"],
                age_band=raw["age_band"],
                diet=raw["diet"],
                kinship_to_head=raw["kinship_to_head"],
                kinship_label=raw.get("kinship_label", {}),
                accessibility=raw.get("accessibility", []),
            )
        self.kinship_edges = [(e["from"], e["to"], e["relation"]) for e in team["kinship_edges"]]

        self.tokens: dict[str, CredentialToken] = {}
        for raw in credentials["tokens"]:
            tok = CredentialToken(
                token_ref=raw["token_ref"],
                member_id=raw["member_id"],
                doc_type=raw["doc_type"],
                issuing_country=raw["issuing_country"],
                doc_number_masked=raw["doc_number_masked"],
                demo_hash=raw["demo_hash"],
                scopes=raw["scopes"],
                status=raw.get("status", "active"),
                expires_on=raw.get("expires_on", ""),
            )
            self.tokens[tok.token_ref] = tok

        self._validate()

    def _validate(self) -> None:
        if self.head_member_id not in self.members:
            raise ValueError("团长不存在")
        # 每位成员有且仅有一个有效令牌，令牌与成员一一对应
        for m in self.members.values():
            tok = self.tokens.get(m.token_ref)
            if tok is None or tok.member_id != m.member_id:
                raise ValueError(f"成员 {m.member_id} 的令牌无效")
        # 房单内的成员必须真实存在
        for unit in self.rooming_request:
            unknown = set(unit["occupants"]) - set(self.members)
            if unknown:
                raise ValueError(f"房单 {unit['unit_id']} 含未知成员：{unknown}")
        # 每个成员只能出现在一个家庭房单元中（家庭房不可拆分的前提）
        seen: dict[str, str] = {}
        for unit in self.rooming_request:
            for mid in unit["occupants"]:
                if mid in seen:
                    raise ValueError(f"成员 {mid} 被重复分入 {seen[mid]} 与 {unit['unit_id']}")
                seen[mid] = unit["unit_id"]

    # ---- 查询 ----
    def active_members(self) -> list[Member]:
        return [m for m in self.members.values() if m.active]

    def get(self, member_id: str) -> Member:
        return self.members[member_id]

    def token_for(self, member_id: str) -> CredentialToken:
        return self.tokens[self.members[member_id].token_ref]

    def members_of_unit(self, unit_id: str) -> list[Member]:
        for unit in self.rooming_request:
            if unit["unit_id"] == unit_id:
                return [self.members[mid] for mid in unit["occupants"]]
        raise KeyError(unit_id)

    def unit_of(self, member_id: str) -> str | None:
        for unit in self.rooming_request:
            if member_id in unit["occupants"]:
                return unit["unit_id"]
        return None

    def wheelchair_count(self) -> int:
        return sum(1 for m in self.active_members() if m.needs_wheelchair)

    # ---- 成员退出 ----
    def withdraw(self, member_id: str) -> Member:
        """成员退出：仅停用本人令牌、标记本人退出。

        家庭房与包车是团队级约束，注册表不做拆散处理；
        编排引擎据此只释放该成员的个人座位/餐位等占用。
        """
        member = self.get(member_id)
        if not member.active:
            raise ValueError(f"成员 {member_id} 已退出")
        member.active = False
        tok = self.tokens[member.token_ref]
        tok.status = "revoked"
        return member
