"""把合成中文原文抽取为带出处的结构化陈述。

口径（与 docs/materials.md 一致）：
- 只做规则化资料合成，不做业务批准；识别不了的句子保留为 UNPARSED 并挂歧义。
- 每条 Claim 保留命中的原句与原文编号，解释确认必须能对应具体原句版本。
- 编号顺序不是效力优先级；后补资料以独立编号保留，由服务层按时间线处理。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from . import dates
from .model import (
    Claim,
    ClaimType,
    ContainerRecord,
    Donor,
    Modality,
    OperationRecord,
    ProductKind,
    PurposeBucket,
)

DEFAULT_YEAR = 2026

_RESEARCH_RE = re.compile(r"R\d+")
_DONOR_RE = re.compile(r"D\d+")
_CONTAINER_RE = re.compile(r"T\d+[A-Z]?")
_VOLUME_RE = re.compile(r"(\d+)\s*微升")

# 模态词表：顺序即优先级，先匹配更具体的全基因组/测序；
# "遗传风险"只在背景语境出现，映射到 WGS 是为了识别背景假说，不等于测序操作
_MODALITY_TERMS: tuple[tuple[Modality, tuple[str, ...]], ...] = (
    (Modality.WGS, ("全基因组", "基因组测序", "测序", "遗传")),
    (Modality.TRANSCRIPT, ("转录",)),
    (Modality.METABOLIC, ("代谢",)),
)

_PRODUCT_TERMS: tuple[tuple[ProductKind, tuple[str, ...]], ...] = (
    (ProductKind.IDENTIFIABLE_VARIANTS, ("可识别变异表", "个体变异表", "个体结果", "变异表")),
    (ProductKind.DEIDENTIFIED_SUMMARY, ("去身份的汇总", "去身份", "汇总统计", "汇总论文")),
)

_SENT_SPLIT_RE = re.compile(r"[。；;]")


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENT_SPLIT_RE.split(text) if s.strip()]


def detect_modality(text: str) -> Modality | None:
    for modality, terms in _MODALITY_TERMS:
        if any(t in text for t in terms):
            return modality
    return None


def detect_all_modalities(text: str) -> tuple[Modality, ...]:
    found = [m for m, terms in _MODALITY_TERMS if any(t in text for t in terms)]
    return tuple(found)


def detect_product(text: str) -> ProductKind | None:
    for product, terms in _PRODUCT_TERMS:
        if any(t in text for t in terms):
            return product
    return None


def _first(pattern: re.Pattern[str], text: str) -> str | None:
    m = pattern.search(text)
    return m.group(0) if m else None


@dataclass
class ExtractionResult:
    case_id: str
    claims: list[Claim]
    donors: dict[str, Donor]
    containers: dict[str, ContainerRecord]
    operations: dict[str, OperationRecord]
    records: dict[str, dict[str, str]]
    warnings: list[str]

    def claims_of(self, claim_type: ClaimType) -> list[Claim]:
        return [c for c in self.claims if c.claim_type is claim_type]

    def claims_for_record(self, record_id: str) -> list[Claim]:
        return [c for c in self.claims if c.record_id == record_id]


def extract(case: dict, default_year: int = DEFAULT_YEAR) -> ExtractionResult:
    records = {r["id"]: r for r in case["records"]}
    donors = {d["id"]: Donor.from_dict(d) for d in case["objects"].get("donors", [])}
    containers = {
        c["id"]: ContainerRecord.from_dict(c, sources=tuple(c.get("sources", ())))
        for c in case["objects"].get("containers", [])
    }
    operations = {o["id"]: OperationRecord.from_dict(o) for o in case["objects"].get("operations", [])}

    claims: list[Claim] = []
    warnings: list[str] = []
    for record in case["records"]:
        produced = _extract_record(record, donors, default_year)
        if not produced:
            produced = [
                Claim(
                    record_id=record["id"],
                    kind=record["kind"],
                    claim_type=ClaimType.UNPARSED,
                    text=record["text"],
                    unparsed=True,
                    ambiguity_note="规则未能解析该原文，须人工阅读后再办理",
                )
            ]
            warnings.append(f"{record['id']} 未能自动解析，已挂歧义")
        claims.extend(produced)

    return ExtractionResult(
        case_id=case["case_id"],
        claims=claims,
        donors=donors,
        containers=containers,
        operations=operations,
        records=records,
        warnings=warnings,
    )


def _extract_record(record: dict, donors: dict[str, Donor], default_year: int) -> list[Claim]:
    kind = record["kind"]
    rid = record["id"]
    text = record["text"]
    if kind in ("proposal", "amendment"):
        return _extract_proposal(rid, kind, text, default_year)
    if kind == "consent":
        return _extract_consent(rid, text, donors, default_year)
    if kind == "ethics":
        return _extract_ethics(rid, text, default_year)
    if kind == "withdrawal":
        return _extract_withdrawal(rid, text, default_year)
    if kind == "lab":
        return _extract_lab(rid, text, default_year)
    if kind == "return":
        return _extract_return(rid, text, default_year)
    return []


def _base(rid: str, kind: str, sent: str, **kw) -> Claim:
    return Claim(record_id=rid, kind=kind, text=sent, **kw)


def _extract_proposal(rid: str, kind: str, text: str, default_year: int) -> list[Claim]:
    ctype = ClaimType.PROPOSAL_OP if kind == "proposal" else ClaimType.AMENDMENT_OP
    research = _first(_RESEARCH_RE, text)
    donor = _first(_DONOR_RE, text)
    out: list[Claim] = []
    for sent in _sentences(text):
        date = dates.first_date(sent, default_year)
        if "背景" in sent:
            out.append(
                _base(
                    rid, kind, sent,
                    claim_type=ClaimType.BACKGROUND,
                    date=date, research=research,
                    modality=detect_modality(sent),
                    bucket=PurposeBucket.BACKGROUND,
                    ambiguity_note="背景陈述不构成实际操作，不得据此视为测序获准",
                )
            )
            continue
        # 共享/外发计划先按整句判定（句内可能还有逗号子句）；
        # 共享句里出现的模态词（如"代谢结果用于论文"）是产物用途，不另立操作
        is_sharing_sentence = any(w in sent for w in ("发合作方", "外发", "公开", "共享", "论文"))
        if is_sharing_sentence:
            product = detect_product(sent)
            if product is not None:
                denied = any(w in sent for w in ("不外发", "不接受", "须另行同意", "不得"))
                out.append(
                    _base(
                        rid, kind, sent,
                        claim_type=ClaimType.SHARING_PLAN,
                        date=date, research=research, donor=donor,
                        product=product,
                        bucket=PurposeBucket.FUTURE_SHARING,
                        sharing_allowed={product: not denied},
                    )
                )
            if not any(w in sent for w in ("测", "分析", "测序")):
                continue
        # 修订中"取消 X，改做 Y"：取消词之后、"改做"之前的模态为被取消项
        if "取消" in sent:
            cut = sent.index("取消") + 2
            pivot = sent.find("改做", cut)
            cancelled_part = sent[cut:pivot] if pivot >= 0 else sent[cut:]
            cancelled = detect_all_modalities(cancelled_part)
            for modality in cancelled:
                out.append(
                    _base(
                        rid, kind, sent,
                        claim_type=ctype, date=date, research=research,
                        modality=modality, bucket=PurposeBucket.INTENDED,
                        ambiguity_note="修订取消该操作，后续办理以修订为准",
                    )
                )
            after = sent[pivot + 2:] if pivot >= 0 else sent[cut:]
            # "改做"之后到下一个逗号（通常接共享/结果用途句）为止才是新操作
            active_part = after.split("，")[0]
            for modality in detect_all_modalities(active_part):
                out.append(
                    _base(
                        rid, kind, sent,
                        claim_type=ctype, date=date, research=research, donor=donor,
                        modality=modality, bucket=PurposeBucket.INTENDED,
                    )
                )
            continue
        if "混入" in sent:
            out.append(
                _base(
                    rid, kind, sent,
                    claim_type=ctype, date=date, research=research, donor=donor,
                    modality=detect_modality(sent), bucket=PurposeBucket.INTENDED,
                    ambiguity_note="方案明文拒绝混入其他捐赠者样本补量",
                )
            )
            continue
        # 普通意图句（含共享句里的操作部分）：按逗号子句逐个识别模态
        for clause in [s.strip() for s in sent.split("，") if s.strip()]:
            modality = detect_modality(clause)
            if modality is not None and any(w in clause for w in ("测", "分析", "做", "测序", "用于")):
                out.append(
                    _base(
                        rid, kind, clause,
                        claim_type=ctype, date=date, research=research, donor=donor,
                        modality=modality, bucket=PurposeBucket.INTENDED,
                    )
                )
    return _dedupe_intentions(out)


def _dedupe_intentions(claims: list[Claim]) -> list[Claim]:
    seen: set[tuple] = set()
    out: list[Claim] = []
    for claim in claims:
        key = (claim.record_id, claim.claim_type, claim.modality, claim.text)
        if key in seen:
            continue
        seen.add(key)
        out.append(claim)
    return out


def _extract_consent(rid: str, text: str, donors: dict[str, Donor], default_year: int) -> list[Claim]:
    donor = _first(_DONOR_RE, text)
    research = _first(_RESEARCH_RE, text)
    date = dates.first_date(text, default_year)
    # 签署人资格：本人主动补充同意优先于"同意"字样；未知资格挂歧义待伦理确认
    if "监护人" in text or "代签" in text:
        signer = "guardian"
    elif "本人" in text or (donor and "补充同意" in text):
        signer = "donor"
    else:
        signer = "unknown"
    # 在全文范围内按模态判断正/反义：否定词只作用于它所在的排除表述
    includes: list[Modality] = []
    excludes: list[Modality] = []
    for modality, terms in _MODALITY_TERMS:
        hit = next((t for t in terms if t in text), None)
        if hit is None:
            continue
        idx = text.index(hit)
        window = text[max(0, idx - 6): idx + len(hit) + 2]
        negative = any(w in window for w in ("不包括", "不含", "不同意", "不包含"))
        (excludes if negative else includes).append(modality)
    includes = list(dict.fromkeys(includes))
    excludes = list(dict.fromkeys(excludes))

    grant_expiry = None
    if "成年后" in text and donor and donor in donors and donors[donor].adult_on is not None:
        grant_expiry = dates.adult_plus_years(donors[donor].adult_on, text)

    re_contact: bool | None = None
    if "不允许研究者再次直接联系" in text or "再次直接联系" in text:
        re_contact = False
    elif "可再次联系" in text:
        re_contact = True

    sharing: dict[ProductKind, bool] = {}
    for sent in _sentences(text):
        # 同一句可能并列多种产物（去身份汇总可公开；个体结果须另行同意），
        # 按逗号子句逐产物判断，否定词在同一子句内即构成限制
        for clause in [s.strip() for s in sent.split("，") if s.strip()]:
            for product, terms in _PRODUCT_TERMS:
                hit = next((t for t in terms if t in clause), None)
                if hit is None:
                    continue
                denied = any(
                    w in clause for w in ("须另行同意", "不同意", "不外发", "不得", "不包括")
                )
                sharing[product] = not denied

    material_scope = "remnant" if "余样" in text else None
    ambiguity = None
    if signer == "unknown":
        ambiguity = "原文未明述签署人，按本人补充同意登记，仍须伦理确认签署资格"
    if "补充同意" in text and re_contact is False:
        ambiguity = (ambiguity + "；" if ambiguity else "") + \
            "再联系限制针对研究者主动联系；捐赠者主动补充同意不构成违规联系"

    return [
        _base(
            rid, "consent", text,
            claim_type=ClaimType.GRANT,
            date=date, donor=donor, research=research,
            signer=signer,
            includes=tuple(includes), excludes=tuple(excludes),
            grant_expiry=grant_expiry,
            re_contact_allowed=re_contact,
            material_scope=material_scope,
            sharing_allowed=sharing,
            ambiguity_note=ambiguity,
        )
    ]


def _extract_ethics(rid: str, text: str, default_year: int) -> list[Claim]:
    research = _first(_RESEARCH_RE, text)
    date = dates.first_date(text, default_year)
    return [
        _base(
            rid, "ethics", text,
            claim_type=ClaimType.ETHICS,
            date=date, research=research,
            ethics_text=text,
            ethics_clarifies="背景遗传风险不等于实际测序；未授权用途不获批准",
            ambiguity_note="伦理意见可确认歧义限制，但不能代替无资格者签署同意",
        )
    ]


def _extract_withdrawal(rid: str, text: str, default_year: int) -> list[Claim]:
    donor = _first(_DONOR_RE, text)
    date = dates.first_date(text, default_year)
    # 模态按整段识别（句读可能把"撤回今后代谢实验"切开）
    modalities = detect_all_modalities(text)
    keep_results = "已完成结果可保存" in text or "已完成" in text
    block_sharing = ("不得" in text and "共享" in text) or "不得新增对外共享" in text
    return [
        _base(
            rid, "withdrawal", text,
            claim_type=ClaimType.WITHDRAWAL,
            date=date, donor=donor,
            withdraw_modalities=tuple(modalities),
            withdraw_future_sharing=block_sharing,
            keep_completed_results=keep_results,
        )
    ]


def _extract_lab(rid: str, text: str, default_year: int) -> list[Claim]:
    research = _first(_RESEARCH_RE, text)
    date = dates.first_date(text, default_year)
    out: list[Claim] = []
    units: list[str] = []
    for sent in _sentences(text):
        units.extend(s.strip() for s in sent.split("，") if s.strip())
    # 同一容器的未启动子句合并为一条事实（"可用30，其余损耗"），完成事实单独成条
    pending: dict[str, dict] = {}
    for clause in units:
        container = _first(_CONTAINER_RE, clause) or _first(_CONTAINER_RE, text)
        if container is None:
            continue
        volumes = [int(v) for v in _VOLUME_RE.findall(clause)]
        modality = detect_modality(clause) or detect_modality(text)
        not_started = any(w in clause for w in ("未分析", "尚未", "未动"))
        executed = not not_started and any(w in clause for w in ("完成", "已用"))
        if executed:
            volume_remaining = None
            if "余" in text:
                tail = text[text.index("余"):]
                tail_volumes = [int(v) for v in _VOLUME_RE.findall(tail)]
                volume_remaining = tail_volumes[0] if tail_volumes else None
            out.append(
                _base(
                    rid, "lab", clause,
                    claim_type=ClaimType.LAB_EVENT, date=date, research=research,
                    container=container, modality=modality,
                    volume_used=volumes[0] if volumes else None,
                    volume_remaining=volume_remaining, volume_loss=None,
                    executed=True, bucket=PurposeBucket.EXISTING_RESULT,
                )
            )
            continue
        entry = pending.setdefault(
            container,
            {"texts": [], "volume_remaining": None, "volume_loss": None, "modality": modality},
        )
        entry["texts"].append(clause)
        if volumes and entry["volume_remaining"] is None:
            entry["volume_remaining"] = volumes[0]
        if "损耗" in clause:
            if len(volumes) > 1:
                entry["volume_loss"] = volumes[1]
            elif entry["volume_remaining"] is not None:
                # 文本只写"实际可用30，其余损耗"时，损耗差值由服务层结合在发台账量计算
                entry["volume_loss"] = -1  # 哨兵：交由 InventoryService 按在发量结算
    for container, entry in pending.items():
        if entry["volume_remaining"] is None and entry["volume_loss"] is None:
            continue
        out.append(
            _base(
                rid, "lab", "，".join(entry["texts"]),
                claim_type=ClaimType.LAB_EVENT, date=date, research=research,
                container=container, modality=entry["modality"],
                volume_used=None,
                volume_remaining=entry["volume_remaining"],
                volume_loss=entry["volume_loss"],
                executed=False,
            )
        )
    return out


def _extract_return(rid: str, text: str, default_year: int) -> list[Claim]:
    research = _first(_RESEARCH_RE, text)
    container = _first(_CONTAINER_RE, text)
    volumes = [int(v) for v in _VOLUME_RE.findall(text)]
    date = dates.first_date(text, default_year)
    return [
        _base(
            rid, "return", text,
            claim_type=ClaimType.RETURN_EVENT,
            date=date, research=research, container=container,
            volume_remaining=volumes[0] if volumes else None,
            ambiguity_note="归还余样原容器号保留，用于新方案前须重新核对",
        )
    ]
