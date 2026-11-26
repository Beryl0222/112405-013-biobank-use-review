"""同意时间线：签署资格、代签有效期、撤回、再联系、用途例外、共享限制。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _support import C3_DATE, ServiceTestCase, TODAY, WITHDRAWAL_DATE  # noqa: E402
from biobank.model import FindingCode, FullDate, Modality, ProductKind  # noqa: E402


class ConsentTimelineTest(ServiceTestCase):
    def test_guardian_grant_expires_one_year_after_adulthood(self):
        # D8 成年 2025-08-01，代签有效至 2026-08-01
        valid, expired = self.svc.consents.valid_grants_on("D8", FullDate("2026-07-31"))
        self.assertTrue(any(
            g.signer == "guardian" and "BR-C1" in g.sources for g in valid))
        valid2, expired2 = self.svc.consents.valid_grants_on("D8", FullDate("2026-08-02"))
        self.assertTrue(any(
            g.signer == "guardian" and "BR-C1" in g.sources for g in expired2))
        self.assertFalse(any(
            g.signer == "guardian" and "BR-C1" in g.sources for g in valid2))

    def test_similar_topic_does_not_expand_authorization(self):
        # 全基因组被明文排除：不能借"都是遗传/组学"主题相近扩大授权
        findings = self.svc.consents.coverage_findings("D8", Modality.WGS, TODAY)
        codes = {f.code for f in findings}
        self.assertIn(FindingCode.EXCLUDED, codes)

    def test_metabolic_withdrawn_after_date_blocks_new_experiments(self):
        before = self.svc.consents.coverage_findings(
            "D8", Modality.METABOLIC, FullDate("2026-08-02"))
        after = self.svc.consents.coverage_findings(
            "D8", Modality.METABOLIC, WITHDRAWAL_DATE)
        self.assertNotIn(FindingCode.WITHDRAWN, {f.code for f in before})
        self.assertIn(FindingCode.WITHDRAWN, {f.code for f in after})

    def test_re_contact_bar_does_not_block_donor_initiated_supplement(self):
        # C3 是捐赠者主动补充同意，仍登记为有效 donor grant
        valid, _ = self.svc.consents.valid_grants_on("D8", TODAY)
        self.assertTrue(any("BR-C3" in g.sources and g.signer == "donor" for g in valid))
        # 但研究者主动补授权的路径仍被再联系限制封住
        findings = self.svc.consents.coverage_findings("D8", Modality.METABOLIC, TODAY)
        self.assertIn(FindingCode.RE_CONTACT_BARRED, {f.code for f in findings})

    def test_remnant_scope_blocks_non_remnant_material_commitment(self):
        # 明确按余样核对、却要发非余样物料时受限
        blocked = self.svc.consents.coverage_findings(
            "D8", Modality.TRANSCRIPT, TODAY, material="fresh_draw")
        self.assertIn(FindingCode.MATERIAL_SCOPE, {f.code for f in blocked})
        # 一般口径不因为授权自带余样范围而否定模态覆盖
        general = self.svc.consents.coverage_findings(
            "D8", Modality.TRANSCRIPT, TODAY, material=None)
        self.assertNotIn(FindingCode.MATERIAL_SCOPE, {f.code for f in general})

    def test_d9_sharing_product_specific_limits(self):
        summary = self.svc.consents.sharing_findings(
            "D9", ProductKind.DEIDENTIFIED_SUMMARY, TODAY)
        self.assertEqual(summary, [])  # 明文允许
        individual = self.svc.consents.sharing_findings(
            "D9", ProductKind.IDENTIFIABLE_VARIANTS, TODAY)
        self.assertIn(FindingCode.SHARING_NOT_COVERED, {f.code for f in individual})

    def test_unknown_signer_grant_never_effective(self):
        # 构造一个签署人不明的授权
        from biobank.model import Grant
        weird = Grant(
            donor="D9", signer="unknown",
            modalities=(Modality.METABOLIC,), excludes=(),
            granted_on=None, expiry=None, re_contact_allowed=None,
            material_scope=None, sharing_allowed={}, sources=("X-1",),
        )
        self.svc.consents.grants.setdefault("D9", []).append(weird)
        ok, reason = self.svc.consents._signer_eligible_on(weird, TODAY)
        self.assertFalse(ok)
        self.assertIn("资格", reason)

    def test_adult_supplement_restores_transcript_only(self):
        findings = self.svc.consents.coverage_findings(
            "D8", Modality.TRANSCRIPT, C3_DATE, material="remnant")
        blocking = {f.code for f in findings} - {FindingCode.DATE_UNKNOWN}
        self.assertEqual(blocking, set())
