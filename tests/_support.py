"""测试共用夹具：每个用例使用独立临时账本，从资料包重建服务。"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from biobank.model import FullDate  # noqa: E402
from biobank.service import BiobankService  # noqa: E402

CASE_PATH = ROOT / "data" / "case.json"
TODAY = FullDate("2026-11-26")
C3_DATE = FullDate("2026-08-05")
WITHDRAWAL_DATE = FullDate("2026-08-03")
ISSUE_DATE = FullDate("2026-08-02")


class ServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ledger_path = str(Path(self._tmp.name) / "ledger.jsonl")
        self.svc = BiobankService(self.ledger_path)
        self.svc.load_pack(CASE_PATH)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def claim_texts(self, record_id: str) -> list[str]:
        return [c.text for c in self.svc.extraction.claims if c.record_id == record_id]

    def finding_codes(self, findings) -> set[str]:
        return {f.code.value if hasattr(f.code, "value") else f["code"] for f in findings}
