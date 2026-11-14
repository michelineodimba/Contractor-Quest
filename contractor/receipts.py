import json
from pathlib import Path
from typing import List

from .models import Receipt


class ReceiptStore:
    def __init__(self, directory: Path):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.receipts: List[Receipt] = []

    def add(self, r: Receipt) -> None:
        self.receipts.append(r)
        with open(self.dir / "receipts.jsonl", "a") as f:
            f.write(json.dumps(r.to_dict()) + "\n")

    def write_markdown(self, currency: str, budget: str, spent: str, outstanding: str) -> Path:
        lines = ["# Receipts", "", f"Budget {budget} {currency} | spent {spent} | held in escrow/disputes {outstanding}", "",
                 "| Part | Bought from | Quoted | Paid (incl. fee) | Check | Status |", "|---|---|---|---|---|---|"]
        for r in self.receipts:
            chk = "passed" if r.check.get("passed") else "FAILED"
            lines.append(f"| {r.part_id} | {r.seller} | {r.quoted or '-'} | {r.total_paid} | {chk} | {r.status} |")
        for r in self.receipts:
            lines += ["", f"## {r.part_id}: {r.title}", f"- status: {r.status}; kind: {r.kind}; seller: {r.seller}",
                      f"- price {r.final_price} + fee {r.fee}; paid out {r.total_paid} {r.currency}",
                      f"- market job: {r.market_job_id}; bid: {r.bid_id}",
                      f"- check ({r.check.get('method')}): {'passed' if r.check.get('passed') else 'FAILED'}; "
                      f"{'; '.join(r.check.get('reasons', []))}"]
            for s in r.negotiation:
                lines.append(f"- negotiation: {s['action']} {s.get('amount') or s.get('offered') or ''} {s.get('reason', '')}".rstrip())
            for p in r.payments:
                lines.append(f"- payment: {p['amount']} to {p['to']} op={p['op_id']} status={p['status']}")
            for n in r.notes:
                lines.append(f"- note: {n}")
        path = self.dir / "receipts.md"
        path.write_text("\n".join(lines) + "\n")
        return path
