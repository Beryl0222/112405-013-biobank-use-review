"""账本完整性、重放一致性、角色视图与多轮办理。"""
from __future__ import annotations

import json as _json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _support import CASE_PATH, ServiceTestCase, TODAY  # noqa: E402
from biobank.model import FullDate, Modality, ProductKind  # noqa: E402
from biobank.service import BiobankService  # noqa: E402
from biobank.views import Views  # noqa: E402


class LedgerReplayTest(ServiceTestCase):
    def test_ledger_verifies_and_rebuilds_identical_state(self):
        self.assertTrue(self.svc.verify_ledger())
        self.svc.process_withdrawal("BR-W1")
        self.svc.reverify("T8A", 40)
        self.svc.reverify("T8B", 30)
        self.svc.issue("O12", "T8A", 40, TODAY, allocation_id="AL-R1")
        replayed = BiobankService(self.ledger_path)
        self.assertTrue(replayed.verify_ledger())
        self.assertEqual(
            {a.id: a.state.value for a in self.svc.allocations.values()},
            {a.id: a.state.value for a in replayed.allocations.values()},
        )
        for cid in ("T8A", "T8B"):
            s1 = self.svc.inventory.state(cid)
            s2 = replayed.inventory.state(cid)
            self.assertEqual((s1.consumed, s1.lost, s1.committed, s1.returned_pending),
                             (s2.consumed, s2.lost, s2.committed, s2.returned_pending))
        self.assertEqual(len(self.svc.executions), len(replayed.executions))

    def test_duplicate_pack_is_rejected(self):
        with self.assertRaises(Exception):
            self.svc.load_pack(CASE_PATH)

    def test_tampered_ledger_is_detected(self):
        from biobank.ledger import LedgerIntegrityError
        path = self.ledger_path
        self.svc.process_withdrawal("BR-W1")
        lines = Path(path).read_text(encoding="utf-8").splitlines()
        event = _json.loads(lines[0])
        event["payload"]["case"]["case_id"] = "tampered"
        lines[0] = _json.dumps(event, ensure_ascii=False, sort_keys=True)
        Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
        with self.assertRaises(LedgerIntegrityError):
            BiobankService(path)


class ViewsTest(ServiceTestCase):
    def setUp(self):
        super().setUp()
        self.views = Views(self.svc)
        self.svc.process_withdrawal("BR-W1")
        self.svc.reverify("T8A", 40)
        self.svc.reverify("T8B", 30)

    def test_ethics_view_lists_all_four_buckets(self):
        view = self.views.ethics_view(TODAY)
        self.assertIn("background", view["purpose_buckets"])
        self.assertIn("intended", view["purpose_buckets"])
        self.assertIn("existing_result", view["purpose_buckets"])
        self.assertIn("future_sharing", view["purpose_buckets"])
        self.assertTrue(view["clarifications"])
        self.assertIn("不替代真实伦理审查", view["disclaimer"])

    def test_custodian_view_freezes_aliquoted_parent(self):
        view = self.views.custodian_view()
        t8 = next(c for c in view["containers"] if c["container"] == "T8")
        self.assertFalse(t8["allocatable"])
        t8a = next(c for c in view["containers"] if c["container"] == "T8A")
        self.assertTrue(t8a["allocatable"])

    def test_researcher_view_separates_continue_stop_and_sharing(self):
        view = self.views.researcher_view("R1", TODAY)
        modalities_continue = {e["modality"] for e in view["can_continue"]}
        self.assertIn("transcript", modalities_continue)
        stopped_modalities = {e["modality"] for e in view["must_stop_or_withheld"]}
        self.assertIn("metabolic", stopped_modalities)
        sharing = {s["product"]: s["allowed"] for s in view["result_sharing"]}
        self.assertFalse(sharing[ProductKind.IDENTIFIABLE_VARIANTS.value])
        self.assertTrue(view["completed_experiments"])

    def test_donor_view_shows_timeline_and_recontact(self):
        view = self.views.donor_view("D8", TODAY)
        self.assertTrue(view["re_contact_barred"])
        self.assertTrue(view["active_withdrawals"])
        valid_sources = [s for g in view["valid_grants"] for s in g["sources"]]
        self.assertIn("BR-C3", valid_sources)
        expired_sources = [s for g in view["expired_grants"] for s in g["sources"]]
        self.assertIn("BR-C1", expired_sources)


class MultiRoundTest(ServiceTestCase):
    def test_second_round_new_operation_and_material_change(self):
        # 第一轮：撤回与核对
        self.svc.process_withdrawal("BR-W1")
        self.svc.reverify("T8B", 30)
        # 第二轮：新方案部分办理 + 新损耗报告
        self.svc.issue("O21", "T8B", 30, TODAY, allocation_id="AL-R2")
        self.svc.record_lab_event(
            "T8B", "R2", Modality.TRANSCRIPT,
            volume_used=10, executed=True, source_id="BR-L3", day=TODAY,
        )
        state = self.svc.inventory.state("T8B")
        self.assertEqual(state.consumed, 10)
        # 历史事件数量只增不改
        seq_before = len(self.svc.ledger.events)
        self.svc.record_return("T8B", 20, "BR-R2", TODAY)
        self.assertEqual(len(self.svc.ledger.events), seq_before + 1)
        self.assertEqual(self.svc.inventory.state("T8B").returned_pending, 20)
