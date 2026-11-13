"""Acceptance checks. Deterministic checks decide; an optional LLM judge may only veto, never approve alone."""
import hashlib
import json
import re
from typing import Optional, Protocol

from .models import CheckResult, Part


class Judge(Protocol):
    def review(self, part: Part, deliverable: str) -> CheckResult: ...


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def run_checks(part: Part, deliverable: str, proof_sha256: Optional[str] = None,
               judge: Optional[Judge] = None) -> CheckResult:
    reasons = []
    c = part.checks or {}
    text = deliverable or ""
    low = text.lower()

    if not text.strip():
        reasons.append("deliverable is empty")
    if proof_sha256 and proof_sha256.lower() != sha256_text(text):
        reasons.append("hash proof does not match the delivered content")
    words = len(re.findall(r"\w+", text))
    if "min_words" in c and words < int(c["min_words"]):
        reasons.append(f"too short: {words} words < {c['min_words']}")
    if "max_words" in c and words > int(c["max_words"]):
        reasons.append(f"too long: {words} words > {c['max_words']}")
    for term in c.get("required_terms", []):
        if term.lower() not in low:
            reasons.append(f"missing required term: {term}")
    for term in c.get("forbidden_terms", []):
        if term.lower() in low:
            reasons.append(f"contains forbidden term: {term}")
    if "json_keys" in c:
        try:
            obj = json.loads(text)
            missing = [k for k in c["json_keys"] if not isinstance(obj, dict) or k not in obj]
            if missing:
                reasons.append(f"JSON missing keys: {', '.join(missing)}")
        except ValueError:
            reasons.append("deliverable is not valid JSON")

    if reasons:
        return CheckResult(False, reasons, "deterministic")
    if judge is None:
        return CheckResult(True, ["all deterministic checks passed"], "deterministic")
    verdict = judge.review(part, deliverable)
    return CheckResult(verdict.passed, verdict.reasons, "deterministic+rubric")
