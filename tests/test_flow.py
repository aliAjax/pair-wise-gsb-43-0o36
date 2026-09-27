import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import DomainError, ProcurementService  # noqa: E402


class ProcurementFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = ProcurementService(Path(self.tmp.name) / "test.db")
        self.vendor1 = self.service.create_vendor("proc1", "procurement", "V-001", "启明科技", "vendor1")
        self.vendor2 = self.service.create_vendor("proc1", "procurement", "V-002", "远山系统", "vendor2")
        criteria = [
            {"name": "报价", "weight": 60, "kind": "cost", "max_value": 1000000},
            {"name": "质量", "weight": 40, "kind": "direct", "max_value": 100},
        ]
        self.tender = self.service.create_tender(
            "proc1", "procurement", "T-001", "数据中心设备", (datetime.now(timezone.utc) + timedelta(seconds=2)).isoformat(), criteria
        )
        self.tender = self.service.publish_tender("proc1", "procurement", self.tender["id"], self.tender["version"])

    def tearDown(self):
        self.tmp.cleanup()

    def bid(self, vendor, actor, number, price, quality):
        return self.service.submit_bid(actor, "vendor", self.tender["id"], vendor["id"], {"报价": price, "质量": quality}, price)

    def open_and_evaluate(self):
        time.sleep(2.1)
        opened = self.service.open_bids("proc1", "procurement", self.tender["id"], self.tender["version"])
        self.service.evaluate_bid("eval1", "evaluator", opened["bids"][0]["id"], {"报价": 800000, "质量": 90})
        self.service.evaluate_bid("eval2", "evaluator", opened["bids"][0]["id"], {"报价": 800000, "质量": 90})
        self.service.evaluate_bid("eval1", "evaluator", opened["bids"][1]["id"], {"报价": 700000, "质量": 80})
        return opened

    def publish_notice(self, seconds=1):
        current = self.service.get_tender("sup1", "supervisor", self.tender["id"])["tender"]
        deadline = (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()
        return self.service.publish_award_notice("sup1", "supervisor", self.tender["id"], deadline, current["version"])

    def test_complete_sealed_bid_open_evaluate_notice_and_award_flow(self):
        self.bid(self.vendor1, "vendor1", "B1", 800000, 90)
        self.bid(self.vendor2, "vendor2", "B2", 700000, 80)
        before = self.service.get_tender("vendor1", "vendor", self.tender["id"])
        self.assertEqual("sealed", before["bids"][0]["status"])
        self.assertNotIn("payload", before["bids"][0])
        opened = self.open_and_evaluate()

        # 监督员把当前排名冻结成公示并写明截止时间
        notice_result = self.publish_notice(seconds=1)
        self.assertEqual("notice", notice_result["tender"]["status"])
        notice = notice_result["notice"]
        self.assertEqual("active", notice["status"])
        self.assertEqual(opened["bids"][0]["id"], notice["candidates"][0]["bid_id"])

        # 同一评审轮次只保留一条有效记录
        current = self.service.get_tender("sup1", "supervisor", self.tender["id"])["tender"]
        with self.assertRaises(DomainError) as dup:
            self.service.publish_award_notice(
                "sup1", "supervisor", self.tender["id"],
                (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(), current["version"])
        self.assertEqual(409, dup.exception.status)

        # 公开页公示期显示候选顺序和截止时间，不展示评分与报价
        public = self.service.state("", "public")["notices"][0]
        self.assertEqual(self.tender["id"], public["tender_id"])
        self.assertIn("deadline", public)
        self.assertEqual(["启明科技", "远山系统"], [c["vendor_name"] for c in public["candidates"]])
        for candidate in public["candidates"]:
            self.assertNotIn("score", candidate)
            self.assertNotIn("price", candidate)

        # 公示期暂停授标
        current = self.service.get_tender("sup1", "supervisor", self.tender["id"])["tender"]
        with self.assertRaises(DomainError) as blocked:
            self.service.award_tender("sup1", "supervisor", self.tender["id"], current["version"])
        self.assertEqual(409, blocked.exception.status)

        time.sleep(1.1)
        # 截止时没有未处理异议，监督员按公示排名授标
        current = self.service.get_tender("sup1", "supervisor", self.tender["id"])["tender"]
        award = self.service.award_tender("sup1", "supervisor", self.tender["id"], current["version"])
        self.assertEqual("awarded", award["tender"]["status"])
        self.assertEqual(opened["bids"][0]["id"], award["award"]["winner"]["bid_id"])

    def test_notice_objection_accepted_voids_notice_and_restarts_evaluation(self):
        self.bid(self.vendor1, "vendor1", "B1", 800000, 90)
        self.bid(self.vendor2, "vendor2", "B2", 700000, 80)
        self.open_and_evaluate()
        notice_result = self.publish_notice(seconds=3600)
        notice_id = notice_result["notice"]["id"]

        # 供应商在公示期提出异议
        objection = self.service.submit_objection("vendor2", "vendor", self.tender["id"], "候选排名计算有误")
        self.assertEqual(notice_id, objection["notice_id"])

        # 非供应商角色不能提异议
        with self.assertRaises(DomainError) as forbidden:
            self.service.submit_objection("eval1", "evaluator", self.tender["id"], "评审人不能提异议")
        self.assertEqual(403, forbidden.exception.status)

        # 受理异议：公示失效并重新评审
        resolved = self.service.resolve_objection("sup1", "supervisor", objection["id"], "accepted", "重新核算得分")
        self.assertEqual("accepted", resolved["status"])
        updated = self.service.get_tender("sup1", "supervisor", self.tender["id"])
        self.assertEqual("reevaluation", updated["tender"]["status"])
        self.assertEqual(2, updated["tender"]["evaluation_round"])
        self.assertEqual("void", updated["notice"]["status"])

        # 失效公示存在未处理记录也不影响新一轮；新一轮可重新评分并再次公示
        self.service.evaluate_bid("eval1", "evaluator",
                                  next(b["id"] for b in updated["bids"] if b["submitted_by"] == "vendor1"),
                                  {"报价": 800000, "质量": 70})
        self.service.evaluate_bid("eval2", "evaluator",
                                  next(b["id"] for b in updated["bids"] if b["submitted_by"] == "vendor1"),
                                  {"报价": 800000, "质量": 70})
        self.service.evaluate_bid("eval1", "evaluator",
                                  next(b["id"] for b in updated["bids"] if b["submitted_by"] == "vendor2"),
                                  {"报价": 700000, "质量": 95})
        new_notice = self.publish_notice(seconds=1)
        self.assertEqual(2, new_notice["notice"]["evaluation_round"])
        # 内部角色仍能看到历史失效公示记录
        notices = [n for n in self.service.state("sup1", "supervisor")["notices"] if n["tender_id"] == self.tender["id"]]
        self.assertEqual({"void", "active"}, {n["status"] for n in notices})

    def test_frozen_notice_ranking_cannot_be_rewritten_by_later_scores(self):
        self.bid(self.vendor1, "vendor1", "B1", 800000, 90)
        self.bid(self.vendor2, "vendor2", "B2", 700000, 80)
        opened = self.open_and_evaluate()
        self.publish_notice(seconds=1)

        # 公示期不能评分、不能废标
        detail = self.service.get_tender("sup1", "supervisor", self.tender["id"])
        with self.assertRaises(DomainError) as no_eval:
            self.service.evaluate_bid("eval1", "evaluator", opened["bids"][0]["id"], {"报价": 1, "质量": 1})
        self.assertEqual(409, no_eval.exception.status)
        winning_bid = next(b for b in detail["bids"] if b["id"] == opened["bids"][0]["id"])
        with self.assertRaises(DomainError) as no_disq:
            self.service.disqualify_bid("sup1", "supervisor", winning_bid["id"], "试图改结果", winning_bid["version"])
        self.assertEqual(409, no_disq.exception.status)

        time.sleep(1.1)
        current = self.service.get_tender("sup1", "supervisor", self.tender["id"])["tender"]
        award = self.service.award_tender("sup1", "supervisor", self.tender["id"], current["version"])
        # 授标赢家来自冻结快照
        self.assertEqual(opened["bids"][0]["id"], award["award"]["winner"]["bid_id"])

    def test_objection_rejected_keeps_notice_and_blocks_late_award(self):
        self.bid(self.vendor1, "vendor1", "B1", 800000, 90)
        self.bid(self.vendor2, "vendor2", "B2", 700000, 80)
        self.open_and_evaluate()
        self.publish_notice(seconds=1)
        objection = self.service.submit_objection("vendor1", "vendor", self.tender["id"], "程序性疑问")
        # 截止时仍有未处理异议，不能授标
        time.sleep(1.1)
        # 公示截止后不能再提异议
        with self.assertRaises(DomainError) as late:
            self.service.submit_objection("vendor2", "vendor", self.tender["id"], "截止后才提出异议")
        self.assertEqual(409, late.exception.status)
        current = self.service.get_tender("sup1", "supervisor", self.tender["id"])["tender"]
        with self.assertRaises(DomainError) as blocked:
            self.service.award_tender("sup1", "supervisor", self.tender["id"], current["version"])
        self.assertEqual(409, blocked.exception.status)
        # 驳回异议后公示仍有效，可按原公示排名授标
        self.service.resolve_objection("sup1", "supervisor", objection["id"], "rejected", "程序合规")
        detail = self.service.get_tender("sup1", "supervisor", self.tender["id"])
        self.assertEqual("active", detail["notice"]["status"])
        award = self.service.award_tender("sup1", "supervisor", self.tender["id"], detail["tender"]["version"])
        self.assertEqual("awarded", award["tender"]["status"])

    def test_conflict_and_duplicate_evaluation_are_rejected(self):
        bid = self.bid(self.vendor1, "vendor1", "B3", 800000, 90)
        time.sleep(2.1)
        self.service.open_bids("proc1", "procurement", self.tender["id"], self.tender["version"])
        self.service.declare_conflict("eval1", "evaluator", self.tender["id"], "eval1", self.vendor1["id"], "曾受雇于供应商")
        with self.assertRaises(DomainError) as ctx:
            self.service.evaluate_bid("eval1", "evaluator", bid["id"], {"报价": 800000, "质量": 90})
        self.assertEqual(403, ctx.exception.status)
        self.service.evaluate_bid("eval2", "evaluator", bid["id"], {"报价": 800000, "质量": 90})
        with self.assertRaises(DomainError) as ctx2:
            self.service.evaluate_bid("eval2", "evaluator", bid["id"], {"报价": 800000, "质量": 90})
        self.assertEqual(409, ctx2.exception.status)

    def test_complaint_reevaluation_award_block_and_permissions(self):
        bid = self.bid(self.vendor1, "vendor1", "B4", 800000, 90)
        time.sleep(2.1)
        self.service.open_bids("proc1", "procurement", self.tender["id"], self.tender["version"])
        self.service.evaluate_bid("eval1", "evaluator", bid["id"], {"报价": 800000, "质量": 90})
        complaint = self.service.submit_complaint("vendor1", "vendor", self.tender["id"], "评分标准理解有误")
        current = self.service.get_tender("sup1", "supervisor", self.tender["id"])
        with self.assertRaises(DomainError):
            self.service.award_tender("sup1", "supervisor", self.tender["id"], current["tender"]["version"])
        resolved = self.service.resolve_complaint("sup1", "supervisor", complaint["id"], "accepted", "按新规则重评")
        self.assertEqual("accepted", resolved["status"])
        updated = self.service.get_tender("sup1", "supervisor", self.tender["id"])["tender"]
        self.assertEqual("reevaluation", updated["status"])
        self.assertEqual(2, updated["evaluation_round"])
        with self.assertRaises(DomainError) as ctx:
            self.service.open_bids("vendor1", "vendor", self.tender["id"], updated["version"])
        self.assertEqual(403, ctx.exception.status)
        # 无公示时即使重新评分完成也不能直接授标
        self.service.evaluate_bid("eval1", "evaluator", bid["id"], {"报价": 800000, "质量": 88})
        current = self.service.get_tender("sup1", "supervisor", self.tender["id"])["tender"]
        with self.assertRaises(DomainError) as no_notice:
            self.service.award_tender("sup1", "supervisor", self.tender["id"], current["version"])
        self.assertEqual(409, no_notice.exception.status)


if __name__ == "__main__":
    unittest.main()
