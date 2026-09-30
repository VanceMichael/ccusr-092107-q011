"""带时区的共享行程事件。

每个事件保存时区感知的绝对时间（UTC instant），并记录展示时区：
- 航段两端分别用出发地/到达地区时渲染（DXB 用 +04:00，CKG 用 +08:00）；
- 境内活动统一用 Asia/Shanghai。
多语视图只翻译文案，不改时间，保证各语言客人看到一致行程。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


@dataclass
class Event:
    event_id: str
    category: str
    service_id: str
    supplier_id: str
    title: dict[str, str]
    start: datetime
    end: datetime
    tz: str  # 展示时区
    participants: list[str]  # member_id；团队级事件为全体成员
    lock_id: str | None = None  # 家庭房/包车等不可拆约束
    detail: dict[str, str] = field(default_factory=dict)
    status: str = "scheduled"  # scheduled / delayed / shifted / cancelled / completed
    depends_on: str | None = None
    offset_from_parent: timedelta | None = None
    ends_tz: str | None = None  # 航段到达端展示时区不同

    def instant(self, moment: datetime) -> str:
        return moment.astimezone(timezone.utc).isoformat()

    def render(self, lang: str) -> dict:
        tz = ZoneInfo(self.tz)
        end_tz = ZoneInfo(self.ends_tz) if self.ends_tz else tz
        title = self.title.get(lang, self.title.get("en", self.event_id))
        detail = {k: (v.get(lang, v.get("en", "")) if isinstance(v, dict) else v) for k, v in self.detail.items()}
        return {
            "event_id": self.event_id,
            "category": self.category,
            "service_id": self.service_id,
            "title": title,
            "start_local": self.start.astimezone(tz).strftime("%Y-%m-%d %H:%M"),
            "end_local": self.end.astimezone(end_tz).strftime("%Y-%m-%d %H:%M"),
            "tz": self.tz if not self.ends_tz else f"{self.tz} -> {self.ends_tz}",
            "start_utc": self.instant(self.start),
            "end_utc": self.instant(self.end),
            "status": self.status,
            "lock_id": self.lock_id,
            "detail": detail,
        }


class Itinerary:
    def __init__(self) -> None:
        self.events: dict[str, Event] = {}

    def add(self, event: Event) -> None:
        self.events[event.event_id] = event

    def get(self, event_id: str) -> Event:
        return self.events[event_id]

    def in_order(self) -> list[Event]:
        return sorted(self.events.values(), key=lambda e: e.start)

    def guest_view(self, lang: str) -> list[dict]:
        """客人视图：多语文案 + 当地时间 + UTC 绝对时刻，不含供应商敏感信息。"""
        return [e.render(lang) for e in self.in_order() if e.status != "cancelled"]

    def consistency_fingerprint(self) -> list[tuple[str, str, str]]:
        """跨语言一致性指纹：event_id、起止 UTC、供应商服务，三种语言必须完全相同。"""
        return [(e.event_id, e.instant(e.start), e.instant(e.end)) for e in self.in_order()]
