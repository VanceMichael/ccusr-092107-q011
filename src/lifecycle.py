"""运营生命周期：咨询、确认、入境、消费、复访。

五个阶段对运营方含义不同，可用动作与披露范围也不同。任何阶段跳转
都必须沿允许的边进行，并记录带时间戳的事件。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class Stage(str, Enum):
    INQUIRY = "inquiry"      # 咨询：询价、占位、能力匹配，未付定金
    CONFIRMED = "confirmed"  # 确认：定金到账、供应商已确认、行程锁定
    ENTERED = "entered"      # 入境：口岸通关、接机履约中
    CONSUMED = "consumed"    # 消费：在途核销、尾款结算
    REVISITED = "revisited"  # 复访：团已结束，留存偏好用于下次组图


# 允许的阶段迁移
TRANSITIONS: dict[Stage, frozenset[Stage]] = {
    Stage.INQUIRY: frozenset({Stage.CONFIRMED}),
    Stage.CONFIRMED: frozenset({Stage.ENTERED}),
    Stage.ENTERED: frozenset({Stage.CONSUMED}),
    Stage.CONSUMED: frozenset({Stage.REVISITED}),
    Stage.REVISITED: frozenset({Stage.INQUIRY}),  # 复访直接发起新咨询
}

# 各阶段默认允许的合作方披露范围（详见 orchestrator 的披露策略）
STAGE_DISCLOSURE: dict[Stage, frozenset[str]] = {
    Stage.INQUIRY: frozenset({"requirements_summary"}),
    Stage.CONFIRMED: frozenset({"requirements_summary", "occupant_codes", "credential_masked"}),
    Stage.ENTERED: frozenset({"requirements_summary", "occupant_codes", "credential_masked", "arrival_window"}),
    Stage.CONSUMED: frozenset({"requirements_summary", "occupant_codes", "redemption_code"}),
    Stage.REVISITED: frozenset({"preferences_only"}),
}


@dataclass(frozen=True)
class StageEvent:
    at: datetime
    frm: Stage
    to: Stage
    note: str = ""


class LifecycleError(ValueError):
    pass


@dataclass
class Lifecycle:
    team_id: str
    stage: Stage = Stage.INQUIRY
    events: list[StageEvent] = field(default_factory=list)

    def advance(self, to: Stage, at: datetime, note: str = "") -> None:
        allowed = TRANSITIONS.get(self.stage, frozenset())
        if to not in allowed:
            raise LifecycleError(
                f"不能从 {self.stage.value} 跳到 {to.value}（团队 {self.team_id}）"
            )
        self.events.append(StageEvent(at=at, frm=self.stage, to=to, note=note))
        self.stage = to

    def has_reached(self, stage: Stage) -> bool:
        order = list(Stage)
        return order.index(self.stage) >= order.index(stage)
