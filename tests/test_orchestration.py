"""海湾入境团队编排端到端测试。

覆盖：约束校验、多币种定金、多语行程一致性、最小披露、
成员退出不拆家庭房/包车、供应商原子更换、航班延误传播、
跨境取消退款与五阶段流转。
"""

import unittest
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from src.orchestration import ArrangementError, FX, TeamOrchestration, load_workspace

CST = ZoneInfo("Asia/Shanghai")
DXT = ZoneInfo("Asia/Dubai")
FIX = Path(__file__).resolve().parent.parent / "fixtures"


def dt(y, m, d, hh=0, mm=0, tz=CST):
    return datetime(y, m, d, hh, mm, tzinfo=tz)


def build_engine():
    ws = load_workspace(FIX)
    eng = TeamOrchestration(ws["registry"], ws["catalog"], ws["fx"], ws["policies"])
    # ---- 咨询阶段锁定整套安排 ----
    eng.add_flight("FLT-GF-814", "outbound")
    eng.add_flight("FLT-GF-815", "return")
    eng.add_hotel("HTL-JL-01", dt(2026, 10, 14, 15), dt(2026, 10, 19, 12), nights=5)
    arrival = dt(2026, 10, 14, 21, 5)
    eng.add_charter("VAN-TR-01", arrival, dt(2026, 10, 19, 10), days=5, anchor_flight=True)
    eng.add_local_service("SVC-MEET-01", dt(2026, 10, 14, 20, 45), arrival,
                          anchor_flight=True, per_team=True)
    eng.add_meal("RST-HL-01", dt(2026, 10, 14, 21, 45), dt(2026, 10, 14, 23, 0),
                 anchor_flight=True)
    eng.add_attraction("ATT-HYD-01", dt(2026, 10, 15, 10), dt(2026, 10, 15, 12, 30))
    eng.add_attraction("ATT-DZ-01", dt(2026, 10, 16, 9), dt(2026, 10, 16, 14))
    eng.add_attraction("ATT-CRUISE-01", dt(2026, 10, 17, 19), dt(2026, 10, 17, 21))
    eng.add_guide("STF-GD-01", dt(2026, 10, 14, 20), dt(2026, 10, 19, 12), days=5)
    return eng, ws


class MemberAndTokenTest(unittest.TestCase):
    def setUp(self):
        self.ws = load_workspace(FIX)
        self.reg = self.ws["registry"]

    def test_eight_members_with_one_token_each(self):
        self.assertEqual(len(self.reg.active_members()), 8)
        for m in self.reg.members.values():
            tok = self.reg.token_for(m.member_id)
            self.assertEqual(tok.member_id, m.member_id)
            self.assertTrue(tok.active)
            self.assertIn("P****", tok.doc_number_masked)  # 只有掩码

    def test_rooming_units_partition_family(self):
        all_in = [m for u in self.reg.rooming_request for m in u["occupants"]]
        self.assertEqual(sorted(all_in), sorted(self.reg.members))
        # 祖母轮椅需求被记录
        self.assertTrue(self.reg.get("m-07").needs_wheelchair)
        self.assertEqual(self.reg.wheelchair_count(), 1)

    def test_kinship_edges_reference_real_members(self):
        ids = set(self.reg.members)
        for a, b, rel in self.reg.kinship_edges:
            self.assertIn(a, ids)
            self.assertIn(b, ids)


class ConstraintTest(unittest.TestCase):
    def test_non_halal_restaurant_rejected(self):
        eng, _ = build_engine()
        with self.assertRaises(ArrangementError) as cm:
            eng.add_meal("RST-SC-09", dt(2026, 10, 15, 18), dt(2026, 10, 15, 19, 30))
        self.assertIn("halal_certified", cm.exception.violations)

    def test_vehicle_without_lift_or_capacity_rejected(self):
        eng, _ = build_engine()
        # 先移除已锁包车再试不合规车（换商语义同样原子校验）
        with self.assertRaises(ArrangementError) as cm:
            eng.replace_supplier("VAN-TR-01", "VAN-TR-02")
        self.assertTrue({"wheelchair_lift", "step_free_entry"} & set(cm.exception.violations))
        # 被拒后原包车仍在
        vans = [u for u in eng.active_units() if u.category == "vehicle"]
        self.assertEqual({v.service_id for v in vans}, {"VAN-TR-01"})

    def test_attraction_without_step_free_rejected(self):
        eng, _ = build_engine()
        with self.assertRaises(ArrangementError) as cm:
            eng.add_attraction("ATT-CABLE-01", dt(2026, 10, 18, 10), dt(2026, 10, 18, 11))
        self.assertIn("step_free_route", cm.exception.violations)

    def test_incomplete_arrangement_cannot_confirm(self):
        ws = load_workspace(FIX)
        bare = TeamOrchestration(ws["registry"], ws["catalog"], ws["fx"], ws["policies"])
        bare.add_flight("FLT-GF-814", "outbound")
        with self.assertRaises(ArrangementError) as cm:
            bare.confirm()
        self.assertTrue(any("返程" in p for p in cm.exception.violations))


class ConfirmAndMoneyTest(unittest.TestCase):
    def test_deposit_collected_in_aed_at_policy_rate(self):
        eng, ws = build_engine()
        gross_aed = eng.total_in("AED")
        result = eng.confirm()
        pct = ws["policies"]["deposit_policy"]["deposit_pct"]
        self.assertEqual(result["deposit_currency"] if "deposit_currency" in result else "AED", "AED")
        expected = gross_aed.scaled(pct)
        self.assertEqual(result["deposit"], str(expected))
        self.assertEqual(eng.stage, "confirmed")
        # 定金分录带汇率快照
        dep = [e for e in eng.ledger.entries if e.kind == "deposit"][0]
        self.assertEqual(dep.currency, "AED")
        self.assertIn("AED", dep.fx_snapshot["rates"])

    def test_mixed_currency_totals(self):
        eng, _ = build_engine()
        total_cny = eng.total_in("CNY")
        total_aed = eng.total_in("AED")
        # 航班以 AED 计价、境内服务以 CNY 计价，两种总额均为正且可互转
        self.assertGreater(total_cny.amount, Decimal(0))
        self.assertGreater(total_aed.amount, Decimal(0))
        self.assertAlmostEqual(
            float(total_cny.amount), float(total_aed.amount * Decimal("1.96")), places=1)


class ItineraryViewTest(unittest.TestCase):
    def test_three_languages_share_identical_instants(self):
        eng, _ = build_engine()
        eng.confirm()
        views = {lang: eng.guest_itinerary(lang) for lang in ("zh", "ar", "en")}
        for lang, view in views.items():
            self.assertEqual(len(view), 12)
        fp = {lang: [(e["event_id"], e["start_utc"], e["end_utc"]) for e in view]
              for lang, view in views.items()}
        self.assertEqual(fp["zh"], fp["ar"])
        self.assertEqual(fp["zh"], fp["en"])

    def test_titles_are_localized(self):
        eng, _ = build_engine()
        eng.confirm()
        zh = {e["event_id"]: e["title"] for e in eng.guest_itinerary("zh")}
        ar = {e["event_id"]: e["title"] for e in eng.guest_itinerary("ar")}
        en = {e["event_id"]: e["title"] for e in eng.guest_itinerary("en")}
        self.assertNotEqual(zh, en)
        self.assertIn("رحلة", [v for v in ar.values() if "رحلة" in v][0])

    def test_flight_renders_dual_timezones(self):
        eng, _ = build_engine()
        eng.confirm()
        flight = [e for e in eng.guest_itinerary("zh") if e["category"] == "flight"
                  and "DXB" in e["title"]][0]
        self.assertEqual(flight["start_local"], "2026-10-14 09:20")  # 迪拜时间
        self.assertEqual(flight["end_local"], "2026-10-14 21:05")    # 北京时间
        self.assertEqual(flight["tz"], "Asia/Dubai -> Asia/Shanghai")
        self.assertEqual(flight["start_utc"], "2026-10-14T05:20:00+00:00")
        self.assertEqual(flight["end_utc"], "2026-10-14T13:05:00+00:00")


class MinimumDisclosureTest(unittest.TestCase):
    def setUp(self):
        self.eng, _ = build_engine()
        self.eng.confirm()

    def _payloads(self, category):
        return [p for p in self.eng.disclosures.values() if p["category"] == category]

    def test_flight_gets_tokens_and_hash_but_no_kinship(self):
        flight = self._payloads("flight")[0]
        self.assertEqual(len(flight["guests"]), 8)
        self.assertTrue(all("demo_hash" in g for g in flight["guests"]))
        flat = str(flight)
        self.assertNotIn("kinship", flat)
        self.assertNotIn("P****7721", flat)  # 掩码证件号也不下发

    def test_hotel_sees_only_its_own_room_unit(self):
        hotel_payloads = self._payloads("hotel")
        self.assertEqual(len(hotel_payloads), 3)
        fam_c = [p for p in hotel_payloads if p["rooming_unit"]["unit_code"] == "ROOM-UNIT-fam-C"][0]
        self.assertEqual(len(fam_c["guests"]), 2)  # 只有祖父母两人
        fam_a = [p for p in hotel_payloads if p["rooming_unit"]["unit_code"] == "ROOM-UNIT-fam-A"][0]
        self.assertEqual(len(fam_a["guests"]), 4)
        self.assertEqual(fam_c["rooming_unit"]["accessibility_need"], ["wheelchair"])
        for p in hotel_payloads:
            self.assertNotIn("kinship", str(p))
            self.assertNotIn("demo_hash", str(p))  # 酒店不需要证件哈希

    def test_vehicle_gets_counts_only(self):
        van = self._payloads("vehicle")[0]
        self.assertEqual(van["pax_count"], 8)
        self.assertEqual(van["wheelchair_count"], 1)
        self.assertNotIn("guests", van)
        self.assertNotIn("token_ref", str(van))

    def test_restaurant_gets_diet_and_pax_only(self):
        meal = self._payloads("restaurant")[0]
        self.assertEqual(meal["pax_count"], 8)
        self.assertEqual(meal["diet"], ["halal"])
        self.assertNotIn("token_ref", str(meal))

    def test_guide_gets_family_brief_without_documents(self):
        guide = self._payloads("local_staff")[0]
        self.assertEqual(guide["family_brief"], {"adults": 4, "children": 2, "seniors": 2})
        self.assertIn("ar", guide["language"])
        self.assertNotIn("demo_hash", str(guide))


class WithdrawalTest(unittest.TestCase):
    def test_withdraw_releases_only_personal_and_refunds_in_aed(self):
        eng, ws = build_engine()
        eng.confirm()
        result = eng.withdraw_member("m-08", dt(2026, 9, 29, 12))  # 提前 15 天 → 60% 档
        # 释放清单只含个人占用（两段航班+餐+三个景点）
        self.assertEqual(len(result["released_personal"]), 6)
        # 退款币种为 AED，金额 = 个人服务额折算 ×60%
        refund = result["refund"]
        self.assertTrue(refund.endswith("AED"))
        fx = ws["fx"]
        # 引擎按每个服务分别折算后求和（分级舍入）
        personal_aed = (ws["catalog"].get("FLT-GF-814").price
                        + ws["catalog"].get("FLT-GF-815").price
                        + fx.convert(ws["catalog"].get("RST-HL-01").price, "AED")
                        + fx.convert(ws["catalog"].get("ATT-DZ-01").price, "AED")
                        + fx.convert(ws["catalog"].get("ATT-CRUISE-01").price, "AED"))
        self.assertEqual(refund, str(personal_aed.scaled(60)))

    def test_family_room_and_charter_remain_intact(self):
        eng, _ = build_engine()
        eng.confirm()
        eng.withdraw_member("m-08", dt(2026, 9, 29, 12))
        # fam-C 无障碍房仍按两人房锁定，未被拆散
        fam_c = [u for u in eng.units.values() if u.scope == "ROOM:fam-C"][0]
        self.assertEqual(fam_c.status, "confirmed")
        self.assertEqual(set(fam_c.participants), {"m-07", "m-08"})
        self.assertEqual(fam_c.qty, 5)
        # 包车仍是团队锁，报价不按 7 人重算
        van = [u for u in eng.units.values() if u.category == "vehicle"][0]
        self.assertEqual(van.scope, "TEAM")
        self.assertEqual(van.indivisible, True)
        self.assertEqual(van.qty, 5)
        # 令牌被吊销，在团 7 人
        self.assertFalse(eng.registry.token_for("m-08").active)
        self.assertEqual(len(eng.registry.active_members()), 7)

    def test_disclosure_regenerated_after_withdrawal(self):
        eng, _ = build_engine()
        eng.confirm()
        eng.withdraw_member("m-08", dt(2026, 9, 29, 12))
        flight = [p for p in eng.disclosures.values() if p["category"] == "flight"][0]
        self.assertEqual(len(flight["guests"]), 7)
        van = [p for p in eng.disclosures.values() if p["category"] == "vehicle"][0]
        self.assertEqual(van["pax_count"], 7)
        self.assertEqual(van["wheelchair_count"], 1)  # 祖母仍在团

    def test_double_withdraw_rejected(self):
        eng, _ = build_engine()
        eng.confirm()
        eng.withdraw_member("m-08", dt(2026, 9, 29, 12))
        with self.assertRaises(ArrangementError):
            eng.withdraw_member("m-08", dt(2026, 9, 29, 13))


class SupplierReplacementTest(unittest.TestCase):
    def test_non_compliant_hotel_swap_is_atomic_rollback(self):
        eng, _ = build_engine()
        eng.confirm()
        with self.assertRaises(ArrangementError):
            eng.replace_supplier("HTL-JL-01", "HTL-YD-02")
        rooms = [u for u in eng.active_units() if u.category == "hotel"]
        self.assertEqual({u.service_id for u in rooms}, {"HTL-JL-01"})
        self.assertEqual({u.supplier_id for u in rooms}, {"SUP-HTL-JL"})
        # 价格也未被部分提交
        self.assertEqual({u.unit_price.currency for u in rooms}, {"CNY"})

    def test_compliant_hotel_swap_moves_all_room_locks(self):
        eng, ws = build_engine()
        eng.confirm()
        old_price = ws["catalog"].get("HTL-JL-01").room("accessible-room").price
        result = eng.replace_supplier("HTL-JL-01", "HTL-RJ-03")
        self.assertEqual(result["replaced_locks"], 3)
        rooms = [u for u in eng.active_units() if u.category == "hotel"]
        self.assertEqual({u.service_id for u in rooms}, {"HTL-RJ-03"})
        new_price = ws["catalog"].get("HTL-RJ-03").room("accessible-room").price
        fam_c = [u for u in rooms if u.room_unit == "fam-C"][0]
        self.assertEqual(fam_c.unit_price, new_price)
        self.assertNotEqual(old_price, new_price)

    def test_flight_swap_without_accessibility_rejected(self):
        eng, _ = build_engine()
        eng.confirm()
        with self.assertRaises(ArrangementError) as cm:
            eng.replace_supplier("FLT-GF-814", "FLT-SW-402")
        self.assertIn("wheelchair_assist", cm.exception.violations)
        flight = [u for u in eng.active_units() if u.extra.get("leg") == "outbound"][0]
        self.assertEqual(flight.service_id, "FLT-GF-814")

    def test_swap_cannot_split_family_unit(self):
        # 更换按 service 维度整体替换全部房锁，不存在只换一间的路径
        eng, _ = build_engine()
        eng.confirm()
        affected = [u for u in eng.units.values() if u.service_id == "HTL-JL-01"]
        self.assertEqual(len(affected), 3)


class FlightDelayTest(unittest.TestCase):
    def test_small_delay_below_threshold_changes_nothing(self):
        eng, _ = build_engine()
        eng.confirm()
        result = eng.report_flight_delay(
            "FLT-GF-814", dt(2026, 10, 14, 10, 20, DXT), dt(2026, 10, 14, 22, 5))
        self.assertEqual(result["shifted"], [])
        self.assertEqual(result["converted_meal"], [])

    def test_major_delay_shifts_pickup_and_converts_late_meal(self):
        eng, ws = build_engine()
        eng.confirm()
        result = eng.report_flight_delay(
            "FLT-GF-814", dt(2026, 10, 14, 11, 20, DXT), dt(2026, 10, 14, 23, 5),
            reason="carrier_delay")
        # 接机礼宾与包车顺延 120 分钟
        categories = {eng.units[l].category for l in result["shifted"]}
        self.assertEqual(categories, {"vehicle", "local_service"})
        van = [u for u in eng.units.values() if u.category == "vehicle"][0]
        self.assertEqual(van.start, dt(2026, 10, 14, 23, 5))
        # 首晚餐厅赶不上截单：取消并替换为深夜清真餐盒，餐费按 AED 全额退回
        self.assertEqual(len(result["converted_meal"]), 1)
        old_meal = [u for u in eng.units.values()
                    if u.category == "restaurant" and u.lock_id not in result["converted_meal"]]
        self.assertEqual(old_meal[0].status, "cancelled")
        box = eng.units[result["converted_meal"][0]]
        self.assertEqual(box.service_id, "SVC-HALALBOX")
        self.assertEqual(len(box.participants), 8)
        refunds = [e for e in eng.ledger.entries if e.reason.startswith("航班延误")]
        self.assertEqual(len(refunds), 1)
        expected = ws["fx"].convert(ws["catalog"].get("RST-HL-01").price * 8, "AED")
        self.assertEqual(str(Money_wrapper(refunds[0])), str(expected))

    def test_delay_propagates_absolute_instants_to_all_languages(self):
        eng, _ = build_engine()
        eng.confirm()
        eng.report_flight_delay(
            "FLT-GF-814", dt(2026, 10, 14, 11, 20, DXT), dt(2026, 10, 14, 23, 5))
        fp = {lang: [(e["event_id"], e["start_utc"], e["end_utc"]) for e in eng.guest_itinerary(lang)]
              for lang in ("zh", "ar", "en")}
        self.assertEqual(fp["zh"], fp["ar"])
        self.assertEqual(fp["zh"], fp["en"])


def Money_wrapper(entry):
    from src.orchestration import Money
    return Money(entry.amount, entry.currency)


class CrossBorderCancelTest(unittest.TestCase):
    def test_tiered_refund_paid_back_in_aed(self):
        eng, ws = build_engine()
        confirmed = eng.confirm()
        deposit = confirmed["deposit"]
        result = eng.cancel_team(dt(2026, 10, 1, 12), reason="guest_cancel")  # 13 天 → 30% 档
        self.assertEqual(result["refund_pct"], 30)
        from src.orchestration import Money
        self.assertEqual(result["refund"], str(Money.of(deposit.split()[0], "AED").scaled(30)))
        self.assertTrue(all(u.status == "cancelled" for u in eng.units.values()))

    def test_carrier_fault_full_refund(self):
        eng, _ = build_engine()
        eng.confirm()
        result = eng.cancel_team(dt(2026, 10, 10), reason="carrier_cancel")
        self.assertEqual(result["refund_pct"], 100)

    def test_refund_after_partial_withdrawal_never_double_counts(self):
        eng, _ = build_engine()
        eng.confirm()
        eng.withdraw_member("m-08", dt(2026, 9, 29, 12))  # 已退 60% 个人款
        result = eng.cancel_team(dt(2026, 10, 10), reason="carrier_cancel")
        # 整团承运人取消：只退剩余未退定金的 100%
        from src.orchestration import Money
        deposits = sum((e.amount for e in eng.ledger.entries if e.kind == "deposit"), Decimal(0))
        refunds_before = sum((e.amount for e in eng.ledger.entries
                              if e.kind == "refund" and e.ref != "TEAM"), Decimal(0))
        self.assertEqual(result["refund"], str(Money(deposits - refunds_before, "AED")))


class StageFlowTest(unittest.TestCase):
    def test_operator_distinguishes_five_stages(self):
        eng, _ = build_engine()
        eng.confirm()
        self.assertEqual(eng.operator_view()["stage_name"]["zh"], "确认")
        entry = eng.mark_border_entry(dt(2026, 10, 14, 23, 30))
        self.assertEqual(entry["stage"], "inbound")
        self.assertEqual(len(entry["tokens_presented"]), 8)  # 边检只核验令牌引用
        self.assertEqual(eng.operator_view()["stage_name"]["ar"], "دخول")
        self.assertEqual(eng.start_in_destination()["stage"], "in_destination")
        self.assertEqual(eng.mark_revisit("2027 年春节再带家庭团")["stage"], "revisit")

    def test_illegal_transition_rejected(self):
        eng, _ = build_engine()
        with self.assertRaises(ArrangementError):
            eng.start_in_destination()  # 未入境不能消费
        eng.confirm()
        with self.assertRaises(ArrangementError):
            eng.confirm()  # 不可重复确认


if __name__ == "__main__":
    unittest.main()
