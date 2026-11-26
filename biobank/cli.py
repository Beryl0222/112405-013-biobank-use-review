"""命令行入口：资料载入、按操作审查、按容器办理、角色视图、账本核验。

示例：
  python3 -m biobank.cli -l runtime/ledger.jsonl load data/case.json
  python3 -m biobank.cli -l runtime/ledger.jsonl ethics
  python3 -m biobank.cli -l runtime/ledger.jsonl decide O12 --date 2026-11-26
  python3 -m biobank.cli -l runtime/ledger.jsonl issue O21 T8B 30 --date 2026-11-26
所有输出为资料合成结果，不替代真实伦理审查。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .model import FullDate, Modality, ProductKind
from .service import BiobankService, ServiceError
from .views import Views


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=False))


def _date(value: str | None) -> FullDate | None:
    return FullDate(value) if value else None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="biobank",
        description="生物样本研究限制与二次使用审查（资料合成，不替代真实伦理审查）",
    )
    parser.add_argument("-l", "--ledger", default="runtime/ledger.jsonl",
                        help="追加式事件账本路径")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("load", help="载入资料包").add_argument("path")

    p_decide = sub.add_parser("decide", help="伦理员按操作给出可批准范围与欠缺依据")
    p_decide.add_argument("operation_id")
    p_decide.add_argument("--date")
    p_decide.add_argument("--donor")
    p_decide.add_argument("--material")

    p_assess = sub.add_parser("assess", help="研究者评估操作可办理性（授权+体积）")
    p_assess.add_argument("operation_id")
    p_assess.add_argument("--date")

    p_issue = sub.add_parser("issue", help="库管按容器发放")
    p_issue.add_argument("operation_id")
    p_issue.add_argument("container")
    p_issue.add_argument("volume", type=int)
    p_issue.add_argument("--date")
    p_issue.add_argument("--allocation-id")

    p_hold = sub.add_parser("hold", help="库管按容器承诺保留（未发）")
    p_hold.add_argument("operation_id")
    p_hold.add_argument("container")
    p_hold.add_argument("volume", type=int)
    p_hold.add_argument("--date")
    p_hold.add_argument("--allocation-id")

    p_lab = sub.add_parser("lab", help="登记新到实验室报告（消耗/损耗/完成）")
    p_lab.add_argument("container")
    p_lab.add_argument("research")
    p_lab.add_argument("modality", choices=[m.value for m in Modality])
    p_lab.add_argument("--used", type=int)
    p_lab.add_argument("--loss", type=int)
    p_lab.add_argument("--executed", action="store_true")
    p_lab.add_argument("--source", required=True)
    p_lab.add_argument("--date")

    p_return = sub.add_parser("return", help="登记研究者归还余样")
    p_return.add_argument("container")
    p_return.add_argument("volume", type=int)
    p_return.add_argument("--source", required=True)
    p_return.add_argument("--date")

    p_reverify = sub.add_parser("reverify", help="旧分配余样凭新依据重新核对")
    p_reverify.add_argument("container")
    p_reverify.add_argument("volume", type=int)
    p_reverify.add_argument("--allocation-id")

    p_withdraw = sub.add_parser("withdraw", help="办理撤回（未发收回/已发未分析停止/结果保留）")
    p_withdraw.add_argument("record_id")

    p_share = sub.add_parser("share", help="结果共享请求判定")
    p_share.add_argument("research")
    p_share.add_argument("donor")
    p_share.add_argument("product", choices=[p.value for p in ProductKind])
    p_share.add_argument("target")
    p_share.add_argument("--date")
    p_share.add_argument("--source", action="append", default=[])

    sub.add_parser("ethics", help="伦理员视图")
    sub.add_parser("custodian", help="库管视图")
    p_rv = sub.add_parser("researcher", help="研究者视图")
    p_rv.add_argument("research")
    p_dv = sub.add_parser("donor", help="捐赠者视图")
    p_dv.add_argument("donor")
    sub.add_parser("verify", help="账本完整性核验")
    sub.add_parser("events", help="列出账本事件")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    service = BiobankService(args.ledger)
    views = Views(service)
    try:
        if args.command == "load":
            _print(service.load_pack(Path(args.path)))
        elif args.command == "decide":
            decision = service.decide_operation(
                args.operation_id, _date(args.date), args.donor, args.material
            )
            _print({
                "operation_id": decision.operation_id,
                "research": decision.research,
                "modality": decision.modality.value,
                "approved": decision.approved,
                "status": decision.status.value,
                "on": decision.on.to_dict(),
                "findings": [f.to_dict() for f in decision.findings],
                "basis": decision.basis.to_dict(),
            })
        elif args.command == "assess":
            _print(service.assess_request(args.operation_id, _date(args.date)))
        elif args.command in ("issue", "hold"):
            method = service.issue if args.command == "issue" else service.hold
            allocation = method(
                args.operation_id, args.container, args.volume,
                _date(args.date), args.allocation_id,
            )
            _print(allocation.to_dict())
        elif args.command == "lab":
            _print(service.record_lab_event(
                args.container, args.research, Modality(args.modality),
                volume_used=args.used, volume_loss=args.loss,
                executed=args.executed, source_id=args.source, day=_date(args.date),
            ))
        elif args.command == "return":
            _print(service.record_return(
                args.container, args.volume, args.source, _date(args.date)))
        elif args.command == "reverify":
            _print(service.reverify(args.container, args.volume, args.allocation_id))
        elif args.command == "withdraw":
            _print(service.process_withdrawal(args.record_id))
        elif args.command == "share":
            _print(service.decide_sharing(
                args.research, args.donor, ProductKind(args.product), args.target,
                _date(args.date), sources=tuple(args.source),
            ))
        elif args.command == "ethics":
            _print(views.ethics_view())
        elif args.command == "custodian":
            _print(views.custodian_view())
        elif args.command == "researcher":
            _print(views.researcher_view(args.research))
        elif args.command == "donor":
            _print(views.donor_view(args.donor))
        elif args.command == "verify":
            _print({"ledger": args.ledger, "intact": service.verify_ledger(),
                    "events": len(service.ledger.events)})
        elif args.command == "events":
            _print([
                {"seq": e.seq, "type": e.event_type, "sources": list(e.sources)}
                for e in service.ledger.events
            ])
    except ServiceError as exc:
        print(json.dumps(
            {"error": str(exc),
             "findings": [f.to_dict() for f in exc.findings]},
            ensure_ascii=False, indent=2), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
