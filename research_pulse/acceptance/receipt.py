"""Sanitization and atomic persistence for acceptance evidence."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import json
import re

from research_pulse.acceptance.models import RealPaperAcceptanceReceipt


MAX_RECEIPT_BYTES = 128_000
_SENSITIVE_PATTERNS = (
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+\-/=]+"),
    re.compile(r"(?i)(?:api[_-]?key|token|password|secret)\s*[:=]\s*[^\s,;]+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"(?i)postgres(?:ql)?://[^\s)]+"),
    re.compile(r"(?i)-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----"),
    re.compile(r"(?<![A-Za-z0-9_])[A-Za-z]:\\(?:Users|Documents and Settings)\\[^\s`]+"),
)


_ERROR_CODES = {
    "preflight": "preflight_unavailable",
    "discovery": "arxiv_unreachable",
    "production": "production_failed",
    "bundle_verify": "bundle_invalid",
    "retrieval_verify": "retrieval_invalid",
    "api_chat_verify": "scoped_answer_insufficient",
    "receipt": "receipt_write_failed",
}


def safe_error(error: Exception, *, stage: str) -> tuple[str, str]:
    """Return an allow-listed code and aggressively redacted short summary."""

    code = _ERROR_CODES.get(stage, "acceptance_failed")
    message = " ".join(str(error).split()) or type(error).__name__
    if stage == "production":
        lowered = message.casefold()
        if "reading_timeout" in lowered:
            code = "reading_timeout"
        elif "provider_timeout" in lowered or "timed out" in lowered:
            code = "provider_timeout"
    for pattern in _SENSITIVE_PATTERNS:
        message = pattern.sub("[REDACTED]", message)
    message = re.sub(r"(?i)https?://[^\s)]+", "[URL]", message)
    message = re.sub(r"(?<![A-Za-z0-9_])/(?:Users|home)/[^\s`]+", "[PATH]", message)
    return code, message[:240]


def assert_sanitized(text: str) -> None:
    if len(text.encode("utf-8")) > MAX_RECEIPT_BYTES:
        raise ValueError("Acceptance receipt exceeds the size limit.")
    for pattern in _SENSITIVE_PATTERNS:
        if pattern.search(text):
            raise ValueError("Acceptance receipt contains sensitive material.")


@dataclass(frozen=True)
class ReceiptPaths:
    json: Path
    markdown: Path


class AcceptanceReceiptWriter:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def write(self, receipt: RealPaperAcceptanceReceipt) -> ReceiptPaths:
        self.root.mkdir(parents=True, exist_ok=True)
        stem = _safe_stem(f"{_timestamp_stem(receipt.started_at)}-{receipt.source_id or receipt.run_id}")
        json_path = self._managed(self.root / f"{stem}.json")
        markdown_path = self._managed(self.root / f"{stem}.md")
        payload = receipt.to_payload()
        json_text = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        markdown_text = _render_markdown(payload)
        assert_sanitized(json_text)
        assert_sanitized(markdown_text)
        self._atomic_write(json_path, json_text, is_json=True)
        try:
            self._atomic_write(markdown_path, markdown_text, is_json=False)
        except Exception:
            json_path.unlink(missing_ok=True)
            raise
        return ReceiptPaths(json=json_path, markdown=markdown_path)

    def _managed(self, path: Path) -> Path:
        resolved = path.resolve(strict=False)
        if not resolved.is_relative_to(self.root):
            raise ValueError("Receipt path escapes the configured output root.")
        return resolved

    def _atomic_write(self, path: Path, text: str, *, is_json: bool) -> None:
        temporary = self._managed(path.with_suffix(path.suffix + ".tmp"))
        try:
            temporary.write_text(text, encoding="utf-8")
            reread = temporary.read_text(encoding="utf-8")
            assert_sanitized(reread)
            if reread != text:
                raise OSError("Acceptance receipt failed byte verification.")
            if is_json:
                RealPaperAcceptanceReceipt.from_payload(json.loads(reread))
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def _render_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# 真实论文端到端验收回执",
        "",
        f"- Run ID: `{payload['run_id']}`",
        f"- Run kind: `{payload['run_kind']}`",
        f"- Core status: `{payload['final_status']}`",
        f"- Browser status: `{payload['browser_status']}`",
        f"- Topic: {payload['topic']}",
        f"- Domain: `{payload['domain']}`",
        f"- Model: `{payload['model']}`",
        f"- Source ID: `{payload['source_id'] or 'not-selected'}`",
        f"- Knowledge ID: `{payload['knowledge_id'] or 'not-published'}`",
        "",
        "## 阶段",
        "",
        "| 阶段 | 状态 | 耗时（ms） | 错误码 |",
        "| --- | --- | ---: | --- |",
    ]
    for stage in payload["stages"]:
        lines.append(
            f"| `{stage['name']}` | `{stage['status']}` | {stage['duration_ms']} | "
            f"`{stage['error_code'] or '-'}` |"
        )
    lines.extend(
        [
            "",
            "## 可复核身份",
            "",
            f"- Claims: {len(payload['claim_ids'])}",
            f"- Source anchors: {len(payload['source_anchor_ids'])}",
            f"- Chunks: {len(payload['chunk_ids'])}",
            f"- Citations: {len(payload['citation_ids'])}",
            f"- Manifest: `{payload['manifest_status'] or 'not-created'}`",
            f"- Bundle: `{payload['bundle_markdown_path'] or 'not-created'}`",
            "",
            "> 此回执只保存身份、哈希、计数和阶段结论，不包含 PDF、全文、提示词、完整回答或 provider 原始响应。",
            "",
        ]
    )
    return "\n".join(lines)


def _safe_stem(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(".-") or "acceptance"


def _timestamp_stem(value: str) -> str:
    return re.sub(r"[^0-9TZ]+", "-", value).strip("-")
