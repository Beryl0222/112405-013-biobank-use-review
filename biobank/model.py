"""领域枚举与值对象。

设计原则：
- 一切结论都带 ``sources``（原句编号），没有"无出处的业务事实"。
- 用途区分四个桶：背景 / 拟执行操作 / 已有结果 / 将来共享计划，互不混用。
- 同意、伦理决定、分配、实验事实分别建模，伦理意见永远不是授权。
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any


class Modality(enum.Enum):
    """分析模态。主题词相近不等于模态相同（如代谢 ≠ 测序）。"""

    METABOLIC = "metabolic"
    TRANSCRIPT = "transcript"
    WGS = "wgs"  # 全基因组测序
    OTHER = "other"

    @property
    def label(self) -> str:
        return {
            Modality.METABOLIC: "代谢检测",
            Modality.TRANSCRIPT: "转录分析",
            Modality.WGS: "全基因组测序",
            Modality.OTHER: "未归类分析",
        }[self]


class ProductKind(enum.Enum):
    """产物/结果类型。"""

    IDENTIFIABLE_VARIANTS = "identifiable_variants"  # 可识别变异表/个体结果
    DEIDENTIFIED_SUMMARY = "deidentified_summary"  # 去身份汇总统计
    RAW_SAMPLE_DERIVATIVE = "raw_sample_derivative"  # 样本派生物（分装不产生新同意）

    @property
    def label(self) -> str:
        return {
            ProductKind.IDENTIFIABLE_VARIANTS: "可识别个体结果/变异表",
            ProductKind.DEIDENTIFIED_SUMMARY: "去身份汇总统计",
            ProductKind.RAW_SAMPLE_DERIVATIVE: "样本派生物",
        }[self]


class PurposeBucket(enum.Enum):
    """用途四桶：资料合成时必须辨认陈述落在哪一桶。"""

    BACKGROUND = "background"  # 研究背景（不构成实际操作）
    INTENDED = "intended"  # 拟执行操作
    EXISTING_RESULT = "existing_result"  # 已有结果
    FUTURE_SHARING = "future_sharing"  # 将来共享计划

    @property
    def label(self) -> str:
        return {
            PurposeBucket.BACKGROUND: "研究背景",
            PurposeBucket.INTENDED: "拟执行操作",
            PurposeBucket.EXISTING_RESULT: "已有结果",
            PurposeBucket.FUTURE_SHARING: "将来共享计划",
        }[self]


class ClaimType(enum.Enum):
    """原文陈述的归类。"""

    PROPOSAL_OP = "proposal_op"
    AMENDMENT_OP = "amendment_op"
    BACKGROUND = "background"
    SHARING_PLAN = "sharing_plan"
    GRANT = "grant"
    WITHDRAWAL = "withdrawal"
    ETHICS = "ethics"
    LAB_EVENT = "lab_event"
    RETURN_EVENT = "return_event"
    UNPARSED = "unparsed"


class FindingCode(enum.Enum):
    """资格审查结论码。全部为保守口径：欠缺即拦截。"""

    OK = "ok"
    NO_GRANT = "no_grant"  # 无任何覆盖该模态的授权
    EXCLUDED = "excluded"  # 授权明文排除该模态/产物
    EXPIRED = "expired"  # 授权在办理时点已失效
    WITHDRAWN = "withdrawn"  # 用途已被撤回
    RE_CONTACT_BARRED = "re_contact_barred"  # 再联系限制：需另行联系才能取得的输入不合法
    MATERIAL_SCOPE = "material_scope"  # 授权只限特定物料（如余样），当前物料不匹配
    SHARING_NOT_COVERED = "sharing_not_covered"  # 产物共享未被授权
    INVENTORY_SHORTAGE = "inventory_shortage"  # 已核对可用体积不足
    DOUBLE_COMMIT = "double_commit"  # 同一有限体积不能在两个研究重复承诺
    UNRESOLVED_AMBIGUITY = "unresolved_ambiguity"  # 歧义未经伦理确认
    UNAPPROVED_ANCILLARY = "unapproved_ancillary"  # 未获准分析包装成附属步骤
    MIXING_REFUSED = "mixing_refused"  # 方案明确拒绝混入其他捐赠者补量
    HISTORICAL_DEFECT = "historical_defect"  # 历史办理在当时依据不足（只记录不改写历史）
    DATE_UNKNOWN = "date_unknown"  # 原文无日期，时间线只能保守判断


class OperationStatus(enum.Enum):
    """操作按生命周期的状态。"""

    PROPOSED = "proposed"  # 方案提出，未审查
    APPROVED = "approved"  # 伦理就该操作批准（且授权齐备）
    PARTIAL = "partial"  # 方案部分操作获准，其余不获准
    WITHHELD = "withheld"  # 欠缺依据，该研究部分暂缓
    STOPPED = "stopped"  # 撤回后已发未分析部分须停止
    EXECUTED = "executed"  # 实验已实际发生
    PARTIALLY_EXECUTED = "partially_executed"  # 部分执行，剩余停止/待核


class AllocationState(enum.Enum):
    """容器分配状态（库管视角）。"""

    ISSUED = "issued"  # 已发放
    HELD = "held"  # 已承诺保留（未发），占用体积
    RECALLED = "recalled"  # 撤回：未发分配收回
    STOPPED_PENDING = "stopped_pending"  # 已发未分析部分停止使用，待确认剩余
    RETURNED = "returned"  # 研究者归还
    CONSUMED = "consumed"  # 已消耗
    PARTIALLY_CONSUMED = "partially_consumed"
    REVERIFIED = "reverified"  # 旧分配余样经新依据重新核对
    DEFECTIVE_HISTORICAL = "defective_historical"  # 历史发放依据不足的标注态


@dataclass(frozen=True)
class FullDate:
    """日期值对象。``inferred`` 表示日期由上下文推定而非原文直述。"""

    iso: str
    inferred: bool = False

    def key(self) -> tuple[int, int, int]:
        y, m, d = (int(x) for x in self.iso.split("-"))
        return y, m, d

    def __lt__(self, other: "FullDate") -> bool:
        return self.key() < other.key()

    def __le__(self, other: "FullDate") -> bool:
        return self.key() <= other.key()

    def __str__(self) -> str:
        return self.iso + ("（推定）" if self.inferred else "")

    def to_dict(self) -> dict[str, Any]:
        return {"iso": self.iso, "inferred": self.inferred}


@dataclass(frozen=True)
class Claim:
    """从一条原文中抽取的一条陈述。

    未解析的原文以 :class:`ClaimType.UNPARSED` 保留，并带
    :data:`FindingCode.UNRESOLVED_AMBIGUITY`，系统不得静默丢弃。
    """

    record_id: str
    kind: str  # 原文 kind（proposal/consent/...）
    claim_type: ClaimType
    text: str  # 命中的原句，而非摘要
    date: FullDate | None = None
    donor: str | None = None
    research: str | None = None
    modality: Modality | None = None
    bucket: PurposeBucket | None = None
    product: ProductKind | None = None
    # 授权语义
    signer: str | None = None  # donor | guardian | unknown
    includes: tuple[Modality, ...] = ()
    excludes: tuple[Modality, ...] = ()
    grant_expiry: FullDate | None = None  # 代签有效期
    re_contact_allowed: bool | None = None
    material_scope: str | None = None  # 如 "remnant"（余样）
    sharing_allowed: dict[ProductKind, bool] = field(default_factory=dict)
    # 撤回语义
    withdraw_modalities: tuple[Modality, ...] = ()
    withdraw_future_sharing: bool = False
    keep_completed_results: bool = False
    # 实验/库存事实
    container: str | None = None
    volume_used: int | None = None
    volume_remaining: int | None = None
    volume_loss: int | None = None
    executed: bool | None = None
    # 伦理语义
    ethics_text: str | None = None
    ethics_clarifies: str | None = None
    ambiguity_note: str | None = None
    unparsed: bool = False

    @property
    def sources(self) -> tuple[str, ...]:
        return (self.record_id,)


@dataclass(frozen=True)
class Donor:
    id: str
    adult_on: FullDate | None  # None 表示登记未提供成年日期

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Donor":
        return cls(id=d["id"], adult_on=FullDate(d["adult_on"]) if d.get("adult_on") else None)


@dataclass(frozen=True)
class ContainerRecord:
    """容器登记。parent 表示物料派生，不表示用途获准。"""

    id: str
    donor: str | None
    parent: str | None
    volume: int
    sources: tuple[str, ...]

    @classmethod
    def from_dict(cls, d: dict[str, Any], sources: tuple[str, ...]) -> "ContainerRecord":
        return cls(
            id=d["id"],
            donor=d.get("donor"),
            parent=d.get("parent"),
            volume=d["volume"],
            sources=tuple(sources),
        )


@dataclass(frozen=True)
class OperationRecord:
    """方案登记的操作（拟执行），consumes 是拟用量而非已批准消耗。"""

    id: str
    research: str
    source: str
    consumes: int

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "OperationRecord":
        return cls(id=d["id"], research=d["research"], source=d["source"], consumes=d["consumes"])


@dataclass(frozen=True)
class Grant:
    """授权的规范化视图（由 consent 类陈述合成，不丢失原句）。"""

    donor: str
    signer: str  # donor | guardian
    modalities: tuple[Modality, ...]
    excludes: tuple[Modality, ...]
    granted_on: FullDate | None
    expiry: FullDate | None
    re_contact_allowed: bool | None
    material_scope: str | None
    sharing_allowed: dict[ProductKind, bool]
    sources: tuple[str, ...]


@dataclass(frozen=True)
class Withdrawal:
    donor: str
    on: FullDate | None
    modalities: tuple[Modality, ...]  # 空元组表示原文列明的具体用途（此处按模态登记）
    future_sharing_blocked: bool
    keep_completed_results: bool
    sources: tuple[str, ...]


@dataclass(frozen=True)
class Clarification:
    """伦理对歧义限制的确认。只能解释限制，不能代替无资格者签同意。"""

    research: str | None
    note: str
    on: FullDate | None
    sources: tuple[str, ...]


@dataclass(frozen=True)
class Finding:
    code: FindingCode
    message: str
    sources: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.code is FindingCode.OK

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code.value, "message": self.message, "sources": list(self.sources)}


@dataclass(frozen=True)
class Basis:
    """一次决定或实验发生时所依据的材料快照（不可变）。"""

    decision_date: FullDate
    grant_sources: tuple[str, ...]
    ethics_sources: tuple[str, ...]
    proposal_sources: tuple[str, ...]
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_date": self.decision_date.to_dict(),
            "grant_sources": list(self.grant_sources),
            "ethics_sources": list(self.ethics_sources),
            "proposal_sources": list(self.proposal_sources),
            "notes": list(self.notes),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Basis":
        return cls(
            decision_date=FullDate(**d["decision_date"]),
            grant_sources=tuple(d["grant_sources"]),
            ethics_sources=tuple(d["ethics_sources"]),
            proposal_sources=tuple(d["proposal_sources"]),
            notes=tuple(d.get("notes", ())),
        )


@dataclass(frozen=True)
class Decision:
    """伦理就单个操作的决定。部分批准时每个操作各有一条。"""

    operation_id: str
    research: str
    modality: Modality
    approved: bool
    on: FullDate
    findings: tuple[Finding, ...]
    basis: Basis
    ethics_officer: str = "officer"  # 合成演练占位，不对应真实资质

    @property
    def status(self) -> OperationStatus:
        return OperationStatus.APPROVED if self.approved else OperationStatus.WITHHELD


@dataclass(frozen=True)
class Allocation:
    """库管按容器办理的一次分配/承诺。"""

    id: str
    container: str
    donor: str
    research: str
    operation_id: str
    volume: int
    state: AllocationState
    opened_on: FullDate
    basis: Basis
    sources: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "container": self.container,
            "donor": self.donor,
            "research": self.research,
            "operation_id": self.operation_id,
            "volume": self.volume,
            "state": self.state.value,
            "opened_on": self.opened_on.to_dict(),
            "basis": self.basis.to_dict(),
            "sources": list(self.sources),
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class ExecutionRecord:
    """实验事实（发生即不可假装未发生）。"""

    operation_id: str
    research: str
    container: str
    modality: Modality
    volume_used: int
    on: FullDate | None
    basis_at_time: Basis  # 实验发生时所依据的材料快照
    findings_at_time: tuple[Finding, ...]  # 当时的依据缺陷一并留存
    sources: tuple[str, ...]


@dataclass(frozen=True)
class SharingRequest:
    """结果共享请求：共享仍受原文单独限制。"""

    research: str
    product: ProductKind
    target: str  # 合作方 | 公开 | 内部
    on: FullDate
    sources: tuple[str, ...]
