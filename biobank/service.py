"""应用服务：资料合成、按操作审查、按容器办理的总编排。

角色边界（对应需求）：
- 伦理员：按操作给出可批准范围与欠缺依据（:meth:`decide_operation`），
  伦理意见不代替签署，未获准分析不能包装成附属步骤。
- 库管：按容器发放/回收/核对（issue/return/reverify/...），体积不可重复承诺。
- 研究者：视图区分可继续、待停止、可否共享；实验事实发生即留存。
所有办理都写入追加式账本，并保留当时的依据快照。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from . import dates
from .consent import ConsentService
from . import extraction
from .extraction import ExtractionResult, extract
from .inventory import ContainerState, InventoryService
from .ledger import Event, Ledger
from .model import (
    Allocation,
    AllocationState as AState,
    Basis,
    Claim,
    ClaimType,
    Decision,
    ExecutionRecord,
    Finding,
    FindingCode,
    FullDate,
    Modality,
    OperationRecord,
    ProductKind,
    PurposeBucket,
)


class ServiceError(RuntimeError):
    def __init__(self, message: str, findings: list[Finding] | None = None):
        super().__init__(message)
        self.findings = findings or []


@dataclass
class OperationView:
    operation: OperationRecord
    research: str
    modality: Modality | None
    modality_inferred: bool
    cancelled: bool
    donor: str | None
    intended_sources: tuple[str, ...]
    claims: tuple[Claim, ...]


class BiobankService:
    def __init__(self, ledger_path: str | Path):
        self.ledger = Ledger(ledger_path)
        self.packs: list[dict] = []
        self._pack_hashes: set[str] = set()
        self.extraction: ExtractionResult | None = None
        self.consents: ConsentService | None = None
        self.inventory: InventoryService | None = None
        self.decisions: dict[str, Decision] = {}
        self.allocations: dict[str, Allocation] = {}
        self.executions: list[ExecutionRecord] = []
        self.sharing_decisions: list[dict] = []
        self.notes: list[dict] = []
        self._replay()

    # ============================================================ loading
    def load_pack(self, path: str | Path) -> dict:
        path = Path(path)
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if digest in self._pack_hashes:
            raise ServiceError(f"资料包 {path.name} 已载入，不可重复入账")
        case = json.loads(raw.decode("utf-8"))
        self.ledger.append(
            "pack_loaded",
            {"path": str(path), "sha256": digest, "case": case},
            sources=tuple(r["id"] for r in case["records"]),
        )
        self._ingest_pack(case, digest)
        return {"case_id": case["case_id"], "sha256": digest, "records": len(case["records"])}

    def _ingest_pack(self, case: dict, digest: str) -> None:
        self.packs.append(case)
        self._pack_hashes.add(digest)
        merged = self._merged_case()
        self.extraction = extract(merged)
        self.consents = ConsentService(self.extraction)
        if self.inventory is None:
            self.inventory = InventoryService(self.extraction.containers)
        else:  # 新包可能登记新容器；旧容器登记保持不变
            self.inventory._records.update(self.extraction.containers)  # noqa: SLF001
            for cid, rec in self.extraction.containers.items():
                self.inventory._states.setdefault(cid, ContainerState(rec))  # noqa: SLF001
        # 历史办理记录（仅首包存在时导入；重放时账本已含事件）
        if not any(e.event_type == "historical_allocation_imported" for e in self.ledger.events):
            for item in case.get("existing", []):
                self._import_historical_allocation(item)
        # 实验室事实与归还事实：资料合成阶段即可入账（事实，不是审批）。
        # 同一原文可拆成多条子句事实，事件键细化到"原文编号+容器+子句"。
        factual_keys = {
            (tuple(e.sources), e.payload.get("fact_key"))
            for e in self.ledger.events
            if e.event_type in ("lab_event_recorded", "return_recorded")
        }
        for claim in self.extraction.claims:
            fact_key = f"{claim.record_id}:{claim.container or ''}:{claim.text}"
            if (claim.sources, fact_key) in factual_keys:
                continue
            if claim.claim_type is ClaimType.LAB_EVENT:
                self._record_lab_from_claim(claim, fact_key)
            elif claim.claim_type is ClaimType.RETURN_EVENT:
                self._record_return_from_claim(claim, fact_key)

    def _merged_case(self) -> dict:
        records: list[dict] = []
        objects: dict[str, list] = {}
        existing: list[dict] = []
        case_id = None
        for pack in self.packs:
            case_id = case_id or pack["case_id"]
            records.extend(pack["records"])
            for group, items in pack.get("objects", {}).items():
                objects.setdefault(group, []).extend(items)
            existing.extend(pack.get("existing", []))
        return {"case_id": case_id, "records": records, "objects": objects, "existing": existing}

    # ------------------------------------------------------------ replay
    def _replay(self) -> None:
        for event in self.ledger.events:
            self._apply(event)

    def _apply(self, event: Event) -> None:
        p = event.payload
        et = event.event_type
        if et == "pack_loaded":
            case = p["case"]
            self.packs.append(case)
            self._pack_hashes.add(p["sha256"])
        elif et == "historical_allocation_imported":
            allocation = _allocation_from_dict(p["allocation"])
            historical_findings = [
                Finding(FindingCode(f["code"]), f["message"], tuple(f.get("sources", ())))
                for f in p.get("findings", [])
            ]
            recorded = self._ensure_inventory().apply_historical_allocation(
                allocation, historical_findings
            )
            self.allocations[recorded.id] = recorded
        elif et == "lab_event_recorded":
            inv = self._ensure_inventory()
            inv.apply_lab_event(
                p["container"], p.get("volume_used"), p.get("volume_loss"),
                p["executed"], event.sources[0] if event.sources else "",
            )
            if p.get("execution"):
                self.executions.append(_execution_from_dict(p["execution"]))
        elif et == "return_recorded":
            self._ensure_inventory().apply_return(
                p["container"], p["volume"], event.sources[0] if event.sources else ""
            )
        elif et == "ethics_decision_recorded":
            self.decisions[p["decision"]["operation_id"]] = _decision_from_dict(p["decision"])
        elif et in ("allocation_opened", "allocation_held"):
            allocation = _allocation_from_dict(p["allocation"])
            self._ensure_inventory().apply_allocation_opened(allocation)
            self.allocations[allocation.id] = allocation
        elif et == "allocation_recalled":
            self._inventory_state_change(p["allocation_id"], AState.RECALLED)
        elif et == "allocation_stopped":
            self._inventory_state_change(p["allocation_id"], AState.STOPPED_PENDING)
        elif et == "allocation_reverified":
            inv = self._ensure_inventory()
            finding = inv.apply_reverify(p["container"], p["volume"])
            if p["allocation_id"] in self.allocations:
                self._inventory_state_change(p["allocation_id"], AState.REVERIFIED)
            if finding:
                self.notes.append({"level": "warning", "message": finding.message})
        elif et == "withdrawal_processed":
            for change in p.get("allocation_changes", []):
                self._inventory_state_change(change["allocation_id"], AState(change["state"]))
            self.notes.append({"level": "info", "message": p["summary"], "sources": list(event.sources)})
        elif et == "sharing_request_decided":
            self.sharing_decisions.append(p)
        elif et == "note_added":
            self.notes.append(p)
        # 重建派生服务
        if self.packs:
            merged = self._merged_case()
            self.extraction = extract(merged)
            self.consents = ConsentService(self.extraction)

    def _ensure_inventory(self) -> InventoryService:
        if self.inventory is None:
            if self.extraction is None:
                raise ServiceError("尚未载入任何资料包")
            self.inventory = InventoryService(self.extraction.containers)
        return self.inventory

    def _inventory_state_change(self, allocation_id: str, state: AState) -> None:
        self.inventory.apply_allocation_state(allocation_id, state)
        if allocation_id in self.allocations:
            old = self.allocations[allocation_id]
            self.allocations[allocation_id] = Allocation(**{**old.__dict__, "state": state})

    # ============================================================ history import
    def _import_historical_allocation(self, item: dict) -> None:
        assert self.extraction and self.consents and self.inventory
        day = FullDate(item["date"])
        container = self.inventory.containers[item["container"]]
        donor = self.inventory.donor_of(item["container"])
        volume = item.get("volume", container.volume)
        modality = self._historical_modality(item["research"], day)
        basis = self._basis_for(donor, item["research"], day, extra_sources=(item["source"],))
        # 历史分配在当时不存在"余样"授权语境，按一般物料范围核对
        findings = self.consents.coverage_findings(donor, modality, day, material=None)
        if modality is Modality.OTHER:
            # 无法从当时材料回溯模态：不能臆测为已授权的代谢，挂歧义待伦理确认
            findings.insert(
                0,
                Finding(
                    FindingCode.UNRESOLVED_AMBIGUITY,
                    f"历史分配 {item['id']} 的分析模态无法从当时原文回溯，"
                    "不能按主题相近推定授权，须伦理核对原始方案",
                    (item["source"],),
                ),
            )
        # 纯提示项（无日期等）不构成历史办理缺陷，阻断依据与歧义都要标注
        advisory = {FindingCode.DATE_UNKNOWN}
        blocking = [f for f in findings if f.code not in advisory]
        allocation = Allocation(
            id=item["id"], container=item["container"], donor=donor,
            research=item["research"], operation_id=item.get("operation_id", ""),
            volume=volume, state=AState.ISSUED, opened_on=day, basis=basis,
            sources=(item["source"],),
            notes=("历史办理记录按原文导入，体积未载明时取容器登记量",),
        )
        recorded = self.inventory.apply_historical_allocation(allocation, blocking)
        self.allocations[recorded.id] = recorded
        self.ledger.append(
            "historical_allocation_imported",
            {
                "allocation": recorded.to_dict(),
                "findings": [f.to_dict() for f in blocking],
                "advisory_findings": [f.to_dict() for f in findings if f.code in advisory],
            },
            sources=(item["source"],),
        )

    def _historical_modality(self, research: str, as_of: FullDate | None = None) -> Modality:
        """历史分配未登记模态的保守回溯。

        只采信在该时点已经成立的材料：实验室已发生事实优先；方案操作的模态必须来自
        不晚于该时点的原文，后补修订（如转录方案）不能倒推为历史分配的模态。
        """
        assert self.extraction
        # 1) 同期实验室事实（已发生的实验是当时真实操作的最强证据）
        for claim in self.extraction.claims:
            if (
                claim.claim_type is ClaimType.LAB_EVENT
                and claim.research == research
                and claim.modality is not None
                and claim.executed
                and (claim.date is None or as_of is None or claim.date <= as_of)
            ):
                return claim.modality
        # 2) 同研究操作登记，其来源原文必须有日期且不晚于历史时点；
        #    无日期的后补原文不能用于历史时点倒推（保守口径）
        for op in self.extraction.operations.values():
            if op.research != research:
                continue
            source = self.extraction.records.get(op.source, {})
            source_date = dates.first_date(source.get("text", ""), extraction.DEFAULT_YEAR)
            if as_of is None:
                modality = self._operation_modality(op)
                if modality is not None:
                    return modality
                continue
            if source_date is None or source_date > as_of:
                continue
            modality = self._operation_modality(op)
            if modality is not None:
                return modality
        return Modality.OTHER

    def _record_lab_from_claim(self, claim: Claim, fact_key: str | None = None) -> None:
        assert self.inventory
        findings, resolved_loss = self.inventory.apply_lab_event(
            claim.container, claim.volume_used, claim.volume_loss,
            bool(claim.executed), claim.record_id,
            volume_remaining=claim.volume_remaining,
        )
        execution = None
        if claim.executed and claim.modality:
            day = claim.date or self._anchor_lab_date(claim)
            basis = self._basis_for(
                self.inventory.donor_of(claim.container), claim.research or "", day,
                extra_sources=claim.sources,
            )
            at_time = self.consents.coverage_findings(
                self.inventory.donor_of(claim.container), claim.modality, day
            ) if self.consents else []
            # 无日期实验须显式提示：时间线结论建立在推定日期上
            if claim.date is None:
                at_time = tuple(at_time) + (
                    Finding(
                        FindingCode.DATE_UNKNOWN,
                        f"{claim.record_id} 未载明实验日期，按在发分配开立日 {day.iso} 推定",
                        claim.sources,
                    ),
                )
            execution = ExecutionRecord(
                operation_id=self._operation_id_for_lab(claim),
                research=claim.research or "",
                container=claim.container,
                modality=claim.modality,
                volume_used=claim.volume_used or 0,
                on=day,
                basis_at_time=basis,
                findings_at_time=tuple(at_time),
                sources=claim.sources,
            )
            self.executions.append(execution)
        self.ledger.append(
            "lab_event_recorded",
            {"container": claim.container, "volume_used": claim.volume_used,
             "volume_loss": resolved_loss, "executed": bool(claim.executed),
             "fact_key": fact_key,
             "execution": _execution_to_dict(execution) if execution else None,
             "findings": [f.to_dict() for f in findings]},
            sources=claim.sources,
        )

    def _record_return_from_claim(self, claim: Claim, fact_key: str | None = None) -> None:
        assert self.inventory
        volume = claim.volume_remaining or 0
        finding = self.inventory.apply_return(claim.container, volume, claim.record_id)
        self.ledger.append(
            "return_recorded",
            {"container": claim.container, "volume": volume, "fact_key": fact_key,
             "finding": finding.to_dict() if finding else None},
            sources=claim.sources,
        )
        if finding:
            self.notes.append({"level": "warning", "message": finding.message, "sources": [claim.record_id]})

    def _anchor_lab_date(self, claim: Claim) -> FullDate:
        """无日期实验室事实的时间锚点：同容器在发分配的开立日；找不到才退回今天。"""
        for allocation in self.allocations.values():
            if allocation.container == claim.container:
                return allocation.opened_on
        return dates.today()

    def _operation_id_for_lab(self, claim: Claim) -> str:
        assert self.extraction
        for op in self.extraction.operations.values():
            if op.research != claim.research:
                continue
            modality = self._operation_modality(op)
            if modality == claim.modality:
                return op.id
        return ""

    # ============================================================ operation views
    def operation_views(self) -> list[OperationView]:
        assert self.extraction
        views: list[OperationView] = []
        for op in self.extraction.operations.values():
            intended = [
                c for c in self.extraction.claims
                if c.record_id == op.source and c.bucket is PurposeBucket.INTENDED
            ]
            modality = self._operation_modality(op)
            # 只有该操作自身模态被修订取消时才标记 cancelled
            cancelled_modalities = {
                c.modality for c in intended
                if c.ambiguity_note and "取消" in (c.ambiguity_note or "")
            }
            cancelled = modality in cancelled_modalities
            inferred = modality is not None and not any(
                c.modality == modality and not (c.ambiguity_note and "取消" in (c.ambiguity_note or ""))
                for c in intended
            )
            donor = self._donor_for_research(op.research)
            views.append(
                OperationView(
                    operation=op, research=op.research, modality=modality,
                    modality_inferred=inferred, cancelled=cancelled, donor=donor,
                    intended_sources=tuple(c.record_id for c in intended),
                    claims=tuple(intended),
                )
            )
        return views

    def _operation_modality(self, op: OperationRecord) -> Modality | None:
        assert self.extraction
        intended = [
            c for c in self.extraction.claims
            if c.record_id == op.source and c.bucket is PurposeBucket.INTENDED and c.modality
        ]
        cancelled_modalities = {
            c.modality for c in intended
            if c.ambiguity_note and "取消" in (c.ambiguity_note or "")
        }
        active = [c for c in intended if c.modality not in cancelled_modalities]
        if len({c.modality for c in active}) == 1:
            return active[0].modality
        if active:
            return active[0].modality
        # 无有效意图时按同研究已发生实验回溯
        for claim in self.extraction.claims:
            if (
                claim.claim_type is ClaimType.LAB_EVENT
                and claim.research == op.research
                and claim.executed and claim.modality
            ):
                return claim.modality
        return intended[0].modality if intended else None

    def _donor_for_research(self, research: str) -> str | None:
        assert self.extraction
        for claim in self.extraction.claims:
            if claim.research == research and claim.donor:
                return claim.donor
        for allocation in self.allocations.values():
            if allocation.research == research:
                return allocation.donor
        return None

    def protocol_intentions(self) -> list[dict]:
        """方案级意图（含被修订取消、从未登记为操作的意图，如 WGS）。"""
        assert self.extraction
        rows: list[dict] = []
        seen: set[tuple[str, Modality]] = set()
        for claim in self.extraction.claims:
            if claim.bucket is not PurposeBucket.INTENDED or not claim.modality:
                continue
            key = (claim.research or "", claim.modality)
            if key in seen:
                continue
            seen.add(key)
            cancelled = "取消" in claim.text
            rows.append(
                {
                    "research": claim.research,
                    "modality": claim.modality,
                    "cancelled_by_amendment": cancelled,
                    "source": claim.record_id,
                    "text": claim.text,
                }
            )
        return rows

    # ============================================================ ethics decisions
    def decide_operation(
        self,
        operation_id: str,
        day: FullDate | None = None,
        donor: str | None = None,
        material: str | None = None,
    ) -> Decision:
        assert self.extraction and self.consents
        day = day or dates.today()
        op = self.extraction.operations.get(operation_id)
        if op is None:
            raise ServiceError(f"未知操作 {operation_id}")
        view = next(v for v in self.operation_views() if v.operation.id == operation_id)
        donor = donor or view.donor
        findings: list[Finding] = []
        if donor is None:
            findings.append(Finding(FindingCode.UNRESOLVED_AMBIGUITY,
                                    "无法从资料确定操作对象捐赠者，须伦理确认后再办理",
                                    view.intended_sources))
        if view.modality is None:
            findings.append(Finding(FindingCode.UNRESOLVED_AMBIGUITY,
                                    "操作模态无法从原文确定", view.intended_sources))
        if view.cancelled:
            findings.append(Finding(FindingCode.UNAPPROVED_ANCILLARY,
                                    "该操作已被修订取消，不得按原方案办理", view.intended_sources))
        if donor and view.modality:
            findings.extend(
                self.consents.coverage_findings(donor, view.modality, day, material=material)
            )
            self._append_ancillary_finding(findings, donor, view, day)
        # 歧义未经确认的授权（unknown signer）
        for grant in self.consents.grants_for(donor or ""):
            if grant.signer == "unknown":
                findings.append(Finding(FindingCode.UNRESOLVED_AMBIGUITY,
                                        f"{grant.sources[0]} 签署资格待伦理确认", grant.sources))
        advisory_codes = {FindingCode.DATE_UNKNOWN}
        blocking = [f for f in findings if f.code not in advisory_codes]
        approved = not blocking
        basis = self._basis_for(donor or "", op.research, day,
                                extra_sources=tuple(s for f in findings for s in f.sources))
        decision = Decision(
            operation_id=op.id, research=op.research,
            modality=view.modality or Modality.OTHER,
            approved=approved, on=day, findings=tuple(findings), basis=basis,
        )
        self.decisions[op.id] = decision
        self.ledger.append(
            "ethics_decision_recorded",
            {"decision": _decision_to_dict(decision)},
            sources=tuple(dict.fromkeys(basis.grant_sources + basis.ethics_sources
                                        + basis.proposal_sources)),
        )
        return decision

    def _append_ancillary_finding(
        self, findings: list[Finding], donor: str, view: OperationView, day: FullDate
    ) -> None:
        """未获准分析不能包装成已批准操作的附属步骤。"""
        assert self.consents
        blocked_codes = {FindingCode.EXCLUDED, FindingCode.NO_GRANT,
                         FindingCode.WITHDRAWN, FindingCode.EXPIRED}
        if not any(f.code in blocked_codes for f in findings):
            return
        # 方案中是否把未授权模态表述为背景/延伸（如"背景涉及遗传风险"）
        assert self.extraction
        for claim in self.extraction.claims:
            if (
                claim.research == view.research
                and claim.bucket is PurposeBucket.BACKGROUND
                and claim.modality == view.modality
            ):
                findings.append(
                    Finding(
                        FindingCode.UNAPPROVED_ANCILLARY,
                        f"{view.modality.label}仅出现在背景陈述中且未获授权，"
                        "不能包装成已批准操作的附属步骤",
                        claim.sources,
                    )
                )

    def _basis_for(
        self,
        donor: str,
        research: str,
        day: FullDate,
        extra_sources: tuple[str, ...] = (),
    ) -> Basis:
        assert self.extraction
        grant_sources = tuple(s for g in self.consents.grants_for(donor) for s in g.sources) \
            if self.consents else ()
        ethics_sources = tuple(
            c.record_id for c in self.extraction.claims
            if c.claim_type is ClaimType.ETHICS and (not research or c.research in (None, research))
        )
        proposal_sources = tuple(
            r["id"] for r in self._merged_case()["records"]
            if r["kind"] in ("proposal", "amendment")
            and (not research or research in r["text"])
        )
        notes = tuple(dict.fromkeys(extra_sources))
        return Basis(
            decision_date=day,
            grant_sources=tuple(dict.fromkeys(grant_sources)),
            ethics_sources=tuple(dict.fromkeys(ethics_sources)),
            proposal_sources=tuple(dict.fromkeys(proposal_sources)),
            notes=notes,
        )

    # ============================================================ custodian actions
    def issue(
        self,
        operation_id: str,
        container_id: str,
        volume: int,
        day: FullDate | None = None,
        allocation_id: str | None = None,
    ) -> Allocation:
        return self._open_allocation(operation_id, container_id, volume, day,
                                     allocation_id, "allocation_opened", AState.ISSUED)

    def hold(
        self,
        operation_id: str,
        container_id: str,
        volume: int,
        day: FullDate | None = None,
        allocation_id: str | None = None,
    ) -> Allocation:
        return self._open_allocation(operation_id, container_id, volume, day,
                                     allocation_id, "allocation_held", AState.HELD)

    def _open_allocation(
        self, operation_id, container_id, volume, day, allocation_id, event_type, state
    ) -> Allocation:
        assert self.extraction and self.inventory and self.consents
        day = day or dates.today()
        op = self.extraction.operations.get(operation_id)
        if op is None:
            raise ServiceError(f"未知操作 {operation_id}")
        donor = self.inventory.donor_of(container_id)
        view = next(v for v in self.operation_views() if v.operation.id == operation_id)
        if view.modality is None:
            raise ServiceError("操作模态不明，不能发放",
                               [Finding(FindingCode.UNRESOLVED_AMBIGUITY, "模态待确认")])
        # 发放前再次核对授权（不能只凭历史决定）；余样授权只对已核对的余样物料生效
        consent_findings = self.consents.coverage_findings(
            donor, view.modality, day,
            material="remnant" if self._grant_remnant_only(donor, view.modality) else None,
        )
        inventory_findings = self.inventory.check_commit(container_id, volume)
        all_findings = consent_findings + inventory_findings
        # 方案拒绝混入：本次发放体积能由该容器独自满足即合法；
        # 拟用量的剩余缺口另在评估视图提示"不足部分暂缓"，不在此拦截本次发放
        advisory_codes = {FindingCode.DATE_UNKNOWN}
        blocking = [f for f in all_findings if f.code not in advisory_codes]
        if blocking:
            raise ServiceError("发放被规则拦截，该研究部分暂缓", blocking)
        allocation = Allocation(
            id=allocation_id or f"AL-{len(self.allocations) + 1:03d}",
            container=container_id, donor=donor, research=op.research,
            operation_id=op.id, volume=volume, state=state, opened_on=day,
            basis=self._basis_for(donor, op.research, day),
            sources=view.intended_sources,
        )
        self.inventory.apply_allocation_opened(allocation)
        self.allocations[allocation.id] = allocation
        self.ledger.append(event_type, {"allocation": allocation.to_dict()},
                           sources=allocation.sources)
        return allocation

    def _mixing_refused(self, research: str, donor: str) -> list[Finding]:
        assert self.extraction
        for claim in self.extraction.claims:
            if claim.research == research and "混入" in claim.text:
                return [Finding(FindingCode.MIXING_REFUSED,
                                f"方案拒绝混入其他捐赠者补量（{donor} 不足时不得用他人样本凑量）",
                                claim.sources)]
        return []

    def _grant_remnant_only(self, donor: str, modality: Modality) -> bool:
        """该模态在有效授权中是否仅有"余样"范围的授权。"""
        assert self.consents
        grants = [
            g for g in self.consents.grants_for(donor)
            if modality in g.modalities and g.material_scope == "remnant"
        ]
        if not grants:
            return False
        return not any(
            modality in g.modalities and g.material_scope is None
            for g in self.consents.grants_for(donor)
        )

    def _donor_shortfall(self, view: OperationView, donor: str) -> int:
        """该捐赠者全部可分配容器的已核对可用量相对拟用量的缺口（>0 才存在补量需求）。"""
        assert self.inventory
        available = 0
        for cid, state in self.inventory.donor_inventory(donor).items():
            if not self.inventory.is_aliquoted_parent(cid):
                available += state.verified_available
        return max(0, view.operation.consumes - available)

    def record_lab_event(
        self,
        container_id: str,
        research: str,
        modality: Modality,
        *,
        volume_used: int | None = None,
        volume_loss: int | None = None,
        executed: bool = False,
        source_id: str = "manual",
        day: FullDate | None = None,
    ) -> dict:
        """办理新到的实验室报告（损耗/消耗/完成），可多次调用。"""
        assert self.inventory
        if any(source_id in e.sources for e in self.ledger.events):
            raise ServiceError(f"{source_id} 已入账，不能重复登记")
        day = day or dates.today()
        findings, _ = self.inventory.apply_lab_event(
            container_id, volume_used, volume_loss, executed, source_id
        )
        execution = None
        if executed:
            donor = self.inventory.donor_of(container_id)
            basis = self._basis_for(donor, research, day, extra_sources=(source_id,))
            at_time = self.consents.coverage_findings(donor, modality, day, material=None) \
                if self.consents else []
            execution = ExecutionRecord(
                operation_id="", research=research, container=container_id,
                modality=modality, volume_used=volume_used or 0, on=day,
                basis_at_time=basis, findings_at_time=tuple(at_time),
                sources=(source_id,),
            )
            self.executions.append(execution)
        self.ledger.append(
            "lab_event_recorded",
            {"container": container_id, "volume_used": volume_used, "volume_loss": volume_loss,
             "executed": executed, "execution": _execution_to_dict(execution) if execution else None,
             "findings": [f.to_dict() for f in findings]},
            sources=(source_id,),
        )
        return {"findings": [f.to_dict() for f in findings]}

    def record_return(
        self, container_id: str, volume: int, source_id: str, day: FullDate | None = None
    ) -> dict:
        assert self.inventory
        if any(source_id in e.sources for e in self.ledger.events):
            raise ServiceError(f"{source_id} 已入账，不能重复登记")
        finding = self.inventory.apply_return(container_id, volume, source_id)
        self.ledger.append(
            "return_recorded",
            {"container": container_id, "volume": volume,
             "finding": finding.to_dict() if finding else None},
            sources=(source_id,),
        )
        return {"finding": finding.to_dict() if finding else None}

    def reverify(self, container_id: str, volume: int, allocation_id: str | None = None) -> dict:
        """旧分配余样凭新依据重新核对（如成年后补充同意到达后恢复用途）。"""
        assert self.inventory and self.consents
        finding = self.inventory.apply_reverify(container_id, volume)
        if finding:
            raise ServiceError("核对失败", [finding])
        if allocation_id and allocation_id in self.allocations:
            self._inventory_state_change(allocation_id, AState.REVERIFIED)
        self.ledger.append(
            "allocation_reverified",
            {"container": container_id, "volume": volume, "allocation_id": allocation_id or ""},
        )
        return {"container": container_id, "verified_available":
                self.inventory.state(container_id).verified_available}

    # ============================================================ withdrawal cascade
    def process_withdrawal(self, record_id: str) -> dict:
        """一项用途撤回的三级处理：未发收回、已发未分析停止、已完成结果保留。"""
        assert self.extraction and self.inventory
        claim = next((c for c in self.extraction.claims
                      if c.record_id == record_id and c.claim_type is ClaimType.WITHDRAWAL), None)
        if claim is None:
            raise ServiceError(f"未知撤回原文 {record_id}")
        if any(e.event_type == "withdrawal_processed" and record_id in e.sources
               for e in self.ledger.events):
            raise ServiceError(f"{record_id} 的撤回已办理，不能重复入账")
        changes: list[dict] = []
        blocked_future: list[str] = []
        terminal_states = (
            AState.RECALLED, AState.CONSUMED, AState.RETURNED,
            AState.REVERIFIED, AState.STOPPED_PENDING,
        )
        for allocation in list(self.allocations.values()):
            if allocation.donor != claim.donor:
                continue
            if allocation.state in terminal_states:
                continue
            state = self.inventory.state(allocation.container)
            # 历史缺陷分配也在撤回范围内：依据不足不影响撤回动作的生成。
            # outstanding=0 可能是已被损耗/归还冲销，此时按物理剩余登记停止核对。
            residual = state.outstanding or state.physical_remaining
            if residual > 0:
                if allocation.state is AState.HELD:
                    self._inventory_state_change(allocation.id, AState.RECALLED)
                    changes.append({"allocation_id": allocation.id,
                                    "state": AState.RECALLED.value,
                                    "reason": "未发分配收回"})
                else:
                    self._inventory_state_change(allocation.id, AState.STOPPED_PENDING)
                    changes.append({"allocation_id": allocation.id,
                                    "state": AState.STOPPED_PENDING.value,
                                    "reason": "已发未分析部分停止使用并确认剩余",
                                    "outstanding_volume": state.outstanding,
                                    "physical_remaining": state.physical_remaining})
                    blocked_future.append(allocation.id)
            elif allocation.state is AState.DEFECTIVE_HISTORICAL:
                # 在发量已被实验/损耗/归还冲销的历史分配：登记核对动作，不改写历史
                changes.append({"allocation_id": allocation.id,
                                "state": allocation.state.value,
                                "reason": "历史分配无在发余量；撤回仅约束今后用途，"
                                          "已完成结果按原文保留并核对剩余"})
        summary = (
            f"{record_id}：未发分配收回 {sum(1 for c in changes if c['state'] == 'recalled')} 笔；"
            f"已发未分析停止 {len(blocked_future)} 笔；已完成结果按原文保留，"
            f"新增对外共享={'禁止' if claim.withdraw_future_sharing else '按原文限制'}"
        )
        self.ledger.append(
            "withdrawal_processed",
            {"record_id": record_id, "allocation_changes": changes, "summary": summary,
             "modalities": [m.value for m in claim.withdraw_modalities],
             "keep_completed_results": claim.keep_completed_results,
             "future_sharing_blocked": claim.withdraw_future_sharing},
            sources=(record_id,),
        )
        self.notes.append({"level": "info", "message": summary, "sources": [record_id]})
        return {"summary": summary, "changes": changes}

    # ============================================================ sharing
    def decide_sharing(
        self,
        research: str,
        donor: str,
        product: ProductKind,
        target: str,
        day: FullDate | None = None,
        sources: tuple[str, ...] = (),
    ) -> dict:
        assert self.consents
        day = day or dates.today()
        findings = self.consents.sharing_findings(donor, product, day)
        allowed = not findings
        result = {
            "research": research, "donor": donor, "product": product.value,
            "target": target, "allowed": allowed, "on": day.to_dict(),
            "findings": [f.to_dict() for f in findings], "sources": list(sources),
        }
        self.ledger.append("sharing_request_decided", result, sources=sources)
        self.sharing_decisions.append(result)
        return result

    # ============================================================ assessment helpers
    def assess_request(self, operation_id: str, day: FullDate | None = None) -> dict:
        """研究者视角：该操作在各候选容器上的可办理性（授权+体积一并核对）。"""
        assert self.extraction and self.inventory
        day = day or dates.today()
        view = next(v for v in self.operation_views() if v.operation.id == operation_id)
        donor = view.donor
        rows = []
        if donor and view.modality:
            consent = self.consents.coverage_findings(
                donor, view.modality, day,
                material="remnant" if self._grant_remnant_only(donor, view.modality) else None,
            )
        else:
            consent = [Finding(FindingCode.UNRESOLVED_AMBIGUITY, "捐赠者或模态不明")]
        for cid, state in self.inventory.donor_inventory(donor or "").items():
            if self.inventory.is_aliquoted_parent(cid):
                continue  # 已分装父容器不可再分
            inv = self.inventory.check_commit(cid, view.operation.consumes)
            rows.append({
                "container": cid,
                "verified_available": state.verified_available,
                "returned_pending": state.returned_pending,
                "requested": view.operation.consumes,
                "inventory_findings": [f.to_dict() for f in inv],
            })
        total_available = sum(r["verified_available"] for r in rows)
        already_committed = sum(
            al.volume for al in self.allocations.values()
            if al.research == view.research
            and (al.operation_id == operation_id or not al.operation_id)
            and al.state in (AState.ISSUED, AState.HELD, AState.REVERIFIED)
        )
        remaining_need = max(0, view.operation.consumes - already_committed)
        max_issuable = min(remaining_need, total_available)
        shortfall = max(0, remaining_need - total_available)
        mixing_refused = bool(self._mixing_refused(view.research, donor or ""))
        feasibility_notes: list[str] = []
        if already_committed:
            feasibility_notes.append(
                f"该操作已承诺/发放 {already_committed}μL，拟用量 {view.operation.consumes}μL，"
                f"尚缺 {remaining_need}μL"
            )
        if shortfall:
            feasibility_notes.append(
                f"{donor} 当前已核对可用合计 {total_available}μL，仍有缺口 {shortfall}μL"
            )
            if mixing_refused:
                feasibility_notes.append(
                    "方案明文拒绝混入其他捐赠者补量：缺口部分暂缓，不得用他人样本凑量"
                )
        return {
            "operation_id": operation_id,
            "research": view.research,
            "modality": view.modality.value if view.modality else None,
            "donor": donor,
            "consent_findings": [f.to_dict() for f in consent],
            "containers": rows,
            "already_committed": already_committed,
            "total_verified_available": total_available,
            "max_issuable_now": max_issuable,
            "shortfall": shortfall,
            "mixing_refused": mixing_refused,
            "feasibility_notes": feasibility_notes,
        }

    def verify_ledger(self) -> bool:
        return self.ledger.verify()


# ====================================================================== serde
def _decision_to_dict(d: Decision) -> dict:
    return {
        "operation_id": d.operation_id,
        "research": d.research,
        "modality": d.modality.value,
        "approved": d.approved,
        "on": d.on.to_dict(),
        "findings": [f.to_dict() for f in d.findings],
        "basis": d.basis.to_dict(),
        "ethics_officer": d.ethics_officer,
    }


def _decision_from_dict(d: dict) -> Decision:
    return Decision(
        operation_id=d["operation_id"], research=d["research"],
        modality=Modality(d["modality"]), approved=d["approved"],
        on=FullDate(**d["on"]),
        findings=tuple(
            Finding(FindingCode(f["code"]), f["message"], tuple(f.get("sources", ())))
            for f in d["findings"]
        ),
        basis=Basis.from_dict(d["basis"]),
        ethics_officer=d.get("ethics_officer", "officer"),
    )


def _allocation_from_dict(d: dict) -> Allocation:
    return Allocation(
        id=d["id"], container=d["container"], donor=d["donor"], research=d["research"],
        operation_id=d["operation_id"], volume=d["volume"],
        state=AState(d["state"]), opened_on=FullDate(**d["opened_on"]),
        basis=Basis.from_dict(d["basis"]), sources=tuple(d.get("sources", ())),
        notes=tuple(d.get("notes", ())),
    )


def _execution_to_dict(e: ExecutionRecord | None) -> dict | None:
    if e is None:
        return None
    return {
        "operation_id": e.operation_id, "research": e.research,
        "container": e.container, "modality": e.modality.value,
        "volume_used": e.volume_used,
        "on": e.on.to_dict() if e.on else None,
        "basis_at_time": e.basis_at_time.to_dict(),
        "findings_at_time": [f.to_dict() for f in e.findings_at_time],
        "sources": list(e.sources),
    }


def _execution_from_dict(d: dict) -> ExecutionRecord:
    return ExecutionRecord(
        operation_id=d["operation_id"], research=d["research"], container=d["container"],
        modality=Modality(d["modality"]), volume_used=d["volume_used"],
        on=FullDate(**d["on"]) if d.get("on") else None,
        basis_at_time=Basis.from_dict(d["basis_at_time"]),
        findings_at_time=tuple(
            Finding(FindingCode(f["code"]), f["message"], tuple(f.get("sources", ())))
            for f in d["findings_at_time"]
        ),
        sources=tuple(d.get("sources", ())),
    )
