"""端到端演示：迪拜八口家庭赴重庆团队编排。

运行：python -m examples.end_to_end_demo
数据全部来自 fixtures 中的脱敏示例。
"""

from datetime import datetime, timedelta

from src.fixtures_io import load_city, load_team
from src.itinerary import (
    DelayPolicy,
    ItemKind,
    Itinerary,
    ItineraryItem,
    LocalizedText,
)
from src.disclosure import Partner, PartnerRole
from src.lifecycle import Lifecycle, Stage
from src.money import Currency, DepositLedger, FxQuote, Money
from src.orchestrator import TeamOrchestrator
from pathlib import Path

FIX = Path(__file__).resolve().parent.parent / "fixtures"
D = datetime.fromisoformat


def L(zh, ar, en):
    return LocalizedText(zh=zh, ar=ar, en=en)


def main() -> None:
    team = load_team(FIX / "dubai_family.json")
    city = load_city(FIX / "chongqing_capabilities.json")
    fx = FxQuote(Currency.AED, Currency.CNY, 1.971, D("2026-09-02T06:00:00+00:00"))
    orch = TeamOrchestrator(
        team=team, directory=city, lifecycle=Lifecycle(team.team_id),
        ledger=DepositLedger(team.team_id, fx=fx), itinerary=Itinerary(team.team_id),
    )
    all8 = {m.member_id for m in team.members()}

    print("=" * 64)
    print("阶段 1 · 咨询：按城市能力匹配整套安排")
    print("=" * 64)
    picks = [
        ("U-FLIGHT", "SUP-AIR-01", "DXB-CKG-319"),
        ("U-HOTEL", "SUP-HTL-RIVER", "FAMILY-SUITE-8P"),
        ("U-VAN", "SUP-VAN-01", "MERCEDES-SPRINTER-9"),
        ("U-CABLEWAY", "SUP-ATTR-CABLEWAY", "YANGTZE-CABLEWAY-FAMILY"),
        ("U-GUIDE", "SUP-GUIDE-AR-01", "ARABIC-LEAD-GUIDE"),
        ("U-DINING", "SUP-DINE-HALAL-01", "PRIVATE-HALAL-DINING-8P"),
    ]
    for uid, sup, ref in picks:
        orch.book(uid, city.get(sup, ref), set(all8))
        print(f"  占位 {uid:<12} <- {sup}:{ref}")

    # 大足石刻缺少轮椅能力，被整包拒绝
    try:
        orch.book("U-DAZU", city.get("SUP-ATTR-DAZU", "DAZU-CARVINGS-PRIVATE"), {"M-06"})
    except Exception as exc:
        print(f"  拒绝大足石刻（含轮椅成员）：{exc}")

    print()
    print("=" * 64)
    print("阶段 2 · 确认：多币种定金到账，供应商锁定")
    print("=" * 64)
    orch.pay_deposit("TEAM", Money.of(12000, Currency.AED), D("2026-09-05T00:00:00+00:00"))
    orch.pay_deposit("TEAM", Money.of(8000, Currency.CNY), D("2026-09-05T00:00:00+00:00"))
    orch.confirm(D("2026-09-06T00:00:00+00:00"))
    print("  AED 12,000 + CNY 8,000 定金入账；6 个单元全部确认")

    print()
    print("=" * 64)
    print("成员退出：G-03（孩子）退出，只释放个人席位")
    print("=" * 64)
    settlement = orch.member_exit("M-03", D("2026-10-01T00:00:00+00:00"))
    print(f"  释放个人席位：{settlement.released_slots} 个（航班/索道）")
    print(f"  保持锁定单元：{settlement.kept_locked_units}（家庭房不拆、包车不换小）")
    for e in settlement.refunds:
        print(f"  个人退款：{e.money.format()}")

    print()
    print("=" * 64)
    print("供应商更换：原酒店检修 -> 备用酒店（原子迁移）")
    print("=" * 64)
    new = orch.swap_supplier(
        "U-HOTEL", city.get("SUP-HTL-JIALING", "FAMILY-LOFT-8P"),
        D("2026-09-10T00:00:00+00:00"), note="原酒店检修",
    )
    print(f"  新酒店 {new.supplier_id}，占用 {len(new.occupants)} 人，锁定容量 {new.locked_capacity}")

    print()
    print("=" * 64)
    print("阶段 3 · 入境：航班延误 3 小时，接机自动顺延")
    print("=" * 64)
    flight = ItineraryItem(
        item_id="I-FLIGHT", kind=ItemKind.FLIGHT,
        start_utc=D("2026-10-14T05:20:00+00:00"), end_utc=D("2026-10-14T13:50:00+00:00"),
        tz="Asia/Dubai", title=L("迪拜—重庆直飞", "دبي - تشونغتشينغ", "DXB-CKG direct"),
        location=L("迪拜国际机场", "مطار دبي", "DXB Airport"), unit_id="U-FLIGHT",
    )
    pickup = ItineraryItem(
        item_id="I-PICKUP", kind=ItemKind.VEHICLE,
        start_utc=D("2026-10-14T14:50:00+00:00"), end_utc=D("2026-10-14T15:50:00+00:00"),
        tz="Asia/Shanghai", title=L("接机包车", "استقبال المطار", "Airport pickup"),
        location=L("江北机场", "مطار جيانغبي", "Jiangbei Airport"),
        unit_id="U-VAN", after="I-FLIGHT", minimum_gap_min=60,
    )
    orch.attach_itinerary_item("U-FLIGHT", flight)
    orch.attach_itinerary_item("U-VAN", pickup)
    orch.lifecycle.advance(Stage.ENTERED, D("2026-10-14T13:00:00+00:00"))
    affected = orch.flight_delay("I-FLIGHT", timedelta(hours=3), D("2026-10-14T04:00:00+00:00"))
    print(f"  受影响行程项：{affected}")
    zh = {i["item_id"]: i for i in orch.guest_itinerary("zh")}
    ar = {i["item_id"]: i for i in orch.guest_itinerary("ar")}
    print(f"  接机（中文视图）：{zh['I-PICKUP']['start']} {zh['I-PICKUP']['tz']}")
    print(f"  接机（阿语视图）：{ar['I-PICKUP']['start']}（时间一致，文案本地化）")

    print()
    print("=" * 64)
    print("最小披露：航司 vs 酒店各自能看到什么")
    print("=" * 64)
    airline = Partner("SUP-AIR-01", PartnerRole.AIRLINE, "DXB-CKG-319")
    hotel = Partner("SUP-HTL-JIALING", PartnerRole.HOTEL, "FAMILY-LOFT-8P")
    av = orch.partner_view(airline, arrival_window="2026-10-15 01:20-02:20 CST")
    hv = orch.partner_view(hotel)
    print(f"  航司字段：{sorted(av.keys())}")
    print(f"  酒店字段：{sorted(hv.keys())}（无证件、无到达窗口）")

    print()
    print("=" * 64)
    print("阶段 4/5 · 消费与复访")
    print("=" * 64)
    orch.lifecycle.advance(Stage.CONSUMED, D("2026-10-15T00:00:00+00:00"))
    orch.lifecycle.advance(Stage.REVISITED, D("2026-10-19T00:00:00+00:00"))
    guide = Partner("SUP-GUIDE-AR-01", PartnerRole.LOCAL_SERVICE, "ARABIC-LEAD-GUIDE")
    # 导游随酒店更换已无有效单元（导游仍在）；复访只给偏好
    gv = orch.partner_view(guide, redemption_code="RP-2027-051")
    print(f"  复访阶段导游可见：{sorted(gv.keys())}")
    print(f"  留存偏好：{gv.get('preferences')}")
    print()
    print("当前运营总览：")
    for line in _overview(orch):
        print(f"  {line}")


def _overview(orch):
    ov = orch.operations_overview()
    yield f"阶段={ov['stage']} 在团人数={ov['headcount']} 户主={ov['head']}"
    for u in ov["units"]:
        yield (f"{u['unit_id']} {u['kind']:<16} {u['status']:<9} "
               f"锁定={u['retention']:<12} 保持容量={u['held_capacity']} "
               f"占用={u['occupants']}")
    for cur, amt in ov["balances"].items():
        yield f"定金余额 {amt}"


if __name__ == "__main__":
    main()
