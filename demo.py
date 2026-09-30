"""八口之家迪拜→重庆独立团编排演示。

运行：python3 demo.py
展示：咨询锁定 → 多币种定金确认 → 合作方最小披露 → 成员退出不拆家庭约束
      → 航班延误传播 → 五阶段推进 → 客人多语行程与运营视图。
"""

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from src.orchestration import ArrangementError, TeamOrchestration, load_workspace

CST = ZoneInfo("Asia/Shanghai")
DXT = ZoneInfo("Asia/Dubai")


def at(y, m, d, hh=0, mm=0, tz=CST):
    return datetime(y, m, d, hh, mm, tzinfo=tz)


def main() -> None:
    ws = load_workspace(Path(__file__).parent / "fixtures")
    eng = TeamOrchestration(ws["registry"], ws["catalog"], ws["fx"], ws["policies"])

    # ---- 咨询：运营人员逐项锁定，并在锁定点即时校验五类约束 ----
    eng.add_flight("FLT-GF-814", "outbound")
    eng.add_flight("FLT-GF-815", "return")
    eng.add_hotel("HTL-JL-01", at(2026, 10, 14, 23, 30), at(2026, 10, 19, 12), nights=5)
    arrival = at(2026, 10, 14, 21, 5)
    eng.add_charter("VAN-TR-01", arrival, at(2026, 10, 19, 10), days=5, anchor_flight=True)
    eng.add_local_service("SVC-MEET-01", at(2026, 10, 14, 20, 45), arrival,
                          anchor_flight=True, per_team=True)
    eng.add_meal("RST-HL-01", at(2026, 10, 14, 21, 45), at(2026, 10, 14, 23, 0),
                 anchor_flight=True)
    eng.add_attraction("ATT-HYD-01", at(2026, 10, 15, 10), at(2026, 10, 15, 12, 30))
    eng.add_attraction("ATT-DZ-01", at(2026, 10, 16, 9), at(2026, 10, 16, 14))
    eng.add_attraction("ATT-CRUISE-01", at(2026, 10, 17, 19), at(2026, 10, 17, 21))
    eng.add_guide("STF-GD-01", at(2026, 10, 14, 20), at(2026, 10, 19, 12), days=5)

    print("== 咨询阶段：总报价 ==")
    print(" CNY:", eng.total_in("CNY"), " AED:", eng.total_in("AED"))
    confirmed = eng.confirm()
    print("== 确认：定金（统一 AED，冻结汇率）==", confirmed["deposit"])

    print("\n== 合作方最小披露（航班 vs 包车）==")
    flight_doc = next(p for p in eng.disclosures.values() if p["category"] == "flight")
    van_doc = next(p for p in eng.disclosures.values() if p["category"] == "vehicle")
    print(" 航班拿到：", sorted(flight_doc["guests"][0].keys()), "（无证件号/亲属）")
    print(" 包车拿到：", {k: v for k, v in van_doc.items() if k not in ("booking_ref", "supplier_id")})

    print("\n== 供应商更换：非清真友好酒店被原子拒绝 ==")
    try:
        eng.replace_supplier("HTL-JL-01", "HTL-YD-02")
    except ArrangementError as exc:
        print(" 拒绝原因：", exc.violations, "——原酒店三个房锁全部保留")
    eng.replace_supplier("HTL-JL-01", "HTL-RJ-03")
    print(" 合规替代：三间房（家庭套房/双床/无障碍房）整体迁至融景Halal服务公寓")

    print("\n== 成员退出：祖父 m-08 提前 15 天取消 ==")
    out = eng.withdraw_member("m-08", at(2026, 9, 29, 12))
    print(" 释放个人占用：", len(out["released_personal"]), "项；退款", out["refund"])
    print(" 家庭房/包车/向导等", len(out["family_locks_intact"]), "个团队锁原样保留")

    print("\n== 航班延误 120 分钟 ==")
    delay = eng.report_flight_delay(
        "FLT-GF-814", at(2026, 10, 14, 11, 20, tz=DXT), at(2026, 10, 14, 23, 5))
    print(" 顺延：", delay["shifted"], "；首晚餐厅改清真餐盒：", delay["converted_meal"])

    print("\n== 阶段推进 ==")
    entry = eng.mark_border_entry(at(2026, 10, 14, 23, 30))
    print(" 入境核验令牌：", entry["tokens_presented"])
    eng.start_in_destination()
    eng.mark_revisit("2027 年春节再带家庭团")

    print("\n== 客人行程（阿语 / 中文首两条）==")
    for lang in ("ar", "zh"):
        print(f" [{lang}]")
        for ev in eng.guest_itinerary(lang)[:3]:
            print(f"   {ev['start_local']} ({ev['tz']})  {ev['title']}  [{ev['status']}]")

    op = eng.operator_view()
    print("\n== 运营视图 ==")
    print(" 阶段：", op["stage_name"]["zh"], "/", op["stage_name"]["ar"], "/", op["stage_name"]["en"])
    print(" 在团人数：", op["active_pax"], "；账务：", op["money"]["ledger_totals"])


if __name__ == "__main__":
    main()
