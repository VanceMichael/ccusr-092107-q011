"""城市服务能力目录。

目录回答两个问题：
1. 某项服务是否满足团队约束（清真、无障碍、阿拉伯语、升降踏板……）；
2. 在给定占用数量下是否仍有容量。

目录是只读的供应侧事实；占用与锁定由编排引擎管理。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .money import Money


@dataclass
class RoomType:
    room_type: str
    inventory: int
    occupancy: int
    features: list[str]
    price: Money


@dataclass
class ServiceDef:
    service_id: str
    category: str
    supplier_id: str
    supplier_name: dict[str, str]
    name: dict[str, str]
    features: list[str] = field(default_factory=list)
    price: Money | None = None
    seats_available: int | None = None
    capacity: int | None = None
    charter_scope: str | None = None
    room_types: dict[str, RoomType] = field(default_factory=dict)
    departs_at: datetime | None = None
    arrives_at: datetime | None = None
    raw: dict = field(default_factory=dict)

    def has(self, feature: str) -> bool:
        return feature in self.features

    def room(self, room_type: str) -> RoomType:
        if room_type not in self.room_types:
            raise KeyError(f"{self.service_id} 无房型 {room_type}")
        return self.room_types[room_type]

    def title(self, lang: str) -> str:
        return self.name.get(lang, self.name.get("en", self.service_id))

    def supplier_title(self, lang: str) -> str:
        return self.supplier_name.get(lang, self.supplier_name.get("en", self.supplier_id))


class ServiceCatalog:
    CATEGORIES = {"flight", "hotel", "vehicle", "restaurant", "attraction", "local_staff", "local_service"}

    def __init__(self, data: dict):
        self.city = data["city"]
        self.timezone = data["timezone"]
        self.airport = data.get("airport", {})
        self.services: dict[str, ServiceDef] = {}
        for raw in data["services"]:
            price = Money.of(raw["price"]["amount"], raw["price"]["currency"]) if raw.get("price") else None
            rooms: dict[str, RoomType] = {}
            for r in raw.get("room_types", []):
                rooms[r["room_type"]] = RoomType(
                    room_type=r["room_type"],
                    inventory=r["inventory"],
                    occupancy=r["occupancy"],
                    features=r.get("features", []),
                    price=Money.of(r["price"]["amount"], r["price"]["currency"]),
                )
            svc = ServiceDef(
                service_id=raw["service_id"],
                category=raw["category"],
                supplier_id=raw["supplier_id"],
                supplier_name=raw["supplier_name"],
                name=raw["name"],
                features=raw.get("features", []),
                price=price,
                seats_available=raw.get("seats_available"),
                capacity=raw.get("capacity"),
                charter_scope=raw.get("charter_scope"),
                room_types=rooms,
                departs_at=datetime.fromisoformat(raw["departs_at"]) if raw.get("departs_at") else None,
                arrives_at=datetime.fromisoformat(raw["arrives_at"]) if raw.get("arrives_at") else None,
                raw=raw,
            )
            self.services[svc.service_id] = svc

    def get(self, service_id: str) -> ServiceDef:
        if service_id not in self.services:
            raise KeyError(f"未知服务：{service_id}")
        return self.services[service_id]

    def by_category(self, category: str) -> list[ServiceDef]:
        return [s for s in self.services.values() if s.category == category]

    def missing_features(self, service_id: str, required: list[str]) -> list[str]:
        """返回服务不具备的特性；空列表表示满足。"""
        svc = self.get(service_id)
        return [f for f in required if f not in svc.features]

    def room_missing_features(self, service_id: str, room_type: str, required: list[str]) -> list[str]:
        svc = self.get(service_id)
        room = svc.room(room_type)
        union = set(svc.features) | set(room.features)
        return [f for f in required if f not in union]
