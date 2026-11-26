"""容器库存引擎。

口径（docs/materials.md）：
- 容器 parent 表示物料派生；分装不产生新同意，派生物继承捐赠者与授权范围。
- 同一有限体积不能在两个研究里重复承诺：已承诺（held/issued）体积直接占用可用量。
- 拟用量不是已批准消耗；实际消耗、损耗、归还只能由 lab/return 事件登记。
- 已耗尽容器不得仅改总状态再次分出。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .model import Allocation, AllocationState, ContainerRecord, Finding, FindingCode


@dataclass
class ContainerState:
    record: ContainerRecord
    consumed: int = 0          # 已实验消耗
    lost: int = 0              # 已确认损耗
    committed: int = 0         # 已承诺/已发放占用（未消耗部分）
    returned_pending: int = 0  # 已归还待重新核对
    # 仍在使用方手中、已发未分析（撤回时须停止的部分按此口径核对）
    outstanding: int = 0
    allocations: dict[str, Allocation] = field(default_factory=dict)
    lab_sources: list[str] = field(default_factory=list)
    return_sources: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.record.volume

    @property
    def verified_available(self) -> int:
        """已核对可用体积（不含待核对的归还量）。"""
        return max(
            0,
            self.total - self.consumed - self.lost - self.committed - self.returned_pending,
        )

    @property
    def physical_remaining(self) -> int:
        """物理剩余（含待核对归还量），用于与实验室报告对账。"""
        return max(0, self.total - self.consumed - self.lost)

    def to_dict(self) -> dict:
        return {
            "container": self.record.id,
            "donor": self.donor_id,
            "parent": self.record.parent,
            "total_volume": self.total,
            "consumed": self.consumed,
            "lost": self.lost,
            "committed": self.committed,
            "returned_pending": self.returned_pending,
            "outstanding": self.outstanding,
            "verified_available": self.verified_available,
            "physical_remaining": self.physical_remaining,
            "sources": list(self.record.sources),
        }

    @property
    def donor_id(self) -> str | None:
        return self.record.donor


class InventoryError(ValueError):
    def __init__(self, message: str, findings: list[Finding] | None = None):
        super().__init__(message)
        self.findings = findings or []


class InventoryService:
    def __init__(self, containers: dict[str, ContainerRecord]):
        self._records = dict(containers)
        self._states = {cid: ContainerState(record=c) for cid, c in containers.items()}

    # ------------------------------------------------------------- registries
    @property
    def containers(self) -> dict[str, ContainerRecord]:
        return dict(self._records)

    def state(self, container_id: str) -> ContainerState:
        if container_id not in self._states:
            raise InventoryError(f"未知容器 {container_id}")
        return self._states[container_id]

    def states(self) -> dict[str, ContainerState]:
        return dict(self._states)

    def donor_of(self, container_id: str) -> str:
        """沿 parent 上溯到捐赠者；派生物不产生新同意。"""
        seen: set[str] = set()
        current = container_id
        while current in self._records:
            if current in seen:
                raise InventoryError(f"容器谱系存在环：{container_id}")
            seen.add(current)
            record = self._records[current]
            if record.donor:
                return record.donor
            if not record.parent:
                break
            current = record.parent
        raise InventoryError(f"容器 {container_id} 无法上溯捐赠者")

    def lineage(self, container_id: str) -> list[str]:
        chain: list[str] = []
        current = container_id
        seen: set[str] = set()
        while current in self._records and current not in seen:
            seen.add(current)
            chain.append(current)
            current = self._records[current].parent or ""
        return chain

    def descendants(self, container_id: str) -> list[str]:
        out = [cid for cid, rec in self._records.items() if rec.parent == container_id]
        for child in list(out):
            out.extend(self.descendants(child))
        return out

    def is_aliquoted_parent(self, container_id: str) -> bool:
        """已分装的父容器体积已由子容器承载，不能再次参与分配。"""
        return bool(self.descendants(container_id))

    def allocatable_containers(self) -> list[str]:
        return [cid for cid in self._records if not self.is_aliquoted_parent(cid)]

    # ------------------------------------------------------------- mutations
    def check_commit(self, container_id: str, volume: int) -> list[Finding]:
        """发放/承诺前核对：体积为正、未耗尽、无双重承诺、父容器未分装。"""
        state = self.state(container_id)
        findings: list[Finding] = []
        if self.is_aliquoted_parent(container_id):
            findings.append(
                Finding(
                    FindingCode.INVENTORY_SHORTAGE,
                    f"{container_id} 已分装为 {', '.join(self.descendants(container_id))}，"
                    "父容器体积已由子容器承载，分装不产生新同意也不能重复分出",
                )
            )
            return findings
        if volume <= 0:
            findings.append(Finding(FindingCode.INVENTORY_SHORTAGE, "分配体积必须为正"))
            return findings
        if state.physical_remaining <= 0:
            findings.append(
                Finding(
                    FindingCode.INVENTORY_SHORTAGE,
                    f"{container_id} 已耗尽（总 {state.total}，消耗 {state.consumed}，损耗 {state.lost}），"
                    "不能仅改总状态再次分出",
                    state.lab_sources,
                )
            )
        if volume > state.verified_available:
            findings.append(
                Finding(
                    FindingCode.DOUBLE_COMMIT,
                    f"{container_id} 已核对可用 {state.verified_available}μL，"
                    f"本次拟承诺 {volume}μL；同一有限体积不能在两个研究重复承诺",
                    tuple(state.lab_sources),
                )
            )
        return findings

    def apply_allocation_opened(self, allocation: Allocation) -> None:
        state = self.state(allocation.container)
        findings = self.check_commit(allocation.container, allocation.volume)
        blocking = [f for f in findings if f.code in (FindingCode.DOUBLE_COMMIT, FindingCode.INVENTORY_SHORTAGE)]
        if blocking:
            raise InventoryError("分配被库存规则拦截", blocking)
        state.committed += allocation.volume
        state.outstanding += allocation.volume
        state.allocations[allocation.id] = allocation

    def apply_historical_allocation(self, allocation: Allocation, findings: list[Finding]) -> Allocation:
        """历史办理记录原样入账；依据缺陷只标注，不按今天的规则改写历史。

        返回（可能被标注为缺陷态的）分配对象，供调用方同步引用。
        重放时分配对象已含缺陷批注，保持幂等，不重复累加。
        """
        state = self.state(allocation.container)
        recorded = allocation
        already_marked = (
            allocation.state is AllocationState.DEFECTIVE_HISTORICAL
            and any(n.startswith("[") for n in allocation.notes)
        )
        if findings and not already_marked:
            recorded = Allocation(
                **{**allocation.__dict__, "state": AllocationState.DEFECTIVE_HISTORICAL,
                   "notes": allocation.notes + tuple(
                       f"[{f.code.value}] {f.message}" for f in findings
                   )}
            )
        state.committed += recorded.volume
        state.outstanding += recorded.volume
        state.allocations[recorded.id] = recorded
        return recorded

    def apply_lab_event(
        self,
        container_id: str,
        volume_used: int | None,
        volume_loss: int | None,
        executed: bool,
        source: str,
        volume_remaining: int | None = None,
    ) -> tuple[list[Finding], int | None]:
        state = self.state(container_id)
        findings: list[Finding] = []
        if volume_loss == -1:
            # 原文只报告可用余量，"其余损耗"= 当前在发量 - 已用量 - 可用余量
            usable = volume_remaining or 0
            volume_loss = max(0, state.outstanding - (volume_used or 0) - usable)
        if volume_used:
            if volume_used > state.outstanding:
                findings.append(
                    Finding(
                        FindingCode.INVENTORY_SHORTAGE,
                        f"{source} 报告 {container_id} 消耗 {volume_used}μL，"
                        f"但在发未分析仅 {state.outstanding}μL，报告与台账不符，须库管核对",
                        (source,),
                    )
                )
            state.consumed += volume_used
            state.committed = max(0, state.committed - volume_used)
            state.outstanding = max(0, state.outstanding - volume_used)
        if volume_loss:
            if volume_loss > state.outstanding:
                findings.append(
                    Finding(
                        FindingCode.INVENTORY_SHORTAGE,
                        f"{source} 报告 {container_id} 损耗 {volume_loss}μL，超出在发量，须核对",
                        (source,),
                    )
                )
            state.lost += volume_loss
            state.committed = max(0, state.committed - volume_loss)
            state.outstanding = max(0, state.outstanding - volume_loss)
        state.lab_sources.append(source)
        return findings, volume_loss

    def apply_return(self, container_id: str, volume: int, source: str) -> Finding | None:
        state = self.state(container_id)
        if volume > state.outstanding:
            return Finding(
                FindingCode.INVENTORY_SHORTAGE,
                f"{source} 归还 {volume}μL 超出 {container_id} 在发量 {state.outstanding}μL，须核对",
                (source,),
            )
        state.committed = max(0, state.committed - volume)
        state.outstanding = max(0, state.outstanding - volume)
        state.returned_pending += volume
        state.return_sources.append(source)
        return None

    def apply_reverify(self, container_id: str, volume: int) -> Finding | None:
        """旧分配余样经新依据重新核对后，归还量转为可再承诺的可用量。"""
        state = self.state(container_id)
        if volume > state.returned_pending:
            return Finding(
                FindingCode.INVENTORY_SHORTAGE,
                f"{container_id} 待核对归还量仅 {state.returned_pending}μL，不能核对 {volume}μL",
            )
        state.returned_pending -= volume
        return None

    def apply_allocation_state(self, allocation_id: str, new_state: AllocationState) -> None:
        for state in self._states.values():
            if allocation_id in state.allocations:
                old = state.allocations[allocation_id]
                state.allocations[allocation_id] = Allocation(
                    **{**old.__dict__, "state": new_state}
                )
                # 收回/停止后释放承诺体积（物理样本仍登记为剩余，不凭空消失）；
                # 停止时把尚未登记待核对的物理剩余转入待核对，等待新依据重新核对
                # （已通过归还事件登记的 returned_pending 不重复计数）
                if new_state in (AllocationState.RECALLED, AllocationState.STOPPED_PENDING):
                    state.committed = max(0, state.committed - old.volume)
                    state.outstanding = max(0, state.outstanding - old.volume)
                    if new_state is AllocationState.STOPPED_PENDING:
                        unaccounted = max(0, state.physical_remaining - state.returned_pending)
                        state.returned_pending += unaccounted
                return
        raise InventoryError(f"未知分配 {allocation_id}")

    # ------------------------------------------------------------- summaries
    def donor_inventory(self, donor: str) -> dict[str, ContainerState]:
        return {cid: s for cid, s in self._states.items() if self.donor_of(cid) == donor}

    def availability_report(self) -> list[dict]:
        rows = []
        for cid, state in sorted(self._states.items()):
            row = state.to_dict()
            row["lineage"] = self.lineage(cid)
            row["allocations"] = [
                {"id": a.id, "research": a.research, "volume": a.volume, "state": a.state.value}
                for a in state.allocations.values()
            ]
            rows.append(row)
        return rows
