"""成员、证件令牌与亲属关系。

面向海湾家庭团的身份管理遵循两条红线：

1. 系统内不保存证件号、姓名等原始个人信息，只保存一次性签发的
   证件令牌（credential token）与脱敏指纹；对合作方披露时另行脱敏。
2. 亲属关系是家庭房、包车等捆绑约束的判定依据，关系图双向维护，
   成员退出只改变成员状态，占用单元能否保留由预订单元另行判定。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class TokenStatus(str, Enum):
    ACTIVE = "active"        # 可用于行程核验
    SUSPENDED = "suspended"  # 航班延误/改签期间临时冻结
    REVOKED = "revoked"      # 成员退出或证件失效，不可恢复


class CredentialKind(str, Enum):
    PASSPORT_VISA = "passport_visa"  # 护照+签证合验（入境阶段）
    EMIRATES_ID = "emirates_id"      # 海湾本地证件（咨询/登记阶段）
    GROUP_PASS = "group_pass"        # 团队通行令牌（消费阶段核销）


@dataclass(frozen=True)
class CredentialToken:
    """证件令牌：只承载可核验性，不承载原始身份数据。"""

    token_id: str
    kind: CredentialKind
    masked_ref: str           # 脱敏指纹，如 "P****2471"，不保存原文
    issuer: str
    issued_at: datetime
    expires_at: datetime | None
    status: TokenStatus = TokenStatus.ACTIVE

    def usable(self, at: datetime) -> bool:
        if self.status is not TokenStatus.ACTIVE:
            return False
        return self.expires_at is None or at < self.expires_at

    def assert_usable(self, at: datetime) -> None:
        if self.status is TokenStatus.REVOKED:
            raise PermissionError(f"令牌 {self.token_id} 已撤销")
        if self.status is TokenStatus.SUSPENDED:
            raise PermissionError(f"令牌 {self.token_id} 已冻结")
        if self.expires_at is not None and at >= self.expires_at:
            raise PermissionError(f"令牌 {self.token_id} 已过期")


class KinRole(str, Enum):
    HEAD = "head"                  # 户主（登记/结算联系人）
    SPOUSE = "spouse"              # 配偶
    CHILD = "child"                # 子女
    PARENT = "parent"              # 父母
    SIBLING = "sibling"            # 兄弟姐妹
    OTHER_RELATIVE = "other"       # 其他亲属

    @property
    def inverse(self) -> "KinRole":
        if self is KinRole.CHILD:
            return KinRole.PARENT
        if self is KinRole.PARENT:
            return KinRole.CHILD
        return self


class Diet(str, Enum):
    HALAL = "halal"                # 清真
    VEGETARIAN = "vegetarian"
    NO_BEEF = "no_beef"


class Language(str, Enum):
    AR = "ar"                      # 阿拉伯语
    EN = "en"                      # 英语
    ZH = "zh"                      # 中文


class AccessNeed(str, Enum):
    WHEELCHAIR = "wheelchair"      # 轮椅通行
    WALKER = "walker"              # 行动协助
    HEARING = "hearing_assist"     # 听障协助
    GROUND_FLOOR = "ground_floor"  # 低层/电梯保障
    BABY_CRIB = "baby_crib"        # 婴儿床
    CHILD_SEAT = "child_seat"      # 儿童安全座椅


@dataclass
class Member:
    """脱敏成员：对外只用假名 member_code（如 G-01）。"""

    member_id: str
    member_code: str
    token: CredentialToken
    age_band: str                          # "adult"/"child"/"infant"，不存生日
    diets: frozenset[Diet] = frozenset()
    languages: frozenset[Language] = frozenset()
    access_needs: frozenset[AccessNeed] = frozenset()
    kin: dict[str, KinRole] = field(default_factory=dict)
    active: bool = True

    def public_view(self) -> dict:
        """客人与合作方可见的最小成员视图。"""
        return {
            "code": self.member_code,
            "age_band": self.age_band,
            "diets": sorted(d.value for d in self.diets),
            "languages": sorted(l.value for l in self.languages),
            "access_needs": sorted(a.value for a in self.access_needs),
        }


class TeamError(ValueError):
    pass


@dataclass
class FamilyTeam:
    """一个家庭独立团：成员集合 + 户主 + 亲属关系图。"""

    team_id: str
    home_city: str
    destination: str
    _head_id: str
    _members: dict[str, Member] = field(default_factory=dict)
    _history: list[str] = field(default_factory=list)

    # ---- 成员与关系 ----

    def add(self, member: Member) -> None:
        if member.member_id in self._members:
            raise TeamError(f"成员 {member.member_id} 已存在")
        self._members[member.member_id] = member

    def get(self, member_id: str) -> Member:
        try:
            m = self._members[member_id]
        except KeyError:
            raise TeamError(f"成员 {member_id} 不存在") from None
        if not m.active:
            raise TeamError(f"成员 {member.member_code} 已退出")
        return m

    def try_get(self, member_id: str) -> Member | None:
        return self._members.get(member_id)

    def members(self, include_inactive: bool = False) -> tuple[Member, ...]:
        ms = self._members.values()
        if not include_inactive:
            ms = (m for m in ms if m.active)
        return tuple(ms)

    def codes(self) -> tuple[str, ...]:
        return tuple(m.member_code for m in self.members())

    def relate(self, a_id: str, b_id: str, role: KinRole) -> None:
        """登记双向亲属关系（HEAD 除外，户主身份只指向一人）。"""
        a, b = self.get(a_id), self.get(b_id)
        if role is KinRole.HEAD:
            raise TeamError("户主关系通过 head 属性登记，不作为普通关系")
        a.kin[b_id] = role
        b.kin[a_id] = role.inverse

    @property
    def head_id(self) -> str:
        return self._head_id

    def kin_of(self, member_id: str) -> dict[str, KinRole]:
        return dict(self.get(member_id).kin)

    # ---- 需求聚合（用于匹配城市能力） ----

    def diet_requirements(self) -> frozenset[Diet]:
        out: set[Diet] = set()
        for m in self.members():
            out.update(m.diets)
        return frozenset(out)

    def access_requirements(self) -> frozenset[AccessNeed]:
        out: set[AccessNeed] = set()
        for m in self.members():
            out.update(m.access_needs)
        return frozenset(out)

    def languages(self) -> frozenset[Language]:
        out: set[Language] = set()
        for m in self.members():
            out.update(m.languages)
        return frozenset(out)

    # ---- 退出 ----

    def exit(self, member_id: str, at: datetime) -> Member:
        """成员退出：吊销其令牌、标记失活；必要时转移户主。

        只处理“人”的状态，不直接释放或拆散任何预订单元——
        单元释放/保留由编排器按最小占用规则处理。
        """
        m = self.try_get(member_id)
        if m is None:
            raise TeamError(f"成员 {member_id} 不存在")
        if not m.active:
            raise TeamError(f"成员 {m.member_code} 已退出，不能重复退出")
        m.active = False
        self._revoke(m)  # 令牌随退出吊销，且不可恢复
        # 清理他人指向该成员的关系边（历史保留在 _history）
        for other in self._members.values():
            other.kin.pop(member_id, None)
        m.kin.clear()
        self._history.append(f"{at.isoformat()} exit {m.member_code}")
        if self._head_id == member_id:
            self._transfer_head(exclude=member_id)
        return m

    @staticmethod
    def _revoke(m: Member) -> None:
        # frozen dataclass 通过 object.__setattr__ 完成状态迁移
        object.__setattr__(m.token, "status", TokenStatus.REVOKED)

    def _transfer_head(self, exclude: str) -> None:
        """户主退出时按 配偶→成年子女→父母→最早登记成员 的顺序转移。"""
        preference = {
            KinRole.SPOUSE: 0,
            KinRole.CHILD: 1,
            KinRole.PARENT: 2,
            KinRole.SIBLING: 3,
            KinRole.OTHER_RELATIVE: 4,
        }
        candidates = [
            m for m in self._members.values()
            if m.active and m.member_id != exclude
        ]
        if not candidates:
            self._head_id = ""  # 团已空，等待编排器解散
            return
        ex_head = self._members[exclude] if exclude in self._members else None
        candidates.sort(
            key=lambda m: (
                preference.get(ex_head.kin.get(m.member_id), 9)
                if ex_head is not None else 9,
                list(self._members).index(m.member_id),
            )
        )
        self._head_id = candidates[0].member_id

    def history(self) -> tuple[str, ...]:
        return tuple(self._history)
