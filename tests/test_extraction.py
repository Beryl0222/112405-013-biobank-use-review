"""原文抽取：四桶用途、模态、同意语义、实验室事实、歧义保留。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _support import ServiceTestCase  # noqa: E402
from biobank.model import ClaimType, Modality, ProductKind, PurposeBucket  # noqa: E402


class ExtractionTest(ServiceTestCase):
    def _claims(self, record_id: str):
        return [c for c in self.svc.extraction.claims if c.record_id == record_id]

    def test_background_is_separated_from_intended_operation(self):
        background = [c for c in self._claims("BR-P1") if c.bucket is PurposeBucket.BACKGROUND]
        intended = [c for c in self._claims("BR-P1") if c.bucket is PurposeBucket.INTENDED]
        self.assertEqual(len(background), 1)
        self.assertEqual(background[0].modality, Modality.WGS)
        self.assertIn("不构成实际操作", background[0].ambiguity_note)
        modalities = {c.modality for c in intended}
        self.assertEqual(modalities, {Modality.METABOLIC, Modality.WGS})

    def test_four_purpose_buckets_are_distinguished(self):
        buckets = {c.bucket for c in self.svc.extraction.claims if c.bucket is not None}
        self.assertEqual(
            buckets,
            {PurposeBucket.BACKGROUND, PurposeBucket.INTENDED,
             PurposeBucket.EXISTING_RESULT, PurposeBucket.FUTURE_SHARING},
        )

    def test_amendment_cancels_wgs_and_introduces_transcript(self):
        amendment = [c for c in self._claims("BR-P2")
                     if c.claim_type is ClaimType.AMENDMENT_OP]
        cancelled = [c for c in amendment if c.ambiguity_note and "取消" in c.ambiguity_note]
        active = [c for c in amendment if not c.ambiguity_note]
        self.assertEqual([c.modality for c in cancelled], [Modality.WGS])
        self.assertEqual([c.modality for c in active], [Modality.TRANSCRIPT])
        # "代谢结果用于汇总论文"归到共享计划桶，不重复登记为新操作
        sharing = [c for c in self._claims("BR-P2")
                   if c.bucket is PurposeBucket.FUTURE_SHARING]
        self.assertTrue(sharing)

    def test_consent_includes_and_excludes_modalities(self):
        grants = {c.record_id: c for c in self.svc.extraction.claims
                  if c.claim_type is ClaimType.GRANT}
        c1 = grants["BR-C1"]
        self.assertEqual(c1.signer, "guardian")
        self.assertIn(Modality.METABOLIC, c1.includes)
        self.assertIn(Modality.WGS, c1.excludes)
        self.assertIsNotNone(c1.grant_expiry)
        self.assertEqual(c1.grant_expiry.iso, "2026-08-01")
        self.assertFalse(c1.re_contact_allowed)

        c2 = grants["BR-C2"]
        self.assertEqual(c2.signer, "donor")
        self.assertIn(Modality.TRANSCRIPT, c2.includes)
        self.assertIn(Modality.WGS, c2.excludes)
        self.assertFalse(c2.sharing_allowed[ProductKind.IDENTIFIABLE_VARIANTS])
        self.assertTrue(c2.sharing_allowed[ProductKind.DEIDENTIFIED_SUMMARY])

    def test_supplemental_consent_is_donor_signed_remnant_scope(self):
        c3 = next(c for c in self._claims("BR-C3") if c.claim_type is ClaimType.GRANT)
        self.assertEqual(c3.signer, "donor")
        self.assertEqual(c3.includes, (Modality.TRANSCRIPT,))
        self.assertIn(Modality.WGS, c3.excludes)
        self.assertEqual(c3.material_scope, "remnant")
        self.assertFalse(c3.sharing_allowed[ProductKind.IDENTIFIABLE_VARIANTS])

    def test_withdrawal_carries_modality_and_sharing_block(self):
        w = next(c for c in self.svc.extraction.claims
                 if c.claim_type is ClaimType.WITHDRAWAL)
        self.assertEqual(w.withdraw_modalities, (Modality.METABOLIC,))
        self.assertTrue(w.withdraw_future_sharing)
        self.assertTrue(w.keep_completed_results)

    def test_lab_execution_remaining_and_loss_split(self):
        lab = [c for c in self.svc.extraction.claims if c.claim_type is ClaimType.LAB_EVENT]
        executed = next(c for c in lab if c.executed)
        self.assertEqual(executed.container, "T8A")
        self.assertEqual(executed.volume_used, 60)
        loss_claim = next(c for c in lab if c.volume_loss is not None)
        self.assertEqual(loss_claim.container, "T8B")
        self.assertEqual(loss_claim.volume_remaining, 30)
        # 损耗差值由库存层按在发量结算，抽取层保留哨兵
        self.assertEqual(loss_claim.volume_loss, -1)

    def test_return_event_keeps_original_container(self):
        ret = next(c for c in self.svc.extraction.claims
                   if c.claim_type is ClaimType.RETURN_EVENT)
        self.assertEqual(ret.container, "T8A")
        self.assertEqual(ret.volume_remaining, 40)
        self.assertIn("重新核对", ret.ambiguity_note)

    def test_every_claim_keeps_source_sentence(self):
        for claim in self.svc.extraction.claims:
            self.assertTrue(claim.text.strip())
            self.assertTrue(claim.record_id)
            self.assertEqual(claim.sources, (claim.record_id,))
