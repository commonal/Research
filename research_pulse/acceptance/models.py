"""Framework-free value objects for a single-paper real acceptance run."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any, Mapping, Sequence


FINAL_STAGE_NAMES = (
    "preflight",
    "discovery",
    "production",
    "bundle_verify",
    "retrieval_verify",
    "api_chat_verify",
    "receipt",
)


class RunKind(StrEnum):
    REAL = "real"
    SIMULATED = "simulated"


class FinalStatus(StrEnum):
    PASSED = "passed"
    ENVIRONMENT_BLOCKED = "environment_blocked"
    ACCEPTANCE_FAILED = "acceptance_failed"


class StageStatus(StrEnum):
    PENDING = "pending"
    PASSED = "passed"
    BLOCKED = "blocked"
    FAILED = "failed"
    SKIPPED = "skipped"


class BrowserStatus(StrEnum):
    PENDING = "pending"
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"


def _non_empty(value: str, field_name: str) -> str:
    normalized = " ".join(value.split())
    if not normalized:
        raise ValueError(f"{field_name} must be non-empty.")
    return normalized


@dataclass(frozen=True)
class StageReceipt:
    name: str
    started_at: str
    duration_ms: int
    status: StageStatus
    error_code: str | None = None
    error_summary: str | None = None
    metrics: Mapping[str, str | int | float | bool | None] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.name not in FINAL_STAGE_NAMES:
            raise ValueError(f"Unsupported acceptance stage: {self.name}")
        _non_empty(self.started_at, "started_at")
        if isinstance(self.duration_ms, bool) or self.duration_ms < 0:
            raise ValueError("duration_ms must be a non-negative integer.")
        if self.status in {StageStatus.BLOCKED, StageStatus.FAILED} and not self.error_code:
            raise ValueError("Blocked and failed stages need an error_code.")
        if self.status not in {StageStatus.BLOCKED, StageStatus.FAILED} and (
            self.error_code or self.error_summary
        ):
            raise ValueError("Only blocked or failed stages may contain an error.")
        if self.error_code is not None:
            _non_empty(self.error_code, "error_code")
        if self.error_summary is not None and len(self.error_summary) > 240:
            raise ValueError("error_summary cannot exceed 240 characters.")
        for key, value in self.metrics.items():
            _non_empty(key, "metric name")
            if not isinstance(value, (str, int, float, bool, type(None))):
                raise ValueError("Stage metrics must contain scalar values only.")

    @classmethod
    def from_payload(cls, value: object) -> "StageReceipt":
        if not isinstance(value, dict):
            raise ValueError("A stage receipt must be an object.")
        expected = {
            "name", "started_at", "duration_ms", "status", "error_code", "error_summary", "metrics"
        }
        if set(value) != expected or not isinstance(value.get("metrics"), dict):
            raise ValueError("Stage receipt fields are incomplete or unsupported.")
        return cls(
            name=value["name"],
            started_at=value["started_at"],
            duration_ms=value["duration_ms"],
            status=StageStatus(value["status"]),
            error_code=value["error_code"],
            error_summary=value["error_summary"],
            metrics=value["metrics"],
        )


@dataclass(frozen=True)
class RealPaperAcceptanceReceipt:
    receipt_schema_version: int
    run_id: str
    run_kind: RunKind
    final_status: FinalStatus
    started_at: str
    topic: str
    domain: str
    model: str
    stages: tuple[StageReceipt, ...]
    source_id: str | None = None
    knowledge_id: str | None = None
    knowledge_version: str | None = None
    evidence_level: str | None = None
    bundle_markdown_path: str | None = None
    content_sha256: str | None = None
    provenance_sha256: str | None = None
    manifest_status: str | None = None
    claim_ids: tuple[str, ...] = ()
    source_anchor_ids: tuple[str, ...] = ()
    chunk_ids: tuple[str, ...] = ()
    citation_ids: tuple[str, ...] = ()
    answer_sha256: str | None = None
    answer_length: int | None = None
    browser_status: BrowserStatus = BrowserStatus.PENDING
    browser_summary: str | None = None

    def __post_init__(self) -> None:
        if self.receipt_schema_version != 1:
            raise ValueError("Unsupported acceptance receipt schema version.")
        for value, name in (
            (self.run_id, "run_id"),
            (self.started_at, "started_at"),
            (self.topic, "topic"),
            (self.domain, "domain"),
            (self.model, "model"),
        ):
            _non_empty(value, name)
        if tuple(stage.name for stage in self.stages) != FINAL_STAGE_NAMES:
            raise ValueError("Acceptance stages must be complete and in canonical order.")
        if self.run_kind == RunKind.SIMULATED and self.final_status == FinalStatus.PASSED:
            raise ValueError("A simulated run cannot be recorded as a real acceptance pass.")
        statuses = tuple(stage.status for stage in self.stages)
        if self.final_status == FinalStatus.ENVIRONMENT_BLOCKED:
            if StageStatus.BLOCKED not in statuses:
                raise ValueError("An environment-blocked receipt needs a blocked stage.")
            blocked_index = statuses.index(StageStatus.BLOCKED)
            if any(status != StageStatus.SKIPPED for status in statuses[blocked_index + 1 : -1]):
                raise ValueError("Stages after an environment blocker must be skipped.")
        elif self.final_status == FinalStatus.ACCEPTANCE_FAILED:
            if StageStatus.FAILED not in statuses:
                raise ValueError("An acceptance failure needs a failed stage.")
        else:
            if self.run_kind != RunKind.REAL:
                raise ValueError("Only a real run can pass acceptance.")
            if any(status != StageStatus.PASSED for status in statuses):
                raise ValueError("Every stage must pass for a real acceptance pass.")
            required = (self.source_id, self.knowledge_id, self.knowledge_version, self.content_sha256)
            if any(not value for value in required):
                raise ValueError("A passed receipt needs complete paper and bundle identity.")
        for collection in (self.claim_ids, self.source_anchor_ids, self.chunk_ids, self.citation_ids):
            if len(collection) != len(set(collection)) or any(not item.strip() for item in collection):
                raise ValueError("Receipt identity collections must contain unique non-empty values.")
        if self.answer_length is not None and (isinstance(self.answer_length, bool) or self.answer_length < 0):
            raise ValueError("answer_length must be a non-negative integer.")
        if self.browser_summary is not None and len(self.browser_summary) > 240:
            raise ValueError("browser_summary cannot exceed 240 characters.")

    @property
    def is_real_core_pass(self) -> bool:
        return self.run_kind == RunKind.REAL and self.final_status == FinalStatus.PASSED

    @property
    def is_complete_product_pass(self) -> bool:
        return self.is_real_core_pass and self.browser_status == BrowserStatus.PASSED

    def to_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["run_kind"] = self.run_kind.value
        payload["final_status"] = self.final_status.value
        payload["browser_status"] = self.browser_status.value
        payload["stages"] = list(payload["stages"])
        for stage in payload["stages"]:
            stage["status"] = stage["status"].value
        for key in ("claim_ids", "source_anchor_ids", "chunk_ids", "citation_ids"):
            payload[key] = list(payload[key])
        return payload

    @classmethod
    def from_payload(cls, value: object) -> "RealPaperAcceptanceReceipt":
        if not isinstance(value, dict):
            raise ValueError("An acceptance receipt must be an object.")
        expected = set(cls.__dataclass_fields__)
        if set(value) != expected or not isinstance(value.get("stages"), list):
            raise ValueError("Acceptance receipt fields are incomplete or unsupported.")
        collections: dict[str, tuple[str, ...]] = {}
        for key in ("claim_ids", "source_anchor_ids", "chunk_ids", "citation_ids"):
            raw = value.get(key)
            if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
                raise ValueError(f"{key} must be a string array.")
            collections[key] = tuple(raw)
        return cls(
            **{
                **value,
                "run_kind": RunKind(value["run_kind"]),
                "final_status": FinalStatus(value["final_status"]),
                "browser_status": BrowserStatus(value["browser_status"]),
                "stages": tuple(StageReceipt.from_payload(item) for item in value["stages"]),
                **collections,
            }
        )


def stage_sequence(
    statuses: Sequence[StageStatus],
    *,
    started_at: str = "1970-01-01T00:00:00Z",
    error_code: str = "injected_failure",
) -> tuple[StageReceipt, ...]:
    """Concise constructor used by tests and early blocked receipts."""

    if len(statuses) != len(FINAL_STAGE_NAMES):
        raise ValueError("A status is required for every acceptance stage.")
    return tuple(
        StageReceipt(
            name=name,
            started_at=started_at,
            duration_ms=0,
            status=status,
            error_code=error_code if status in {StageStatus.BLOCKED, StageStatus.FAILED} else None,
            error_summary="Acceptance stage did not complete." if status in {StageStatus.BLOCKED, StageStatus.FAILED} else None,
        )
        for name, status in zip(FINAL_STAGE_NAMES, statuses, strict=True)
    )
