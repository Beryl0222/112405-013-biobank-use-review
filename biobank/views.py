"""四类角色视图。

同一套事件状态按不同职责投影：
- 伦理员：按操作看可批准范围与欠缺依据，按方案意图识别背景/操作/结果/共享四桶。
- 库管：按容器看分出/派生/消耗/损耗/待核对/还可用于什么。
- 研究者：区分可继续实验、待停止实验、可否共享结果。
- 捐赠者：本人授权时间线、撤回状态与再联系限制。
视图只读取状态，不产生新的业务效力。
"""
from __future__ import annotations

from .model import (
    AllocationState as AState,
    FullDate,
    ProductKind,
    PurposeBucket,
)
from .service import BiobankService


class Views:
    def __init__(self, service: BiobankService):
        self.svc = service

    # ============================================================ ethics officer
    def ethics_view(self, day: FullDate | None = None) -> dict:
        svc = self.svc
        day = day or _today()
        operations = []
        for view in svc.operation_views():
            decision = svc.decisions.get(view.operation.id)
            row = {
                "operation_id": view.operation.id,
                "research": view.research,
                "modality": view.modality.value if view.modality else None,
                "cancelled_by_amendment": view.cancelled,
                "donor": view.donor,
                "requested_consumes": view.operation.consumes,
                "intended_sources": list(view.intended_sources),
                "decision": None,
            }
            if decision is not None and decision.on <= day:
                row["decision"] = {
                    "approved": decision.approved,
                    "status": decision.status.value,
                    "on": decision.on.to_dict(),
                    "findings": [f.to_dict() for f in decision.findings],
                    "basis": decision.basis.to_dict(),
                }
            operations.append(row)
        # 方案级意图（含从未登记为操作、被修订取消的意图），按用途四桶陈列
        intentions: dict[str, list] = {b.value: [] for b in PurposeBucket}
        for claim in svc.extraction.claims:
            if claim.bucket is None:
                continue
            intentions[claim.bucket.value].append(
                {
                    "record": claim.record_id,
                    "research": claim.research,
                    "modality": claim.modality.value if claim.modality else None,
                    "product": claim.product.value if claim.product else None,
                    "text": claim.text,
                    "note": claim.ambiguity_note,
                }
            )
        return {
            "as_of": day.to_dict(),
            "protocol_intentions": svc.protocol_intentions(),
            "purpose_buckets": intentions,
            "operations": operations,
            "clarifications": [
                {"research": c.research, "note": c.note, "sources": list(c.sources)}
                for c in svc.consents.clarifications
            ],
            "unparsed_records": [
                {"record": c.record_id, "text": c.text, "note": c.ambiguity_note}
                for c in svc.extraction.claims if c.unparsed
            ],
            "disclaimer": "资料合成结果，不替代真实伦理审查；伦理意见不能代替无资格者签署同意",
        }

    # ============================================================ custodian
    def custodian_view(self) -> dict:
        svc = self.svc
        containers = []
        for row in svc.inventory.availability_report():
            cid = row["container"]
            is_parent = svc.inventory.is_aliquoted_parent(cid)
            containers.append({
                **row,
                "allocatable": not is_parent,
                "frozen_reason": "已分装父容器，体积由子容器承载" if is_parent else None,
            })
        allocations = [
            {
                "id": a.id, "container": a.container, "donor": a.donor,
                "research": a.research, "operation_id": a.operation_id,
                "volume": a.volume, "state": a.state.value,
                "opened_on": a.opened_on.to_dict(),
                "basis": a.basis.to_dict(), "sources": list(a.sources),
                "notes": list(a.notes),
            }
            for a in svc.allocations.values()
        ]
        return {
            "containers": containers,
            "allocations": allocations,
            "lineage_note": "parent 表示物料派生；分装不产生新同意，派生物继承捐赠者与授权范围",
        }

    # ============================================================ researcher
    def researcher_view(self, research: str, day: FullDate | None = None) -> dict:
        svc = self.svc
        day = day or _today()
        can_continue: list[dict] = []
        must_stop: list[dict] = []
        for view in svc.operation_views():
            if view.research != research:
                continue
            assessment = svc.assess_request(view.operation.id, day)
            entry = {
                "operation_id": view.operation.id,
                "modality": view.modality.value if view.modality else None,
                "requested": view.operation.consumes,
                "assessment": assessment,
            }
            blocked_codes = {f["code"] for f in assessment["consent_findings"]}
            if blocked_codes & {"withdrawn", "excluded", "no_grant", "expired",
                                "material_scope", "unresolved_ambiguity"}:
                entry["reason"] = sorted(blocked_codes)
                must_stop.append(entry)
            else:
                can_continue.append(entry)
        # 已发未分析、被停止的分配
        stopped_allocations = [
            {"allocation_id": a.id, "container": a.container, "volume": a.volume,
             "state": a.state.value, "notes": list(a.notes)}
            for a in svc.allocations.values()
            if a.research == research and a.state in (AState.STOPPED_PENDING, AState.RECALLED)
        ]
        # 结果共享：逐个产物给出可行性
        sharing = []
        donor = next((v.donor for v in svc.operation_views()
                      if v.research == research and v.donor), None)
        if donor:
            for product in ProductKind:
                findings = svc.consents.sharing_findings(donor, product, day)
                sharing.append({
                    "product": product.value,
                    "product_label": product.label,
                    "allowed": not findings,
                    "findings": [f.to_dict() for f in findings],
                })
        completed = [
            {"operation_id": e.operation_id, "container": e.container,
             "modality": e.modality.value, "volume_used": e.volume_used,
             "on": e.on.to_dict() if e.on else None,
             "results_kept": True,
             "sharing_subject_to_original_text": True,
             "basis_at_time": e.basis_at_time.to_dict(),
             "findings_at_time": [f.to_dict() for f in e.findings_at_time]}
            for e in svc.executions if e.research == research
        ]
        return {
            "research": research,
            "as_of": day.to_dict(),
            "can_continue": can_continue,
            "must_stop_or_withheld": must_stop,
            "stopped_allocations": stopped_allocations,
            "completed_experiments": completed,
            "result_sharing": sharing,
            "note": "已完成结果不能假装未形成；其共享仍受原文单独限制",
        }

    # ============================================================ donor
    def donor_view(self, donor: str, day: FullDate | None = None) -> dict:
        svc = self.svc
        day = day or _today()
        snapshot = svc.consents.snapshot_on(donor, day)
        donor_obj = svc.extraction.donors.get(donor)
        containers = [
            cid for cid in svc.inventory.donor_inventory(donor)
        ]
        allocations = [
            {"allocation_id": a.id, "container": a.container, "research": a.research,
             "volume": a.volume, "state": a.state.value, "opened_on": a.opened_on.to_dict()}
            for a in svc.allocations.values() if a.donor == donor
        ]
        return {
            "donor": donor,
            "as_of": day.to_dict(),
            "adult_on": donor_obj.adult_on.to_dict() if donor_obj and donor_obj.adult_on else None,
            "valid_grants": [_grant_row(g) for g in snapshot.valid_grants],
            "expired_grants": [_grant_row(g) for g in snapshot.expired_grants],
            "active_withdrawals": [
                {"on": w.on.to_dict() if w.on else None,
                 "modalities": [m.value for m in w.modalities],
                 "future_sharing_blocked": w.future_sharing_blocked,
                 "keep_completed_results": w.keep_completed_results,
                 "sources": list(w.sources)}
                for w in snapshot.withdrawals
            ],
            "re_contact_barred": snapshot.re_contact_barred,
            "containers": containers,
            "allocations": allocations,
            "advisory": [f.to_dict() for f in snapshot.findings],
        }


def _grant_row(grant) -> dict:
    return {
        "signer": grant.signer,
        "modalities": [m.value for m in grant.modalities],
        "excludes": [m.value for m in grant.excludes],
        "granted_on": grant.granted_on.to_dict() if grant.granted_on else None,
        "expiry": grant.expiry.to_dict() if grant.expiry else None,
        "material_scope": grant.material_scope,
        "re_contact_allowed": grant.re_contact_allowed,
        "sharing_allowed": {k.value: v for k, v in grant.sharing_allowed.items()},
        "sources": list(grant.sources),
    }


def _today() -> FullDate:
    from . import dates
    return dates.today()
