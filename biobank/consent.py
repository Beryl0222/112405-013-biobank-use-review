"""同意时间线服务。

核心口径：
- 研究者称"主题相近"不能扩大授权；覆盖判定按模态+产物+物料范围逐条核对。
- 伦理可以确认歧义限制，但不能代替无资格的人签同意（未知签署人的授权不生效）。
- 代签授权在成年有效期后失效；本人补充同意不构成研究者违规再联系。
- 撤回只作用于今后用途；已完成结果的保存与共享另按原文单独限制。
"""
from __future__ import annotations

from dataclasses import dataclass

from .extraction import ExtractionResult
from .model import (
    ClaimType,
    Clarification,
    Finding,
    FindingCode,
    FullDate,
    Grant,
    Modality,
    ProductKind,
    Withdrawal,
)


@dataclass
class ConsentSnapshot:
    donor: str
    valid_grants: tuple[Grant, ...]
    expired_grants: tuple[Grant, ...]
    withdrawals: tuple[Withdrawal, ...]
    clarifications: tuple[Clarification, ...]
    re_contact_barred: bool
    findings: tuple[Finding, ...]


class ConsentService:
    def __init__(self, extraction: ExtractionResult):
        self._ex = extraction
        self.grants: dict[str, list[Grant]] = {}
        self.withdrawals: dict[str, list[Withdrawal]] = {}
        self.clarifications: list[Clarification] = []
        self._build()

    # ------------------------------------------------------------------ build
    def _build(self) -> None:
        for claim in self._ex.claims:
            if claim.claim_type is ClaimType.GRANT and claim.donor:
                grant = Grant(
                    donor=claim.donor,
                    signer=claim.signer or "unknown",
                    modalities=claim.includes,
                    excludes=claim.excludes,
                    granted_on=claim.date,
                    expiry=claim.grant_expiry,
                    re_contact_allowed=claim.re_contact_allowed,
                    material_scope=claim.material_scope,
                    sharing_allowed=dict(claim.sharing_allowed),
                    sources=claim.sources,
                )
                self.grants.setdefault(claim.donor, []).append(grant)
            elif claim.claim_type is ClaimType.WITHDRAWAL and claim.donor:
                self.withdrawals.setdefault(claim.donor, []).append(
                    Withdrawal(
                        donor=claim.donor,
                        on=claim.date,
                        modalities=claim.withdraw_modalities,
                        future_sharing_blocked=claim.withdraw_future_sharing,
                        keep_completed_results=claim.keep_completed_results,
                        sources=claim.sources,
                    )
                )
            elif claim.claim_type is ClaimType.ETHICS:
                self.clarifications.append(
                    Clarification(
                        research=claim.research,
                        note=claim.ethics_clarifies or claim.ethics_text or "",
                        on=claim.date,
                        sources=claim.sources,
                    )
                )

    def grants_for(self, donor: str) -> list[Grant]:
        return list(self.grants.get(donor, ()))

    def withdrawals_for(self, donor: str) -> list[Withdrawal]:
        return list(self.withdrawals.get(donor, ()))

    def is_re_contact_barred(self, donor: str) -> bool:
        return any(g.re_contact_allowed is False for g in self.grants_for(donor))

    # ------------------------------------------------------------- eligibility
    def _signer_eligible_on(self, grant: Grant, day: FullDate) -> tuple[bool, str | None]:
        donor = self._ex.donors.get(grant.donor)
        adult_on = donor.adult_on if donor else None
        if grant.signer == "guardian":
            if grant.expiry is not None and day > grant.expiry:
                return False, f"代签授权已于 {grant.expiry.iso} 到期"
            if adult_on is not None and grant.expiry is None and day > adult_on:
                return False, "捐赠者已成年且未约定代签有效期，代签不再有效"
            return True, None
        if grant.signer == "donor":
            if adult_on is not None and day < adult_on:
                return False, "本人签署时未成年，该签署不具备资格"
            # 无日期本人授权：能确认其在该时点已存在时（如材料包整体早于办理日）
            # 保守视为有效，另以 DATE_UNKNOWN 提示；不因此阻断
            return True, None
        return False, "签署人资格原文未明述，须伦理确认前不生效"

    def valid_grants_on(self, donor: str, day: FullDate) -> tuple[list[Grant], list[Grant]]:
        valid: list[Grant] = []
        expired: list[Grant] = []
        for grant in self.grants_for(donor):
            ok, _ = self._signer_eligible_on(grant, day)
            (valid if ok else expired).append(grant)
        return valid, expired

    def active_withdrawals_on(self, donor: str, day: FullDate) -> list[Withdrawal]:
        active: list[Withdrawal] = []
        for withdrawal in self.withdrawals_for(donor):
            if withdrawal.on is None:
                continue  # 无日期撤回不臆造生效时间
            if withdrawal.on <= day:
                active.append(withdrawal)
        return active

    def coverage_findings(
        self,
        donor: str,
        modality: Modality,
        day: FullDate,
        material: str | None = None,
    ) -> list[Finding]:
        """核对某捐赠者在某日做某模态分析是否有有效授权。"""
        findings: list[Finding] = []
        valid, expired = self.valid_grants_on(donor, day)
        active_withdrawals = self.active_withdrawals_on(donor, day)

        # 1) 明文排除（用途例外）——最强限制，任何授权都不能用主题相近绕过
        for grant in self.grants_for(donor):
            if modality in grant.excludes:
                findings.append(
                    Finding(
                        FindingCode.EXCLUDED,
                        f"{grant.donor} 的授权明文排除{modality.label}，主题相近不扩大授权",
                        grant.sources,
                    )
                )

        # 2) 撤回（今后实验）
        for withdrawal in active_withdrawals:
            if modality in withdrawal.modalities:
                findings.append(
                    Finding(
                        FindingCode.WITHDRAWN,
                        f"{donor} 已于 {withdrawal.on} 撤回{modality.label}的今后实验",
                        withdrawal.sources,
                    )
                )

        # 3) 在有效授权中寻找覆盖。
        # 物料范围只在"按余样授权、却要发非余样物料"时构成限制（库管发放环节）；
        # 一般口径（历史时点、伦理层操作判断）不因授权自带范围而否定模态覆盖。
        covered: list[Grant] = []
        scope_blocked: list[Grant] = []
        for grant in valid:
            if modality not in grant.modalities:
                continue
            if grant.material_scope == "remnant" and material not in ("remnant", None):
                scope_blocked.append(grant)
            else:
                covered.append(grant)

        for grant in scope_blocked:
            findings.append(
                Finding(
                    FindingCode.MATERIAL_SCOPE,
                    f"{grant.sources[0]} 的授权仅限余样（remnant），当前物料不在该范围",
                    grant.sources,
                )
            )

        # 4) 无覆盖时解释欠缺依据
        if not covered and not any(f.code is FindingCode.EXCLUDED for f in findings):
            if expired and any(modality in g.modalities for g in expired):
                for grant in expired:
                    if modality in grant.modalities:
                        ok, reason = self._signer_eligible_on(grant, day)
                        findings.append(
                            Finding(FindingCode.EXPIRED, reason or "授权在办理时点已失效", grant.sources)
                        )
            elif not any(modality in g.modalities for g in self.grants_for(donor)):
                findings.append(
                    Finding(
                        FindingCode.NO_GRANT,
                        f"{donor} 不存在覆盖{modality.label}的授权",
                        tuple(s for g in self.grants_for(donor) for s in g.sources),
                    )
                )
        if not covered and not findings:
            findings.append(Finding(FindingCode.NO_GRANT, f"{donor} 对{modality.label}无可用授权"))

        # 5) 再联系限制：依据欠缺时，说明不能靠研究者直接联系补授权
        if not covered and self.is_re_contact_barred(donor):
            findings.append(
                Finding(
                    FindingCode.RE_CONTACT_BARRED,
                    "原文限制研究者再次直接联系捐赠者；欠缺授权不能通过研究者联系补取，"
                    "仅捐赠者主动补充同意可恢复用途",
                    tuple(s for g in self.grants_for(donor) if g.re_contact_allowed is False for s in g.sources),
                )
            )

        # 6) 无日期授权的保守提示
        for grant in self.grants_for(donor):
            if grant.granted_on is None and modality in grant.modalities:
                findings.append(
                    Finding(
                        FindingCode.DATE_UNKNOWN,
                        f"{grant.sources[0]} 未载明签署日期，时间线判断为保守口径",
                        grant.sources,
                    )
                )
        return findings

    def sharing_findings(
        self, donor: str, product: ProductKind, day: FullDate
    ) -> list[Finding]:
        """产物共享按原文单独限制核对，与样本分析授权分开判断。

        三种口径：明文允许 -> 放行；明文限制/须另行同意 -> 阻断；
        原文完全未提及该产物 -> 阻断并提示不得默认允许。
        """
        findings: list[Finding] = []
        explicitly_allowed = False
        for grant in self.grants_for(donor):
            if product not in grant.sharing_allowed:
                continue
            if grant.sharing_allowed[product] is False:
                findings.append(
                    Finding(
                        FindingCode.SHARING_NOT_COVERED,
                        f"{grant.sources[0]} 明文限制{product.label}共享（个体结果须另行同意）",
                        grant.sources,
                    )
                )
            else:
                explicitly_allowed = True
        for withdrawal in self.active_withdrawals_on(donor, day):
            if withdrawal.future_sharing_blocked:
                findings.append(
                    Finding(
                        FindingCode.WITHDRAWN,
                        "撤回声明不得新增对外共享；已形成结果的共享仍按原文单独限制",
                        withdrawal.sources,
                    )
                )
        if not explicitly_allowed and not any(
            f.code is FindingCode.SHARING_NOT_COVERED for f in findings
        ):
            findings.append(
                Finding(
                    FindingCode.SHARING_NOT_COVERED,
                    f"{donor} 的授权未提及{product.label}共享，不得默认允许",
                )
            )
        # 明文允许且无撤回/限制时返回空结论（放行）
        if explicitly_allowed and not findings:
            return []
        return findings

    def snapshot_on(self, donor: str, day: FullDate) -> ConsentSnapshot:
        valid, expired = self.valid_grants_on(donor, day)
        notes: list[Finding] = []
        for grant in valid:
            if grant.granted_on is None:
                notes.append(Finding(FindingCode.DATE_UNKNOWN, "授权原文无日期", grant.sources))
        return ConsentSnapshot(
            donor=donor,
            valid_grants=tuple(valid),
            expired_grants=tuple(expired),
            withdrawals=tuple(self.active_withdrawals_on(donor, day)),
            clarifications=tuple(c for c in self.clarifications),
            re_contact_barred=self.is_re_contact_barred(donor),
            findings=tuple(notes),
        )
