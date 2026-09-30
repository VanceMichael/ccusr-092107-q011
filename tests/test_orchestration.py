"""编排服务端到端测试。

使用仓库中的脱敏 fixture（重庆城市能力 + 迪拜八口家庭），覆盖：
成员退出不拆散家庭房/包车、多币种定金、航班延误级联、供应商原子
更换、跨境取消结算、合作方最小披露与多语行程一致性。
"""

import unittest
from datetime import datetime, timedelta
from pathlib import Path

from src.capabilities import Capability
from src.disclosure import Partner, PartnerRole
from src.fixtures_io import load_city, load_team
from src.itinerary import (
    DelayPolicy,
    ItemKind,
    Itinerary,
    ItineraryItem,
    LocalizedText,
)
from src.lifecycle import Lifecycle, Stage
from src.money import (
    Currency,
    DepositLedger,
    EntryType,
    FxQuote,
    LedgerEntry,
    Money,
)
from src.orchestrator import TeamOrchestrator
from src.units import Retention, UnitKind, UnitStatus

FIX = Path(__file__).resolve().parent.parent / "fixtures"
D = datetime.fromisoformat


def _lt(zh: str, ar: str, en: str) -> LocalizedText:
    return LocalizedText(zh=zh, ar=ar, en=en)


class FixtureTest(unittest.TestCase):
    def setUp(self) -> None:
        self.team = load_team(FIX / "dubai_family.json")
        self.city = load_city(FIX / "chongqing_capabilities.json")

    def test_team_is_family_of_eight(self):
        self.assertEqual(len(self.team.members()), 8)
        self.assertEqual(self.team.head_id, "M-01")
        # 户主-配偶双向关系
        self.assertEqual(
            self.team.kin_of("M-01")["M-02"].value, "spouse"
        )
        self.assertEqual(
            self.team.kin_of("M-02")["M-01"].value, "spouse"
        )
        # 全员清真，含轮椅与儿童座椅需求
        self.assertIn("halal", [d.value for d in self.team.diet_requirements()])
        access = {a.value for a in self.team.access_requirements()}
        self.assertIn("wheelchair", access)
        self.assertIn("child_seat", access)

    def test_capability_filtering_halal_family(self):
        hotels = self.city.find(
            city="重庆",
            kind=UnitKind.HOTEL_ROOM,
            required=frozenset({
                Capability.FAMILY_ROOM,
                Capability.HALAL_CERTIFIED,
                Capability.WHEELCHAIR_ACCESS,
                Capability.ARABIC_STAFF,
            }),
            min_capacity=8,
        )
        refs = sorted(h.service_ref for h in hotels)
        self.assertEqual(
            refs, ["FAMILY-LOFT-8P", "FAMILY-SUITE-8P"]
        )

    def test_no_real_identity_in_fixtures(self):
        # 脱敏：任何令牌引用都是掩码形式
        for m in self.team.members():
            self.assertIn("*", m.token.masked_ref)
            self.assertNotIn("name", m.public_view())


def build_booked_team() -> TeamOrchestrator:
    """根据 fixture 完成整套预订（咨询阶段）。"""
    team = load_team(FIX / "dubai_family.json")
    city = load_city(FIX / "chongqing_capabilities.json")
    fx = FxQuote(
        base=Currency.AED, quote=Currency.CNY,
        rate=1.971, quoted_at=D("2026-09-02T06:00:00+00:00"),
    )
    orch = TeamOrchestrator(
        team=team,
        directory=city,
        lifecycle=Lifecycle(team.team_id),
        ledger=DepositLedger(team.team_id, fx=fx),
        itinerary=Itinerary(team.team_id),
    )
    all8 = {m.member_id for m in team.members()}

    flight = city.get("SUP-AIR-01", "DXB-CKG-319")
    hotel = city.get("SUP-HTL-RIVER", "FAMILY-SUITE-8P")
    van = city.get("SUP-VAN-01", "MERCEDES-SPRINTER-9")
    cableway = city.get("SUP-ATTR-CABLEWAY", "YANGTZE-CABLEWAY-FAMILY")
    guide = city.get("SUP-GUIDE-AR-01", "ARABIC-LEAD-GUIDE")
    dining = city.get("SUP-DINE-HALAL-01", "PRIVATE-HALAL-DINING-8P")

    orch.book("U-FLIGHT", flight, all8)
    orch.book("U-HOTEL", hotel, all8)
    orch.book("U-VAN", van, all8)
    orch.book("U-CABLEWAY", cableway, all8)
    orch.book("U-GUIDE", guide, all8)
    orch.book("U-DINING", dining, all8)
    return orch


def attach_full_itinerary(orch: TeamOrchestrator) -> None:
    # 航班：2026-10-14 迪拜 09:20(GST) -> 重庆 21:50(CST)
    flight = ItineraryItem(
        item_id="I-FLIGHT", kind=ItemKind.FLIGHT,
        start_utc=D("2026-10-14T05:20:00+00:00"),
        end_utc=D("2026-10-14T13:50:00+00:00"),
        tz="Asia/Dubai",
        title=_lt("迪拜—重庆直飞", "دبي - تشونغتشينغ مباشرة", "Dubai - Chongqing direct"),
        location=_lt("迪拜国际机场", "مطار دبي الدولي", "Dubai International"),
        unit_id="U-FLIGHT",
    )
    orch.attach_itinerary_item("U-FLIGHT", flight)
    pickup = ItineraryItem(
        item_id="I-PICKUP",
        kind=ItemKind.VEHICLE,
        start_utc=D("2026-10-14T14:50:00+00:00"),
        end_utc=D("2026-10-14T15:50:00+00:00"),
        tz="Asia/Shanghai",
        title=_lt("机场接机包车", "استقبال المطار بالحافلة", "Airport pickup charter"),
        location=_lt("江北国际机场", "مطار جيانغبي الدولي", "Jiangbei Int'l Airport"),
        unit_id="U-VAN",
        after="I-FLIGHT",
        minimum_gap_min=60,
    )
    orch.attach_itinerary_item("U-VAN", pickup)
    hotel = ItineraryItem(
        item_id="I-HOTEL",
        kind=ItemKind.HOTEL,
        start_utc=D("2026-10-14T16:30:00+00:00"),
        end_utc=D("2026-10-18T12:00:00+00:00"),
        tz="Asia/Shanghai",
        title=_lt("江景家庭套房入住", "تسجيل دخول الجناح العائلي", "Family suite check-in"),
        location=_lt("渝中半岛", "شبه جزيرة يوزهونغ", "Yuzhong Peninsula"),
        unit_id="U-HOTEL",
        after="I-PICKUP",
        minimum_gap_min=30,
        delay_policy=DelayPolicy.NOTIFY,
    )
    orch.attach_itinerary_item("U-HOTEL", hotel)
    cable = ItineraryItem(
        item_id="I-CABLEWAY",
        kind=ItemKind.ATTRACTION,
        start_utc=D("2026-10-15T06:00:00+00:00"),
        end_utc=D("2026-10-15T07:30:00+00:00"),
        tz="Asia/Shanghai",
        title=_lt("长江索道家庭包舱", "كابينة عائلية لتلفريك اليانغتسي", "Yangtze Cableway family cabin"),
        location=_lt("长江索道", "تلفريك نهر اليانغتسي", "Yangtze Cableway"),
        unit_id="U-CABLEWAY",
        delay_policy=DelayPolicy.NOTIFY,
    )
    orch.attach_itinerary_item("U-CABLEWAY", cable)


class BookingTest(unittest.TestCase):
    def test_full_team_books_with_retention(self):
        orch = build_booked_team()
        by_kind = {u.kind: u for u in orch.units()}
        self.assertEqual(
            by_kind[UnitKind.HOTEL_ROOM].retention, Retention.GROUP_LOCKED
        )
        self.assertEqual(
            by_kind[UnitKind.CHARTER_VEHICLE].retention, Retention.GROUP_LOCKED
        )
        self.assertEqual(
            by_kind[UnitKind.FLIGHT_SEAT].retention, Retention.INDIVIDUAL
        )
        for u in orch.units():
            self.assertEqual(len(u.occupants), 8)

    def test_missing_capability_rejected(self):
        orch = build_booked_team()
        dazu = orch.directory.get("SUP-ATTR-DAZU", "DAZU-CARVINGS-PRIVATE")
        with self.assertRaises(Exception) as cm:
            orch.book("U-DAZU", dazu, {"M-01"})
        self.assertIn("wheelchair_access", str(cm.exception))


class MemberExitTest(unittest.TestCase):
    def test_exit_releases_only_own_seat(self):
        orch = build_booked_team()
        result = orch.member_exit("M-03", D("2026-10-01T00:00:00+00:00"))
        # 释放的只有个人席位：航班 + 索道 = 2
        self.assertEqual(result.released_slots, 2)
        # 家庭房与包车保持原状
        kept = set(result.kept_locked_units)
        self.assertIn("U-HOTEL", kept)
        self.assertIn("U-VAN", kept)
        hotel = next(u for u in orch.units() if u.unit_id == "U-HOTEL")
        van = next(u for u in orch.units() if u.unit_id == "U-VAN")
        self.assertEqual(hotel.held_capacity, 8)  # 不缩房
        self.assertEqual(van.held_capacity, 9)    # 包车容量不变
        self.assertEqual(len(hotel.occupants), 7)
        # 令牌吊销且不可再被查询为在团成员
        self.assertFalse(orch.team.try_get("M-03").active)
        with self.assertRaises(Exception):
            orch.team.get("M-03")

    def test_exit_does_not_split_family_room_when_head_leaves(self):
        orch = build_booked_team()
        orch.member_exit("M-01", D("2026-10-01T00:00:00+00:00"))
        # 户主转移给配偶 M-02
        self.assertEqual(orch.team.head_id, "M-02")
        hotel = next(u for u in orch.units() if u.unit_id == "U-HOTEL")
        self.assertEqual(hotel.held_capacity, 8)
        self.assertEqual(hotel.status, UnitStatus.HELD)
        # 家庭房内仍有 7 名彼此保持关系的家庭成员
        self.assertIn("M-02", hotel.occupants)

    def test_double_exit_rejected(self):
        orch = build_booked_team()
        orch.member_exit("M-03", D("2026-10-01T00:00:00+00:00"))
        with self.assertRaises(Exception):
            orch.member_exit("M-03", D("2026-10-02T00:00:00+00:00"))


class DepositTest(unittest.TestCase):
    def test_multi_currency_deposit_and_exit_refund(self):
        orch = build_booked_team()
        at = D("2026-09-05T00:00:00+00:00")
        # 客人用迪拉姆付定金，尾款人民币（在华消费）
        orch.pay_deposit("U-FLIGHT", Money.of(8000, Currency.AED), at)
        orch.pay_deposit("U-HOTEL", Money.of(6000, Currency.CNY), at)
        self.assertEqual(orch.ledger.balance(Currency.AED), Money.of(8000, Currency.AED))
        self.assertEqual(orch.ledger.balance(Currency.CNY), Money.of(6000, Currency.CNY))

        result = orch.member_exit("M-04", D("2026-10-01T00:00:00+00:00"))
        # 8 口之一退出：退 1/8 个人席位定金（各币种分别退）
        refunded = {e.money.currency: e.money for e in result.refunds}
        self.assertEqual(
            refunded[Currency.AED], Money(8000 * 100 // 8, Currency.AED)
        )
        self.assertEqual(
            refunded[Currency.CNY], Money(6000 * 100 // 8, Currency.CNY)
        )
        # 余额各自减少，未跨币种轧差
        self.assertEqual(
            orch.ledger.balance(Currency.AED), Money.of(7000, Currency.AED)
        )
        self.assertEqual(
            orch.ledger.balance(Currency.CNY), Money.of(5250, Currency.CNY)
        )

    def test_over_refund_blocked(self):
        ledger = DepositLedger("T")
        ledger.post(LedgerEntry(
            "x", EntryType.DEPOSIT, Money.of(100, Currency.AED),
            D("2026-09-01T00:00:00+00:00")))
        ledger.post(LedgerEntry(
            "x", EntryType.REFUND, Money.of(100, Currency.AED),
            D("2026-09-02T00:00:00+00:00")))
        with self.assertRaises(Exception):
            ledger.post(LedgerEntry(
                "x", EntryType.REFUND, Money.of(1, Currency.AED),
                D("2026-10-02T00:00:00+00:00")))
            ledger.balance(Currency.AED)
        with self.assertRaises(Exception):
            ledger.balance(Currency.AED)


class DelayTest(unittest.TestCase):
    def test_flight_delay_cascades_to_pickup_only(self):
        orch = build_booked_team()
        attach_full_itinerary(orch)
        before = orch.itinerary.get("I-PICKUP").start_utc
        # 航班延误 3 小时
        affected = orch.flight_delay(
            "I-FLIGHT", timedelta(hours=3), D("2026-10-14T04:00:00+00:00")
        )
        self.assertIn("I-PICKUP", affected)
        after = orch.itinerary.get("I-PICKUP").start_utc
        self.assertEqual(after - before, timedelta(hours=3))
        # 酒店是 NOTIFY：时间不变，只记日志
        hotel_start = orch.itinerary.get("I-HOTEL").start_utc
        self.assertEqual(
            hotel_start,
            D("2026-10-14T16:30:00+00:00"),
        )
        self.assertTrue(any("notify-only" in line for line in orch.itinerary.delay_log()))

    def test_timezone_rendering(self):
        orch = build_booked_team()
        attach_full_itinerary(orch)
        zh = orch.guest_itinerary("zh")[0]
        # 05:20 UTC = 09:20 迪拜
        self.assertTrue(zh["start"].startswith("2026-10-14 09:20"))
        self.assertEqual(zh["tz"], "Asia/Dubai")
        pickup = next(i for i in orch.guest_itinerary("zh") if i["item_id"] == "I-PICKUP")
        # 14:50 UTC = 22:50 重庆（UTC+8）
        self.assertTrue(pickup["start"].endswith("22:50"))


class SupplierSwapTest(unittest.TestCase):
    def test_atomic_swap_kept_whole_family_lock(self):
        orch = build_booked_team()
        backup = orch.directory.get("SUP-HTL-JIALING", "FAMILY-LOFT-8P")
        new = orch.swap_supplier(
            "U-HOTEL", backup, D("2026-09-10T00:00:00+00:00"),
            note="原酒店检修",
        )
        self.assertEqual(new.supplier_id, "SUP-HTL-JIALING")
        self.assertEqual(len(new.occupants), 8)
        self.assertEqual(new.locked_capacity, 8)  # 整户锁定随换迁移
        self.assertEqual(new.retention, Retention.GROUP_LOCKED)

    def test_swap_rejected_when_capability_weaker(self):
        # 备用车辆若无阿语司机则不应能换（用临时弱能力选项模拟）
        from src.capabilities import ServiceOption
        orch = build_booked_team()
        weak = ServiceOption(
            supplier_id="SUP-VAN-X", service_ref="PLAIN-VAN",
            kind=UnitKind.CHARTER_VEHICLE, city="重庆",
            timezone="Asia/Shanghai", capacity=20,
            capabilities=frozenset({
                Capability.WHEELCHAIR_ACCESS, Capability.CHILD_SEAT,
                Capability.LARGE_LUGGAGE, Capability.AIRPORT_PICKUP,
            }),  # 缺 arabic_staff
            display={"zh": "x", "ar": "x", "en": "x"},
        )
        with self.assertRaises(Exception):
            orch.swap_supplier("U-VAN", weak, D("2026-09-10T00:00:00+00:00"))
        # 原包车完好无损（原子性：失败不留中间态）
        van = next(u for u in orch.units() if u.unit_id == "U-VAN")
        self.assertEqual(van.supplier_id, "SUP-VAN-01")
        self.assertEqual(van.status, UnitStatus.HELD)
        self.assertEqual(len(van.occupants), 8)

    def test_swap_forbidden_after_entry(self):
        orch = build_booked_team()
        orch.pay_deposit("U-FLIGHT", Money.of(5000, Currency.AED), D("2026-09-05T00:00:00+00:00"))
        orch.confirm(D("2026-09-06T00:00:00+00:00"))
        orch.lifecycle.advance(Stage.ENTERED, D("2026-10-14T14:00:00+00:00"))
        backup = orch.directory.get("SUP-VAN-02", "MAXUS-G10-9")
        with self.assertRaises(Exception):
            orch.swap_supplier("U-VAN", backup, D("2026-10-14T15:00:00+00:00"))


class CrossBorderCancelTest(unittest.TestCase):
    def test_free_cancel_refunds_all(self):
        orch = build_booked_team()
        at = D("2026-09-05T00:00:00+00:00")
        orch.pay_deposit("TEAM", Money.of(10000, Currency.AED), at)
        # 所有单元都在免费时限之前取消
        detail = orch.cross_border_cancel(D("2026-09-06T00:00:00+00:00"))
        self.assertTrue(all(v["free_cancel"] for v in detail.values()))
        self.assertEqual(orch.ledger.balance(Currency.AED), Money(0, Currency.AED))
        self.assertTrue(all(u.status is UnitStatus.CANCELLED for u in orch.units()))

    def test_late_cancel_forfeits_half(self):
        orch = build_booked_team()
        orch.pay_deposit("TEAM", Money.of(10000, Currency.AED), D("2026-09-05T00:00:00+00:00"))
        deadlines = {
            "U-FLIGHT": D("2026-10-10T00:00:00+00:00"),
            "U-HOTEL": D("2026-10-10T00:00:00+00:00"),
            "U-VAN": D("2026-10-10T00:00:00+00:00"),
            "U-CABLEWAY": D("2026-10-10T00:00:00+00:00"),
            "U-GUIDE": D("2026-10-10T00:00:00+00:00"),
            "U-DINING": D("2026-10-10T00:00:00+00:00"),
        }
        orch.cross_border_cancel(D("2026-10-12T00:00:00+00:00"), deadlines)
        # 没收 50% 后退清余额
        self.assertEqual(orch.ledger.balance(Currency.AED), Money(0, Currency.AED))
        forfeits = [e for e in orch.ledger.entries() if e.entry_type is EntryType.FORFEIT]
        self.assertEqual(forfeits[0].money, Money.of(5000, Currency.AED))


class DisclosureTest(unittest.TestCase):
    def test_inquiry_reveals_requirements_only(self):
        orch = build_booked_team()
        hotel = Partner("SUP-HTL-RIVER", PartnerRole.HOTEL, "FAMILY-SUITE-8P")
        view = orch.partner_view(hotel)
        self.assertIn("requirements", view)
        self.assertNotIn("occupants", view)
        self.assertNotIn("credentials_masked", view)

    def test_confirmed_view_masked_and_scoped(self):
        orch = build_booked_team()
        orch.pay_deposit("U-FLIGHT", Money.of(1000, Currency.AED), D("2026-09-05T00:00:00+00:00"))
        orch.confirm(D("2026-09-06T00:00:00+00:00"))
        hotel = Partner("SUP-HTL-RIVER", PartnerRole.HOTEL, "FAMILY-SUITE-8P")
        view = orch.partner_view(hotel)
        self.assertEqual(len(view["occupants"]), 8)
        # 酒店不需要证件，确认阶段默认也不下发证件指纹
        self.assertNotIn("credentials_masked", view)
        airline = Partner("SUP-AIR-01", PartnerRole.AIRLINE, "DXB-CKG-319")
        aview = orch.partner_view(airline)
        for c in aview["credentials_masked"]:
            self.assertIn("*", c["credential"])
            self.assertNotIn("name", c)

    def test_partner_cannot_see_other_units(self):
        orch = build_booked_team()
        outsider = Partner("SUP-ATTR-DAZU", PartnerRole.ATTRACTION, "DAZU-CARVINGS-PRIVATE")
        with self.assertRaises(Exception):
            orch.partner_view(outsider)

    def test_no_cross_member_leakage_after_exit(self):
        orch = build_booked_team()
        orch.pay_deposit("U-FLIGHT", Money.of(1000, Currency.AED), D("2026-09-05T00:00:00+00:00"))
        orch.confirm(D("2026-09-06T00:00:00+00:00"))
        orch.member_exit("M-03", D("2026-10-01T00:00:00+00:00"))
        airline = Partner("SUP-AIR-01", PartnerRole.AIRLINE, "DXB-CKG-319")
        view = orch.partner_view(airline)
        self.assertNotIn("G-03", view["occupants"])


class LifecycleTest(unittest.TestCase):
    def test_stage_order_enforced(self):
        lc = Lifecycle("T")
        with self.assertRaises(Exception):
            lc.advance(Stage.ENTERED, D("2026-10-14T00:00:00+00:00"))

    def test_full_journey_stages(self):
        orch = build_booked_team()
        orch.pay_deposit("TEAM", Money.of(2000, Currency.AED), D("2026-09-05T00:00:00+00:00"))
        orch.confirm(D("2026-09-06T00:00:00+00:00"))
        orch.lifecycle.advance(Stage.ENTERED, D("2026-10-14T14:00:00+00:00"))
        orch.lifecycle.advance(Stage.CONSUMED, D("2026-10-18T00:00:00+00:00"))
        orch.lifecycle.advance(Stage.REVISITED, D("2026-10-20T00:00:00+00:00"))
        self.assertEqual(orch.lifecycle.stage, Stage.REVISITED)
        guide = Partner("SUP-GUIDE-AR-01", PartnerRole.LOCAL_SERVICE, "ARABIC-LEAD-GUIDE")
        view = orch.partner_view(guide, redemption_code="RPL-2027")
        # 复访阶段只给偏好，不再给占用成员
        self.assertIn("preferences", view)
        self.assertNotIn("occupants", view)

    def test_confirm_requires_deposit(self):
        orch = build_booked_team()
        with self.assertRaises(Exception):
            orch.confirm(D("2026-09-06T00:00:00+00:00"))


class MultiLanguageTest(unittest.TestCase):
    def test_three_languages_consistent(self):
        orch = build_booked_team()
        attach_full_itinerary(orch)
        views = orch.itinerary.render_all_languages()
        self.assertEqual({k for k in views}, {"zh", "ar", "en"})
        for zh, ar, en in zip(views["zh"], views["ar"], views["en"]):
            self.assertEqual(zh["item_id"], ar["item_id"])
            self.assertEqual(zh["item_id"], en["item_id"])
            self.assertEqual(zh["start"], ar["start"])
            self.assertEqual(zh["start"], en["start"])
            self.assertNotEqual(zh["title"], en["title"])  # 文案本地化
            self.assertNotEqual(zh["title"], ar["title"])

    def test_every_supplier_display_has_three_locales(self):
        city = load_city(FIX / "chongqing_capabilities.json")
        for ref in city.find(city="重庆", kind=UnitKind.HOTEL_ROOM, required=frozenset(), min_capacity=0):
            self.assertEqual(set(ref.display), {"zh", "ar", "en"})


if __name__ == "__main__":
    unittest.main()
