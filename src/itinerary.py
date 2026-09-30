"""带时区的共享行程。

每条行程项（航班、酒店、车辆、景点、当地服务人员）都带：

- 起止时间（UTC 存储）与发生地 IANA 时区；
- zh / ar / en 三语展示文案，由同一份底层结构渲染，保证客人看到的
  多语行程在时间、地点、编号上严格一致；
- 指向预订单元的引用，但展示层不暴露供应商主体信息。

航班延误使用两种策略：SHIFT（后移自身并级联接驳项）与 NOTIFY
（仅通知，不改动时间）。接驳链上的车辆接机随到达时间自动顺延。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum

from zoneinfo import ZoneInfo

from .units import UnitKind


class ItemKind(str, Enum):
    FLIGHT = "flight"
    HOTEL = "hotel"
    VEHICLE = "vehicle"
    ATTRACTION = "attraction"
    LOCAL_STAFF = "local_staff"


UNIT_TO_ITEM: dict[UnitKind, ItemKind] = {
    UnitKind.FLIGHT_SEAT: ItemKind.FLIGHT,
    UnitKind.HOTEL_ROOM: ItemKind.HOTEL,
    UnitKind.CHARTER_VEHICLE: ItemKind.VEHICLE,
    UnitKind.ATTRACTION_SLOT: ItemKind.ATTRACTION,
    UnitKind.LOCAL_STAFF: ItemKind.LOCAL_STAFF,
}


class DelayPolicy(str, Enum):
    SHIFT = "shift"    # 时间后移并沿接驳链级联
    NOTIFY = "notify"  # 只通知客人与服务人员，不改时间


@dataclass(frozen=True)
class LocalizedText:
    zh: str
    ar: str
    en: str

    def get(self, lang: str) -> str:
        return getattr(self, lang)


@dataclass
class ItineraryItem:
    item_id: str
    kind: ItemKind
    start_utc: datetime
    end_utc: datetime
    tz: str                        # 发生地 IANA 时区
    title: LocalizedText
    location: LocalizedText
    unit_id: str
    # 接驳关系：该项排在 after 之后，前者 SHIFT 时本级联顺延
    after: str | None = None
    minimum_gap_min: int = 0       # 与前序项的最小间隔（如入境/取行李）
    delay_policy: DelayPolicy = DelayPolicy.SHIFT
    notes: LocalizedText | None = None
    cancelled: bool = False

    def __post_init__(self) -> None:
        if self.start_utc.tzinfo is None or self.end_utc.tzinfo is None:
            raise ValueError("行程时间必须带时区（UTC）")
        if self.end_utc < self.start_utc:
            raise ValueError("结束时间早于开始时间")
        ZoneInfo(self.tz)  # 校验 IANA 合法

    def local_window(self) -> tuple[datetime, datetime]:
        zone = ZoneInfo(self.tz)
        return self.start_utc.astimezone(zone), self.end_utc.astimezone(zone)

    def shift(self, delta: timedelta) -> None:
        self.start_utc += delta
        self.end_utc += delta

    def render(self, lang: str) -> dict:
        """渲染给客人的单语视图（时间已换算到发生地时区）。"""
        start, end = self.local_window()
        return {
            "item_id": self.item_id,
            "kind": self.kind.value,
            "start": start.strftime("%Y-%m-%d %H:%M"),
            "end": end.strftime("%Y-%m-%d %H:%M"),
            "tz": self.tz,
            "title": self.title.get(lang),
            "location": self.location.get(lang),
            "notes": self.notes.get(lang) if self.notes else "",
            "status": "cancelled" if self.cancelled else "active",
        }


class ItineraryError(ValueError):
    pass


@dataclass
class Itinerary:
    team_id: str
    _items: dict[str, ItineraryItem] = field(default_factory=dict)
    _delay_log: list[str] = field(default_factory=list)

    def add(self, item: ItineraryItem) -> None:
        if item.item_id in self._items:
            raise ItineraryError(f"行程项 {item.item_id} 已存在")
        if item.after is not None and item.after not in self._items:
            raise ItineraryError(f"接驳前序 {item.after} 不存在，请先添加")
        self._items[item.item_id] = item

    def items(self, include_cancelled: bool = False) -> tuple[ItineraryItem, ...]:
        ms = self._items.values()
        if not include_cancelled:
            ms = (i for i in ms if not i.cancelled)
        return tuple(sorted(ms, key=lambda i: i.start_utc))

    def get(self, item_id: str) -> ItineraryItem:
        try:
            return self._items[item_id]
        except KeyError:
            raise ItineraryError(f"行程项 {item_id} 不存在") from None

    # ---- 延误 ----

    def apply_delay(self, item_id: str, delta: timedelta, at: datetime) -> list[str]:
        """航班（或任何 SHIFT 项）延误。

        自身按 delta 后移；沿 ``after`` 链且时间约束会被破坏的接驳项
        一并顺延到满足最小间隔。NOTIFY 项只记日志。
        返回被调整的行程项 ID 列表（不含仅通知项）。
        """
        first = self.get(item_id)
        affected: list[str] = []
        if first.delay_policy is DelayPolicy.NOTIFY:
            self._delay_log.append(
                f"{at.isoformat()} notify-only {item_id} +{delta}"
            )
            return affected

        first.shift(delta)
        affected.append(item_id)
        # 不断扫描，直到所有接驳约束重新满足
        changed = True
        while changed:
            changed = False
            for item in self._items.values():
                if item.after is None or item.cancelled:
                    continue
                prev = self._items[item.after]
                if prev.cancelled:
                    continue
                earliest = prev.end_utc + timedelta(minutes=item.minimum_gap_min)
                if item.start_utc < earliest:
                    slip = earliest - item.start_utc
                    if item.delay_policy is DelayPolicy.NOTIFY:
                        self._delay_log.append(
                            f"{at.isoformat()} notify-only {item.item_id} needs +{slip}"
                        )
                        continue
                    item.shift(slip)
                    affected.append(item.item_id)
                    changed = True
        self._delay_log.append(
            f"{at.isoformat()} delay {item_id} +{delta} -> {affected}"
        )
        return affected

    def cancel_item(self, item_id: str) -> None:
        self.get(item_id).cancelled = True

    def delay_log(self) -> tuple[str, ...]:
        return tuple(self._delay_log)

    # ---- 多语渲染 ----

    def render(self, lang: str) -> list[dict]:
        if lang not in ("zh", "ar", "en"):
            raise ItineraryError(f"不支持的语言：{lang}")
        return [i.render(lang) for i in self.items()]

    def render_all_languages(self) -> dict[str, list[dict]]:
        """三语并出，供测试与对客页面校验一致性。"""
        views = {lang: self.render(lang) for lang in ("zh", "ar", "en")}
        self._assert_consistent(views)
        return views

    @staticmethod
    def _assert_consistent(views: dict[str, list[dict]]) -> None:
        """多语视图的时间/地点编号必须逐项一致，只允许文案不同。"""
        keys = [("item_id",), ("kind",), ("start",), ("end",), ("tz",)]
        n = len(views["zh"])
        for lang in ("ar", "en"):
            if len(views[lang]) != n:
                raise ItineraryError("多语行程条目数量不一致")
            for a, b in zip(views["zh"], views[lang]):
                for (k,) in keys:
                    if a[k] != b[k]:
                        raise ItineraryError(f"多语行程在 {k} 上不一致：{a[k]} != {b[k]}")
