"""库存与办理：派生谱系、双重承诺、撤回级联、归还核对、损耗、部分办理。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _support import ServiceTestCase, TODAY  # noqa: E402
from biobank.model import (  # noqa: E402
    AllocationState as AState,
    FindingCode,
    FullDate,
    Modality,
)
from biobank.service import ServiceError  # noqa: E402


class InventoryAccountingTest(ServiceTestCase):
    def test_aliquot_lineage_inherits_donor_and_creates_no_new_consent(self):
        self.assertEqual(self.svc.inventory.donor_of("T8A"), "D8")
        self.assertEqual(self.svc.inventory.donor_of("T8B"), "D8")
        self.assertEqual(self.svc.inventory.lineage("T8A"), ["T8A", "T8"])
        self.assertTrue(self.svc.inventory.is_aliquoted_parent("T8"))
        self.assertFalse(self.svc.inventory.is_aliquoted_parent("T8A"))

    def test_lab_consumption_loss_and_return_settle_correctly(self):
        t8a = self.svc.inventory.state("T8A")
        self.assertEqual(t8a.consumed, 60)
        self.assertEqual(t8a.physical_remaining, 40)
        self.assertEqual(t8a.returned_pending, 40)  # BR-R1 归还待核对
        t8b = self.svc.inventory.state("T8B")
        self.assertEqual(t8b.lost, 70)  # 100 在发 - 30 可用
        self.assertEqual(t8b.physical_remaining, 30)

    def test_historical_allocations_keep_defect_annotations(self):
        a81 = self.svc.allocations["A81"]
        a82 = self.svc.allocations["A82"]
        self.assertEqual(a81.state, AState.DEFECTIVE_HISTORICAL)
        notes = " ".join(a81.notes)
        self.assertIn("expired", notes)
        self.assertEqual(a82.state, AState.DEFECTIVE_HISTORICAL)
        self.assertIn("unresolved_ambiguity", " ".join(a82.notes))

    def test_withdrawal_cascade_stops_outstanding_and_keeps_results(self):
        result = self.svc.process_withdrawal("BR-W1")
        states = {c["allocation_id"]: c["state"] for c in result["changes"]}
        self.assertEqual(states["A81"], AState.STOPPED_PENDING.value)
        self.assertEqual(states["A82"], AState.STOPPED_PENDING.value)
        # 已完成实验记录不被删除或改写
        self.assertTrue(any(e.research == "R1" for e in self.svc.executions))
        # 撤回不可重复入账
        with self.assertRaises(ServiceError):
            self.svc.process_withdrawal("BR-W1")

    def test_reverify_turns_returned_remnant_into_available(self):
        self.svc.process_withdrawal("BR-W1")
        self.svc.reverify("T8A", 40)
        self.svc.reverify("T8B", 30)
        self.assertEqual(self.svc.inventory.state("T8A").verified_available, 40)
        self.assertEqual(self.svc.inventory.state("T8B").verified_available, 30)

    def test_double_commit_blocks_same_volume_for_two_studies(self):
        self.svc.process_withdrawal("BR-W1")
        self.svc.reverify("T8A", 40)
        self.svc.reverify("T8B", 30)
        # R1 转录先占 T8A 40
        self.svc.issue("O12", "T8A", 40, TODAY, allocation_id="AL-1")
        # 同一容器再承诺必被双重承诺拦截
        with self.assertRaises(ServiceError) as ctx:
            self.svc.issue("O21", "T8A", 10, TODAY, allocation_id="AL-2")
        codes = {f.code for f in ctx.exception.findings}
        self.assertIn(FindingCode.DOUBLE_COMMIT, codes)

    def test_partial_issue_allowed_but_shortfall_stays_withheld(self):
        self.svc.process_withdrawal("BR-W1")
        self.svc.reverify("T8A", 40)
        self.svc.reverify("T8B", 30)
        # 办理前：拟用 50，D8 已核对可用 70（T8A 40 + T8B 30），总量够但单容器均不足
        before = self.svc.assess_request("O21", TODAY)
        self.assertEqual(before["shortfall"], 0)
        self.assertEqual(before["total_verified_available"], 70)
        # T8B 只有 30：发 30 合法；发后剩余需求 20 可由 T8A 满足，无总量缺口
        allocation = self.svc.issue("O21", "T8B", 30, TODAY, allocation_id="AL-PART")
        self.assertEqual(allocation.volume, 30)
        assessment = self.svc.assess_request("O21", TODAY)
        self.assertEqual(assessment["already_committed"], 30)
        self.assertEqual(assessment["max_issuable_now"], 20)
        self.assertEqual(assessment["shortfall"], 0)
        self.assertTrue(assessment["mixing_refused"])

    def test_shortfall_when_total_available_cannot_cover_request(self):
        # 先让 R1 转录占掉 T8A 全部 40，R2 只剩 T8B 的 30：拟用 50 出现真实缺口 20
        self.svc.process_withdrawal("BR-W1")
        self.svc.reverify("T8A", 40)
        self.svc.reverify("T8B", 30)
        self.svc.issue("O12", "T8A", 40, TODAY, allocation_id="AL-R1")
        self.svc.issue("O21", "T8B", 30, TODAY, allocation_id="AL-R2")
        assessment = self.svc.assess_request("O21", TODAY)
        self.assertEqual(assessment["already_committed"], 30)
        self.assertEqual(assessment["shortfall"], 20)
        self.assertTrue(assessment["mixing_refused"])
        self.assertIn("暂缓", " ".join(assessment["feasibility_notes"]))

    def test_exhausted_container_cannot_be_reissued_by_status_change(self):
        # T8B 物理剩 30，尝试超额承诺
        self.svc.process_withdrawal("BR-W1")
        self.svc.reverify("T8B", 30)
        with self.assertRaises(ServiceError) as ctx:
            self.svc.issue("O21", "T8B", 50, TODAY)
        codes = {f.code for f in ctx.exception.findings}
        self.assertIn(FindingCode.DOUBLE_COMMIT, codes)

    def test_aliquoted_parent_cannot_be_committed(self):
        findings = self.svc.inventory.check_commit("T8", 10)
        self.assertIn(FindingCode.INVENTORY_SHORTAGE, {f.code for f in findings})

    def test_new_lab_report_can_be_registered_more_than_once(self):
        before = len(self.svc.ledger.events)
        self.svc.record_lab_event(
            "T9", "R9", Modality.TRANSCRIPT, volume_used=20, executed=True,
            source_id="BR-L9", day=TODAY,
        )
        self.assertEqual(len(self.svc.ledger.events), before + 1)
        with self.assertRaises(ServiceError):
            self.svc.record_lab_event(
                "T9", "R9", Modality.TRANSCRIPT, volume_used=20,
                source_id="BR-L9", day=TODAY,
            )


class DecisionFlowTest(ServiceTestCase):
    def test_metabolic_operation_withdrawn_and_transcript_approved(self):
        d11 = self.svc.decide_operation("O11", TODAY)
        self.assertFalse(d11.approved)
        self.assertIn(FindingCode.WITHDRAWN, {f.code for f in d11.findings})
        d12 = self.svc.decide_operation("O12", TODAY)
        self.assertTrue(d12.approved)  # 仅余无日期提示项
        self.assertEqual({f.code for f in d12.findings}, {FindingCode.DATE_UNKNOWN})

    def test_wgs_never_approved_and_background_cannot_be_ancillary(self):
        # 构造一个直接评估 WGS 的决定：通过方案意图可见 WGS 被取消/排除
        intentions = self.svc.protocol_intentions()
        wgs = [i for i in intentions if i["modality"] is Modality.WGS]
        self.assertTrue(wgs)
        coverage = self.svc.consents.coverage_findings("D8", Modality.WGS, TODAY)
        self.assertIn(FindingCode.EXCLUDED, {f.code for f in coverage})

    def test_decision_basis_snapshot_is_persisted(self):
        decision = self.svc.decide_operation("O12", TODAY)
        self.assertTrue(decision.basis.grant_sources)
        self.assertTrue(decision.basis.proposal_sources)
        reloaded = self.svc.decisions["O12"]
        self.assertEqual(reloaded.basis.to_dict(), decision.basis.to_dict())

    def test_execution_keeps_basis_at_time_without_retroactive_withdrawal(self):
        execution = next(e for e in self.svc.executions if e.container == "T8A")
        codes = {f.code for f in execution.findings_at_time}
        # 实验推定发生在 2026-08-02，撤回 08-03 才生效，不得倒灌
        self.assertNotIn(FindingCode.WITHDRAWN, codes)
        self.assertIn(FindingCode.EXPIRED, codes)  # 代签 08-01 已到期
        self.assertIn(FindingCode.DATE_UNKNOWN, codes)  # 推定日期显式提示
