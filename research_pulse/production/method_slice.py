"""One-question MemGuard reading harness.

This is deliberately not a publication path.  It proves that one bounded
method explanation can retain a route back to the PDF region that supplied it.
"""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from enum import Enum
from hashlib import sha256
from typing import Any, Mapping, Protocol, Sequence
import json
import re

from .normalized import NormalizedBlock


METHOD_QUESTION = "MemGuard 的核心方法是什么？"
MAX_METHOD_CANDIDATES = 8
_METHOD_TERMS = (
    "memguard",
    "persistent",
    "verifier",
    "admission",
    "governance",
    "retrieval",
    "conflict",
    "archival",
)
_FORMULA_ASSIGNMENT = re.compile(
    r"\b[A-Za-z][A-Za-z0-9]*\s*_\s*(?:\{[^}]+\}|[A-Za-z0-9]+)\s*=\s*\([^)]{1,400}\)"
)


class ClaimUse(str, Enum):
    TEXT_QUOTE = "TEXT_QUOTE"
    NUMERIC_VALUE = "NUMERIC_VALUE"
    STRUCTURED_RELATION = "STRUCTURED_RELATION"
    FORMULA_SEMANTICS = "FORMULA_SEMANTICS"
    VISUAL_DESCRIPTION = "VISUAL_DESCRIPTION"


@dataclass(frozen=True)
class PageAnchor:
    page_start: int | None
    page_end: int | None
    bbox: tuple[float, float, float, float] | None


@dataclass(frozen=True)
class ParserObservation:
    parser: str
    text: str
    locators: tuple[str, ...]


@dataclass(frozen=True)
class SourceSpan:
    span_id: str
    kind: str
    section_path: tuple[str, ...]
    anchor: PageAnchor
    observations: tuple[ParserObservation, ...]
    parser_variants_available: bool = False
    material_status: str = "available"

    @property
    def observations_conflict(self) -> bool:
        if not self.parser_variants_available or len(self.observations) < 2:
            return False
        normalized = tuple(" ".join(item.text.casefold().split()) for item in self.observations)
        first = normalized[0]
        return any(SequenceMatcher(None, first, item).ratio() < 0.85 for item in normalized[1:])

    def read_view(self) -> str:
        """Return a machine-consumption view, never an authoritative source text."""

        if self.observations_conflict:
            raise ValueError("Conflicting parser observations have no preferred read view.")
        return self.observations[0].text

    def allows(self, use: ClaimUse) -> bool:
        """Answer only which claim operation this material currently permits."""

        if self.material_status != "available" or self.observations_conflict or not self.observations:
            return False
        if use is ClaimUse.TEXT_QUOTE:
            return self.kind in {"text", "caption"}
        # V1 has no trustworthy structure/vision assessment attached to a
        # SourceSpan.  All non-text capabilities therefore stay closed.
        return False


@dataclass(frozen=True)
class PaperMaterialSnapshot:
    source_id: str
    pdf_source: str
    source_spans: tuple[SourceSpan, ...]
    limitations: tuple[str, ...] = ()

    def allows(self, span_id: str, use: ClaimUse) -> bool:
        span = next((item for item in self.source_spans if item.span_id == span_id), None)
        if span is None:
            raise KeyError(span_id)
        return span.allows(use)

    @classmethod
    def from_normalized(
        cls,
        *,
        source_id: str,
        source_url: str,
        blocks: Sequence[NormalizedBlock],
    ) -> "PaperMaterialSnapshot":
        spans: list[SourceSpan] = []
        limitations: list[str] = []
        for block in blocks:
            anchor = PageAnchor(block.page_start, block.page_end, block.bbox)
            region_key = repr((source_id, block.kind, block.page_start, block.page_end, block.bbox)).encode("utf-8")
            region_hash = sha256(region_key).hexdigest()[:12]
            locators = tuple(f"{item.parser}:{item.locator}" for item in block.sources)
            spans.append(SourceSpan(
                span_id=f"source:{source_id}:region:{region_hash}",
                kind=block.kind,
                section_path=block.section_path,
                anchor=anchor,
                observations=(ParserObservation("normalized", block.text, locators),),
                parser_variants_available=False,
                material_status=block.parse_status,
            ))
        if spans:
            limitations.append("parser_variants_unavailable")
        return cls(source_id=source_id, pdf_source=source_url, source_spans=tuple(spans), limitations=tuple(limitations))


@dataclass(frozen=True)
class ReadingFinding:
    proposition: str
    source_span_ids: tuple[str, ...]
    derivation: str


@dataclass(frozen=True)
class WritingTask:
    question: str
    proposition: str
    finding: ReadingFinding


@dataclass(frozen=True)
class DraftBlock:
    markdown: str
    task: WritingTask


@dataclass(frozen=True)
class GroundingAssessment:
    verdict: str
    inspected_span_ids: tuple[str, ...]
    issues: tuple[str, ...] = ()


@dataclass(frozen=True)
class SearchTrace:
    candidate_span_ids: tuple[str, ...]
    accepted_span_ids: tuple[str, ...]
    searched_sections: tuple[str, ...]


@dataclass(frozen=True)
class Provenance:
    pdf_source: str
    source_span_ids: tuple[str, ...]
    anchors: tuple[PageAnchor, ...]


@dataclass(frozen=True)
class MethodSliceResult:
    status: str
    finding: ReadingFinding | None
    draft_block: DraftBlock | None
    grounding: GroundingAssessment | None
    search_trace: SearchTrace
    provenance: Provenance
    limitations: tuple[str, ...] = ()
    rejected_draft_block: DraftBlock | None = None


class MethodSliceModel(Protocol):
    def form_finding(self, *, question: str, candidates: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]: ...

    def write_single_finding(self, *, task: Mapping[str, Any]) -> str: ...

    def assess_grounding(self, *, claim: str, evidence: str) -> Mapping[str, Any]: ...


class JsonModelProvider(Protocol):
    text_model: str

    def call_json(
        self,
        operation: str,
        model: str,
        prompt: str,
        image: str | None = None,
    ) -> Mapping[str, Any]: ...


@dataclass
class DeepSeekMethodSliceModel:
    """Thin adapter from the slice roles to the existing bounded JSON provider."""

    provider: JsonModelProvider
    last_finding_response: Mapping[str, Any] | None = None
    last_grounding_response: Mapping[str, Any] | None = None

    def form_finding(self, *, question: str, candidates: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
        prompt = json.dumps({
            "operation": "method_slice_find",
            "question": question,
            "rules": [
                "只回答 MemGuard 的核心方法，不写实验、背景、优点或局限。",
                "只能使用 candidates 中的原文；source_span_ids 必须逐字选择已有 ID。",
                "proposition 用一到两句中文表达原文直接支持的机制，不补充常识或因果推断。",
                "严格保留原文的时间与阶段关系；不要把 before/after/during 等修饰语错误扩展到其他操作。",
                "V1 的正文 observation 只有 TEXT_QUOTE 能力：不要转写公式、符号定义或精确数值。",
                "V1 只允许 derivation=direct；如果材料不足，proposition 返回空字符串。",
                "不要把 parser observation 当成经过证明的真值。",
            ],
            "candidates": list(candidates),
            "return": {
                "proposition": "",
                "source_span_ids": [],
                "derivation": "direct",
            },
        }, ensure_ascii=False)
        response = self.provider.call_json("method_slice_find", self.provider.text_model, prompt)
        self.last_finding_response = response
        return response

    def write_single_finding(self, *, task: Mapping[str, Any]) -> str:
        # V1 permits no new factual content at the writing step.  Returning the
        # already-bounded proposition is therefore both deeper and safer than a
        # second model call whose only legal behavior would be to restate it.
        return str(task.get("proposition") or "").strip()

    def assess_grounding(self, *, claim: str, evidence: str) -> Mapping[str, Any]:
        prompt = (
            "Judge whether every technical statement in CLAIM is directly supported by EVIDENCE.\n"
            "Use only EVIDENCE. Do not search, use outside knowledge, repair the claim, or add evidence.\n"
            "Return exactly one JSON object with only these keys:\n"
            '{"verdict":"supported|uncertain|unsupported","issues":["short issue"]}\n'
            "Use unsupported when the claim adds effects, causality, scope, absolute language, mechanisms, "
            "or temporal relations absent from the evidence. Use uncertain when support is incomplete but not contradicted.\n\n"
            f"CLAIM:\n{claim}\n\nEVIDENCE:\n{evidence}"
        )
        response = self.provider.call_json("method_slice_ground", self.provider.text_model, prompt)
        self.last_grounding_response = response
        raw_verdict = str(response.get("verdict") or "").strip()
        verdict = raw_verdict.casefold()
        if verdict not in {"supported", "uncertain", "unsupported"}:
            issue = f"grounding_reviewer_invalid_verdict:{raw_verdict[:80] or 'missing'}"
            fallback = str(response.get("_fallback") or "").strip()
            if fallback:
                issue = f"{issue};provider_fallback:{fallback[:80]}"
            return {"verdict": "uncertain", "issues": [issue]}
        return {
            "verdict": verdict,
            "issues": tuple(str(item) for item in response.get("issues", ())),
        }


def run_memguard_method_slice(
    snapshot: PaperMaterialSnapshot,
    model: MethodSliceModel,
) -> MethodSliceResult:
    """Run the non-publishing q-method development slice over bounded spans."""

    relevant = tuple(
        span
        for span in snapshot.source_spans
        if any(_method_score(observation.text) > 0 for observation in span.observations)
    )
    candidates = tuple(sorted(
        relevant,
        key=lambda span: (
            -max(_method_score(item.text) for item in span.observations),
            -_method_section_score(span.section_path),
            span.anchor.page_start if span.anchor.page_start is not None else 10**9,
            span.span_id,
        ),
    )[:MAX_METHOD_CANDIDATES])
    conflicting = tuple(span for span in candidates if span.observations_conflict)
    searched_sections = tuple(dict.fromkeys(
        " / ".join(span.section_path) or "body" for span in snapshot.source_spans
    ))
    safe_candidates = tuple(
        span for span in candidates
        if not span.observations_conflict and span.allows(ClaimUse.TEXT_QUOTE)
    )
    if conflicting and not safe_candidates:
        return MethodSliceResult(
            status="CONFLICTING",
            finding=None,
            draft_block=None,
            grounding=None,
            search_trace=SearchTrace(
                candidate_span_ids=tuple(span.span_id for span in candidates),
                accepted_span_ids=(),
                searched_sections=searched_sections,
            ),
            provenance=Provenance(
                pdf_source=snapshot.pdf_source,
                source_span_ids=tuple(span.span_id for span in conflicting),
                anchors=tuple(span.anchor for span in conflicting),
            ),
            limitations=(*snapshot.limitations, "parser_observations_conflict"),
        )
    run_limitations = ("conflicting_method_candidates_excluded",) if conflicting else ()
    candidates = safe_candidates
    if not candidates:
        return MethodSliceResult(
            status="MATERIAL_UNAVAILABLE",
            finding=None,
            draft_block=None,
            grounding=None,
            search_trace=SearchTrace(
                candidate_span_ids=(),
                accepted_span_ids=(),
                searched_sections=searched_sections,
            ),
            provenance=Provenance(
                pdf_source=snapshot.pdf_source,
                source_span_ids=(),
                anchors=(),
            ),
            limitations=(*snapshot.limitations, *run_limitations, "method_evidence_unavailable"),
        )
    candidate_payload = tuple({
        "span_id": span.span_id,
        "section_path": list(span.section_path),
        "text": _method_transport_view(span),
        "page_start": span.anchor.page_start,
        "page_end": span.anchor.page_end,
        "bbox": span.anchor.bbox,
    } for span in candidates)
    response = model.form_finding(question=METHOD_QUESTION, candidates=candidate_payload)
    proposition = str(response.get("proposition") or "").strip()
    selected_ids = tuple(str(item) for item in response.get("source_span_ids", ()))
    candidate_by_id = {span.span_id: span for span in candidates}
    if not proposition or not selected_ids or any(item not in candidate_by_id for item in selected_ids):
        raise ValueError("Method finding must use non-empty evidence from the bounded candidate set.")
    finding = ReadingFinding(
        proposition=proposition,
        source_span_ids=selected_ids,
        derivation=str(response.get("derivation") or "direct"),
    )
    selected = tuple(candidate_by_id[item] for item in selected_ids)
    if finding.derivation != "direct":
        return MethodSliceResult(
            status="PARTIAL",
            finding=finding,
            draft_block=None,
            grounding=None,
            search_trace=SearchTrace(
                candidate_span_ids=tuple(span.span_id for span in candidates),
                accepted_span_ids=selected_ids,
                searched_sections=searched_sections,
            ),
            provenance=Provenance(
                pdf_source=snapshot.pdf_source,
                source_span_ids=selected_ids,
                anchors=tuple(span.anchor for span in selected),
            ),
            limitations=(*snapshot.limitations, *run_limitations, "inferred_finding_not_publishable"),
        )
    task = WritingTask(question=METHOD_QUESTION, proposition=proposition, finding=finding)
    markdown = model.write_single_finding(task={
        "question": task.question,
        "proposition": task.proposition,
        "source_span_ids": list(selected_ids),
    }).strip()
    draft = DraftBlock(markdown=markdown, task=task)
    if not markdown:
        return MethodSliceResult(
            status="UNSUPPORTED",
            finding=finding,
            draft_block=None,
            grounding=GroundingAssessment(
                verdict="unsupported",
                inspected_span_ids=(),
                issues=("empty_writer_output",),
            ),
            search_trace=SearchTrace(
                candidate_span_ids=tuple(span.span_id for span in candidates),
                accepted_span_ids=selected_ids,
                searched_sections=searched_sections,
            ),
            provenance=Provenance(
                pdf_source=snapshot.pdf_source,
                source_span_ids=selected_ids,
                anchors=tuple(span.anchor for span in selected),
            ),
            limitations=(*snapshot.limitations, *run_limitations),
            rejected_draft_block=draft,
        )
    required_uses = _required_claim_uses(markdown)
    disallowed_uses = tuple(
        use for use in required_uses
        if not any(snapshot.allows(span.span_id, use) for span in selected)
    )
    if disallowed_uses:
        return MethodSliceResult(
            status="UNSUPPORTED",
            finding=finding,
            draft_block=None,
            grounding=GroundingAssessment(
                verdict="unsupported",
                inspected_span_ids=selected_ids,
                issues=tuple(f"claim_use_not_allowed:{use.value}" for use in disallowed_uses),
            ),
            search_trace=SearchTrace(
                candidate_span_ids=tuple(span.span_id for span in candidates),
                accepted_span_ids=selected_ids,
                searched_sections=searched_sections,
            ),
            provenance=Provenance(
                pdf_source=snapshot.pdf_source,
                source_span_ids=selected_ids,
                anchors=tuple(span.anchor for span in selected),
            ),
            limitations=(*snapshot.limitations, *run_limitations),
            rejected_draft_block=draft,
        )
    evidence = "\n\n".join(span.read_view() for span in selected)
    review = model.assess_grounding(claim=markdown, evidence=evidence)
    verdict = str(review.get("verdict") or "unsupported").casefold()
    issues = tuple(str(item) for item in review.get("issues", ()))
    grounding = GroundingAssessment(verdict=verdict, inspected_span_ids=selected_ids, issues=issues)
    status = "SUPPORTED" if verdict == "supported" else "UNSUPPORTED"
    return MethodSliceResult(
        status=status,
        finding=finding,
        draft_block=draft if status == "SUPPORTED" else None,
        grounding=grounding,
        search_trace=SearchTrace(
            candidate_span_ids=tuple(span.span_id for span in candidates),
            accepted_span_ids=selected_ids,
            searched_sections=searched_sections,
        ),
        provenance=Provenance(
            pdf_source=snapshot.pdf_source,
            source_span_ids=selected_ids,
            anchors=tuple(span.anchor for span in selected),
        ),
        limitations=(*snapshot.limitations, *run_limitations),
        rejected_draft_block=draft if status == "UNSUPPORTED" else None,
    )


def _method_score(text: str) -> int:
    lowered = text.casefold()
    return sum(term in lowered for term in _METHOD_TERMS)


def _method_section_score(section_path: Sequence[str]) -> int:
    section = " ".join(section_path).casefold()
    return sum(marker in section for marker in ("abstract", "introduction", "method", "framework", "governance"))


def _required_claim_uses(markdown: str) -> tuple[ClaimUse, ...]:
    uses: list[ClaimUse] = []
    if re.search(r"\b[A-Za-z][A-Za-z0-9]*\s*_\s*(?:\{[^}]+\}|[A-Za-z0-9]+)\s*=", markdown):
        uses.append(ClaimUse.FORMULA_SEMANTICS)
    return tuple(uses)


def _method_transport_view(span: SourceSpan) -> str:
    text = span.read_view()
    if not span.allows(ClaimUse.FORMULA_SEMANTICS):
        text = _FORMULA_ASSIGNMENT.sub("[FORMULA OMITTED: FORMULA_SEMANTICS unavailable]", text)
    return text
