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

    def publish_announcement(self, hours=1):
        current = self.service.get_tender("sup1", "supervisor", self.tender["id"])["tender"]
        deadline = (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat(timespec="seconds")
        return self.service.publish_announcement("sup1", "supervisor", self.tender["id"], deadline, current["version"])

    def test_complete_sealed_bid_open_evaluate_and_award_flow(self):
        self.bid(self.vendor1, "vendor1", "B1", 800000, 90)
        self.bid(self.vendor2, "vendor2", "B2", 700000, 80)
        before = self.service.get_tender("vendor1", "vendor", self.tender["id"])
        self.assertEqual("sealed", before["bids"][0]["status"])
        self.assertNotIn("payload", before["bids"][0])
        opened = self.open_and_evaluate()
        announcement = self.publish_announcement()
        self.assertEqual("announcing", self.service.get_tender("sup1", "supervisor", self.tender["id"])["tender"]["status"])
        candidates = announcement["announcement"]["candidates"]
        self.assertEqual(opened["bids"][0]["id"], candidates[0]["bid_id"])
        self.assertEqual(opened["bids"][1]["id"], candidates[1]["bid_id"])
        with self.assertRaises(DomainError) as early:
            current = self.service.get_tender("sup1", "supervisor", self.tender["id"])
            self.service.award_tender("sup1", "supervisor", self.tender["id"], current["tender"]["version"])
        self.assertEqual(409, early.exception.status)
        # 同一轮评审只能有一条有效公示
        with self.assertRaises(DomainError) as dup:
            current = self.service.get_tender("sup1", "supervisor", self.tender["id"])
            later = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(timespec="seconds")
            self.service.publish_announcement("sup1", "supervisor", self.tender["id"], later, current["tender"]["version"])
        self.assertEqual(409, dup.exception.status)
        past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(timespec="seconds")
        expired = self.service.connect()
        expired.execute("UPDATE announcements SET objection_deadline=? WHERE tender_id=?", (past, self.tender["id"]))
        expired.commit()
        expired.close()
        current = self.service.get_tender("sup1", "supervisor", self.tender["id"])
        award = self.service.award_tender("sup1", "supervisor", self.tender["id"], current["tender"]["version"])
        self.assertEqual("awarded", award["tender"]["status"])
        self.assertEqual(opened["bids"][0]["id"], award["award"]["winner"]["bid_id"])

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

    def test_public_announcement_shows_only_order_and_deadline(self):
        self.bid(self.vendor1, "vendor1", "B5", 800000, 90)
        self.bid(self.vendor2, "vendor2", "B6", 700000, 80)
        self.open_and_evaluate()
        self.assertIsNone(self.service.get_tender("anonymous", "public", self.tender["id"])["announcement"])
        deadline = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(timespec="seconds")
        self.publish_announcement()
        public = self.service.get_tender("anonymous", "public", self.tender["id"])["announcement"]
        self.assertEqual("active", public["status"])
        self.assertEqual(deadline[:16], public["objection_deadline"][:16])
        self.assertEqual(2, len(public["candidates"]))
        for candidate in public["candidates"]:
            self.assertNotIn("score", candidate)
            self.assertNotIn("price", candidate)
        state = self.service.state("anonymous", "public")
        self.assertEqual(1, len(state["announcements"]))
        self.assertNotIn("score", state["announcements"][0])

    def test_objection_accepted_invalidates_announcement_and_reevaluates(self):
        self.bid(self.vendor1, "vendor1", "B7", 800000, 90)
        self.bid(self.vendor2, "vendor2", "B8", 700000, 80)
        opened = self.open_and_evaluate()
        self.publish_announcement()
        # 公示期间评分已锁定，无法改分
        with self.assertRaises(DomainError) as locked:
            self.service.evaluate_bid("eval1", "evaluator", opened["bids"][0]["id"], {"报价": 800000, "质量": 90})
        self.assertEqual(409, locked.exception.status)
        # 非供应商不能提异议
        with self.assertRaises(DomainError) as denied:
            self.service.submit_objection("eval1", "evaluator", self.tender["id"], "有问题")
        self.assertEqual(403, denied.exception.status)
        objection = self.service.submit_objection("vendor2", "vendor", self.tender["id"], "候选顺序存疑")
        with self.assertRaises(DomainError) as blocked:
            current = self.service.get_tender("sup1", "supervisor", self.tender["id"])
            self.service.award_tender("sup1", "supervisor", self.tender["id"], current["tender"]["version"])
        self.assertEqual(409, blocked.exception.status)
        # 截止时间过后不能再提异议
        past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(timespec="seconds")
        conn = self.service.connect()
        conn.execute("UPDATE announcements SET objection_deadline=? WHERE tender_id=?", (past, self.tender["id"]))
        conn.commit()
        conn.close()
        with self.assertRaises(DomainError):
            self.service.submit_objection("vendor1", "vendor", self.tender["id"], "过期异议")
        result = self.service.resolve_objection("sup1", "supervisor", objection["id"], "accepted", "重新评审")
        self.assertEqual("accepted", result["status"])
        tender = self.service.get_tender("sup1", "supervisor", self.tender["id"])
        self.assertEqual("reevaluation", tender["tender"]["status"])
        self.assertEqual(2, tender["tender"]["evaluation_round"])
        self.assertEqual(0, tender["tender"]["evaluations_locked"])
        # 失效公示保留留痕，但不再是有效公示
        self.assertEqual("invalid", tender["announcement"]["status"])
        self.assertIsNone(self.service.get_tender("vendor1", "vendor", self.tender["id"])["announcement"])
        self.assertIsNone(self.service.get_tender("anonymous", "public", self.tender["id"])["announcement"])
        # 失效公示保留，新一轮可以再发一条；同一轮重复发布仍被拒绝
        self.service.evaluate_bid("eval1", "evaluator", opened["bids"][1]["id"], {"报价": 700000, "质量": 95})
        self.service.evaluate_bid("eval2", "evaluator", opened["bids"][1]["id"], {"报价": 700000, "质量": 95})
        self.service.evaluate_bid("eval1", "evaluator", opened["bids"][0]["id"], {"报价": 800000, "质量": 90})
        deadline = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(timespec="seconds")
        current = self.service.get_tender("sup1", "supervisor", self.tender["id"])["tender"]
        second = self.service.publish_announcement("sup1", "supervisor", self.tender["id"], deadline, current["version"])
        self.assertEqual(2, second["announcement"]["evaluation_round"])
        with self.assertRaises(DomainError) as dup:
            self.service.publish_announcement("sup1", "supervisor", self.tender["id"], deadline, current["version"] + 1)
        self.assertEqual(409, dup.exception.status)
        # 授标以第二轮公示快照为准：vendor2 提分后成为第一
        past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(timespec="seconds")
        conn = self.service.connect()
        conn.execute("UPDATE announcements SET objection_deadline=? WHERE status='active' AND tender_id=?", (past, self.tender["id"]))
        conn.commit()
        conn.close()
        current = self.service.get_tender("sup1", "supervisor", self.tender["id"])["tender"]
        award = self.service.award_tender("sup1", "supervisor", self.tender["id"], current["version"])
        self.assertEqual(opened["bids"][1]["id"], award["award"]["winner"]["bid_id"])
        self.assertEqual(2, award["award"]["round"])

    def test_rejected_objection_allows_award_after_deadline(self):
        self.bid(self.vendor1, "vendor1", "B9", 800000, 90)
        self.bid(self.vendor2, "vendor2", "B10", 700000, 80)
        opened = self.open_and_evaluate()
        self.publish_announcement()
        objection = self.service.submit_objection("vendor2", "vendor", self.tender["id"], "顺序有疑问")
        self.service.resolve_objection("sup1", "supervisor", objection["id"], "rejected", "评分无误")
        past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(timespec="seconds")
        conn = self.service.connect()
        conn.execute("UPDATE announcements SET objection_deadline=? WHERE tender_id=?", (past, self.tender["id"]))
        conn.commit()
        conn.close()
        current = self.service.get_tender("sup1", "supervisor", self.tender["id"])
        award = self.service.award_tender("sup1", "supervisor", self.tender["id"], current["tender"]["version"])
        self.assertEqual(opened["bids"][0]["id"], award["award"]["winner"]["bid_id"])

    def test_only_supervisor_can_publish_announcement(self):
        self.bid(self.vendor1, "vendor1", "B11", 800000, 90)
        self.bid(self.vendor2, "vendor2", "B12", 700000, 80)
        self.open_and_evaluate()
        deadline = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(timespec="seconds")
        current = self.service.get_tender("proc1", "procurement", self.tender["id"])
        with self.assertRaises(DomainError) as ctx:
            self.service.publish_announcement("proc1", "procurement", self.tender["id"], deadline, current["tender"]["version"])
        self.assertEqual(403, ctx.exception.status)
        with self.assertRaises(DomainError) as ctx2:
            self.service.award_tender("proc1", "procurement", self.tender["id"], current["tender"]["version"])
        self.assertEqual(403, ctx2.exception.status)


if __name__ == "__main__":
    unittest.main()
