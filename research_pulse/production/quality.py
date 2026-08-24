"""Publish-time quality gates for a source-backed knowledge draft.

The gate is deliberately independent from a particular LLM or parser.  A
model can propose claims; it cannot mark its own claims as supported.  The
caller supplies source fragments and, when available, an independent
entailment judge.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import re
from typing import Literal, Mapping, Protocol, Sequence

from research_pulse.knowledge.models import (
    DurableEvidenceAnchor,
    EvidenceAnchor,
    KnowledgeAsset,
    KnowledgeAssetError,
    KnowledgeBundle,
    KnowledgeClaim,
    ReadingSectionEvidence,
    ReadingVisualEvidence,
    normalize_evidence_excerpt,
)
from research_pulse.production.evidence import EvidenceBlock


EntailmentVerdict = Literal["supported", "ambiguous", "unsupported"]
IssueSeverity = Literal["blocking", "review"]


class EntailmentJudge(Protocol):
    """A separately prompted judge which may only assess one supplied claim."""

    def assess(self, *, claim: str, evidence: str) -> EntailmentVerdict: ...


class ReadingSection(Protocol):
    text: str
    evidence_block_ids: tuple[str, ...]
    visuals: tuple[object, ...]


class ReadingAnalysis(Protocol):
    def sections(self) -> Mapping[str, ReadingSection]: ...


@dataclass(frozen=True)
class QualityIssue:
    code: str
    severity: IssueSeverity
    message: str
    claim_id: str | None = None
    anchor_id: str | None = None


@dataclass(frozen=True)
class QualityGateResult:
    approved: bool
    issues: tuple[QualityIssue, ...]
    bundle: KnowledgeBundle | None = None

    @property
    def requires_human_review(self) -> bool:
        return any(issue.severity == "review" for issue in self.issues)


def validate_draft(
    *,
    asset: KnowledgeAsset,
    claims: Sequence[KnowledgeClaim],
    anchors: Mapping[str, EvidenceAnchor],
    source_fragments: Mapping[str, str],
    evidence_blocks: Mapping[str, EvidenceBlock] | None = None,
    reading_analysis: ReadingAnalysis | None = None,
    visual_assets: Mapping[str, object] | None = None,
    entailment_judge: EntailmentJudge | None = None,
) -> QualityGateResult:
    """Return a conservative publishing decision for one candidate asset.

    ``source_fragments`` is transient parser output keyed by anchor ID.  It is
    intentionally passed in rather than saved with the asset, so the published
    Markdown stays a concise knowledge product instead of a copy of a paper.
    """

    issues: list[QualityIssue] = []
    if asset.publication_status not in {"needs_review", "published"}:
        issues.append(
            QualityIssue(
                code="invalid_publication_state",
                severity="blocking",
                message="Only a reviewed draft may enter the publication gate.",
            )
        )

    durable_anchors = _materialize_durable_anchors(
        asset=asset,
        claims=claims,
        anchors=anchors,
        source_fragments=source_fragments,
        issues=issues,
    )
    accepted_facets: set[str] = set()
    accepted_excerpt_facets: dict[str, str] = {}
    accepted_anchor_ids: set[str] = set()
    for claim in claims:
        if claim.claim_type != "source_fact":
            continue
        issue_count = len(issues)
        facet = _validate_source_fact(
            claim=claim,
            asset=asset,
            anchors=anchors,
            source_fragments=source_fragments,
            durable_anchors=durable_anchors,
            evidence_blocks=evidence_blocks,
            entailment_judge=entailment_judge,
            issues=issues,
        )
        if facet is not None:
            excerpts = {
                durable_anchors[anchor_id].evidence_excerpt
                for anchor_id in claim.anchor_ids
                if anchor_id in durable_anchors
            }
            reused = next(
                (
                    excerpt
                    for excerpt in excerpts
                    if excerpt in accepted_excerpt_facets and accepted_excerpt_facets[excerpt] != facet
                ),
                None,
            )
            if reused is not None:
                issues.append(
                    QualityIssue(
                        code="duplicate_required_facet_anchor",
                        severity="blocking",
                        message="One identical durable excerpt cannot satisfy multiple required facets.",
                        claim_id=claim.claim_id,
                        anchor_id=claim.anchor_ids[0] if claim.anchor_ids else None,
                    )
                )
                continue
            accepted_facets.add(facet)
            for excerpt in excerpts:
                accepted_excerpt_facets[excerpt] = facet
            if len(issues) == issue_count:
                accepted_anchor_ids.update(anchor_id for anchor_id in claim.anchor_ids if anchor_id in durable_anchors)

    if evidence_blocks:
        for facet in ("problem", "method", "experiment", "limitation"):
            if facet not in accepted_facets:
                issues.append(
                    QualityIssue(
                        code="missing_evidence_facet",
                        severity="blocking",
                        message=f"No eligible source fact covers the required '{facet}' facet.",
                    )
                )

    # Recompute the anchors accepted for reading sections from the final
    # claim-level verdicts.  The old incremental counter could permanently
    # omit a later valid claim after an earlier claim added a review issue,
    # making otherwise durable reading citations fail with
    # ``reading_section_anchor_not_durable``.
    blocking_claim_ids = {
        issue.claim_id
        for issue in issues
        if issue.severity == "blocking" and issue.claim_id is not None
    }
    accepted_anchor_ids = {
        anchor_id
        for claim in claims
        if claim.claim_type == "source_fact" and claim.claim_id not in blocking_claim_ids
        for anchor_id in claim.anchor_ids
        if anchor_id in durable_anchors
    }

    reading_sections: tuple[ReadingSectionEvidence, ...] = ()
    if reading_analysis is not None:
        reading_sections = _validate_reading_analysis(
            reading_analysis=reading_analysis,
            durable_anchors=durable_anchors,
            evidence_blocks=evidence_blocks or {},
            accepted_anchor_ids=accepted_anchor_ids,
            visual_assets=visual_assets or {},
            issues=issues,
        )

    approved = not any(issue.severity == "blocking" for issue in issues)
    bundle = None
    if approved:
        try:
            bundle = KnowledgeBundle(
                asset=asset,
                claims=tuple(claims),
                anchors=tuple(durable_anchors.values()),
                reading_sections=reading_sections,
                visual_assets=visual_assets or {},
            )
        except KnowledgeAssetError as error:
            issues.append(QualityIssue(code="invalid_knowledge_bundle", severity="blocking", message=str(error)))
            approved = False
    return QualityGateResult(approved=approved, issues=tuple(issues), bundle=bundle)


def _materialize_durable_anchors(
    *,
    asset: KnowledgeAsset,
    claims: Sequence[KnowledgeClaim],
    anchors: Mapping[str, EvidenceAnchor],
    source_fragments: Mapping[str, str],
    issues: list[QualityIssue],
) -> dict[str, DurableEvidenceAnchor]:
    durable: dict[str, DurableEvidenceAnchor] = {}
    # A reading inference may mention an anchor before the projection emits a
    # source fact for it.  Materialize source-fact excerpts first so the
    # durable 1,000-character window is centered on the verified text actually
    # used to support numbers and not on an arbitrary inference claim.
    ordered_claims = tuple(
        claim for claim in claims if claim.claim_type == "source_fact"
    ) + tuple(
        claim for claim in claims if claim.claim_type != "source_fact"
    )
    for claim in ordered_claims:
        for anchor_id in claim.anchor_ids:
            if anchor_id in durable:
                continue
            anchor = anchors.get(anchor_id)
            fragment = source_fragments.get(anchor_id, "").strip()
            if anchor is None or not fragment:
                issues.append(QualityIssue("unresolvable_anchor", "blocking", "The source anchor cannot be resolved in this parsing run.", claim.claim_id, anchor_id))
                continue
            if anchor.source_url not in asset.source_urls:
                issues.append(QualityIssue("anchor_source_outside_asset", "blocking", "An evidence anchor must point to one of the asset sources.", claim.claim_id, anchor_id))
                continue
            # Preserve the exact continuous source excerpt selected by the
            # source fact when one is available.  Falling back to the first
            # 1,000 characters of a long block can discard a result number
            # later in the same block, making an otherwise verbatim claim look
            # unsupported after materialization.
            evidence_excerpt = None
            if claim.claim_type == "source_fact":
                normalized_claim = normalize_evidence_excerpt(claim.text)
                normalized_fragment = normalize_evidence_excerpt(fragment)
                if normalized_claim and normalized_claim in normalized_fragment:
                    evidence_excerpt = claim.text
            try:
                durable[anchor_id] = make_durable_anchor(
                    anchor=anchor,
                    source_fragment=fragment,
                    evidence_excerpt=evidence_excerpt,
                )
            except KnowledgeAssetError as error:
                issues.append(QualityIssue("invalid_durable_anchor", "blocking", str(error), claim.claim_id, anchor_id))
    return durable


def make_durable_anchor(
    *, anchor: EvidenceAnchor, source_fragment: str, evidence_excerpt: str | None = None
) -> DurableEvidenceAnchor:
    normalized_source = normalize_evidence_excerpt(source_fragment)
    excerpt = normalize_evidence_excerpt(evidence_excerpt or normalized_source[:1_000])
    if evidence_excerpt is not None and excerpt not in normalized_source:
        raise KnowledgeAssetError("The durable excerpt must be continuous text from the verified source fragment.")
    return DurableEvidenceAnchor(
        anchor_id=anchor.anchor_id,
        source_url=anchor.source_url,
        section=anchor.section,
        page_start=anchor.page_start,
        page_end=anchor.page_end,
        figure_or_table=anchor.figure_or_table,
        block_kind=anchor.block_kind,
        parse_status=anchor.parse_status,
        locator_completeness=anchor.locator_completeness,
        bbox=anchor.bbox,
        evidence_excerpt=excerpt,
        excerpt_sha256=sha256(excerpt.encode("utf-8")).hexdigest(),
    )


def _validate_source_fact(
    *,
    claim: KnowledgeClaim,
    asset: KnowledgeAsset,
    anchors: Mapping[str, EvidenceAnchor],
    source_fragments: Mapping[str, str],
    durable_anchors: Mapping[str, DurableEvidenceAnchor],
    evidence_blocks: Mapping[str, EvidenceBlock] | None,
    entailment_judge: EntailmentJudge | None,
    issues: list[QualityIssue],
) -> str | None:
    if not claim.anchor_ids:
        issues.append(
            QualityIssue(
                code="source_fact_without_anchor",
                severity="blocking",
                message="Source facts require at least one evidence anchor.",
                claim_id=claim.claim_id,
            )
        )
        return None

    valid_facet: str | None = claim.source_facet
    if evidence_blocks is not None:
        if claim.source_facet not in {"problem", "method", "experiment", "limitation"}:
            issues.append(
                QualityIssue(
                    code="source_fact_missing_facet",
                    severity="blocking",
                    message="A source fact from enhanced evidence needs one required facet.",
                    claim_id=claim.claim_id,
                )
            )
            valid_facet = None

    supported_fragments: list[str] = []
    for anchor_id in claim.anchor_ids:
        anchor = anchors.get(anchor_id)
        fragment = source_fragments.get(anchor_id, "").strip()
        if anchor is None or not fragment or anchor_id not in durable_anchors:
            issues.append(
                QualityIssue(
                    code="unresolvable_anchor",
                    severity="blocking",
                    message="The source anchor cannot be resolved in this parsing run.",
                    claim_id=claim.claim_id,
                    anchor_id=anchor_id,
                )
            )
            continue
        if evidence_blocks is not None:
            evidence_block = evidence_blocks.get(anchor_id)
            if evidence_block is None or not evidence_block.eligible_for_fact:
                issues.append(
                    QualityIssue(
                        code="ineligible_evidence_block",
                        severity="blocking",
                        message="A source fact must resolve to an eligible classified evidence block.",
                        claim_id=claim.claim_id,
                        anchor_id=anchor_id,
                    )
                )
                valid_facet = None
                continue
            if claim.source_facet not in evidence_block.supported_facets:
                issues.append(
                    QualityIssue(
                        code="evidence_block_facet_mismatch",
                        severity="blocking",
                        message="The selected evidence block does not support the requested source-fact facet.",
                        claim_id=claim.claim_id,
                        anchor_id=anchor_id,
                    )
                )
                valid_facet = None
                continue
        if anchor.source_url not in asset.source_urls:
            issues.append(
                QualityIssue(
                    code="anchor_source_outside_asset",
                    severity="blocking",
                    message="An evidence anchor must point to one of the asset sources.",
                    claim_id=claim.claim_id,
                    anchor_id=anchor_id,
                )
            )
            continue
        _check_numeric_tokens(claim, fragment, anchor_id, issues)
        supported_fragments.append(durable_anchors[anchor_id].evidence_excerpt)

    if not supported_fragments:
        return None
    if not _is_verbatim_anchored_excerpt(claim.text, supported_fragments):
        issues.append(
            QualityIssue(
                code="source_fact_not_verbatim",
                severity="blocking",
                message="A source fact must be a continuous excerpt from one of its cited anchors.",
                claim_id=claim.claim_id,
            )
        )
        return None
    if entailment_judge is None:
        issues.append(
            QualityIssue(
                code="entailment_judge_missing",
                severity="review",
                message="Anchor integrity passed, but semantic support still needs independent review.",
                claim_id=claim.claim_id,
            )
        )
        return valid_facet

    verdict = entailment_judge.assess(claim=claim.text, evidence="\n\n".join(supported_fragments))
    if verdict == "unsupported":
        issues.append(
            QualityIssue(
                code="entailment_unsupported",
                severity="blocking",
                message="The independent judge found that the anchor does not support this fact.",
                claim_id=claim.claim_id,
            )
        )
    elif verdict == "ambiguous":
        issues.append(
            QualityIssue(
                code="entailment_ambiguous",
                severity="review",
                message="The independent judge could not establish full support; require human review.",
                claim_id=claim.claim_id,
            )
        )
    return valid_facet


def _is_verbatim_anchored_excerpt(claim_text: str, fragments: Sequence[str]) -> bool:
    normalized_claim = normalize_evidence_excerpt(claim_text)
    if not normalized_claim:
        return False
    return any(normalized_claim in normalize_evidence_excerpt(fragment) for fragment in fragments)


def _validate_reading_analysis(
    *,
    reading_analysis: ReadingAnalysis,
    durable_anchors: Mapping[str, DurableEvidenceAnchor],
    evidence_blocks: Mapping[str, EvidenceBlock],
    accepted_anchor_ids: set[str],
    visual_assets: Mapping[str, object],
    issues: list[QualityIssue],
) -> tuple[ReadingSectionEvidence, ...]:
    # The narrative may legitimately connect an introduction definition to a
    # method section, or a method design to an experiment interpretation. The
    # generation prompt provides the reading order; this gate only verifies
    # that cited blocks are eligible, same-version evidence. Facet semantics
    # remain enforced on durable source_fact claims themselves.
    expected_facets: dict[str, set[str]] = {
        section: {"problem", "method", "experiment", "limitation"}
        for section in reading_analysis.sections()
    }
    all_excerpt_numbers = {
        number
        for anchor in durable_anchors.values()
        for number in _numeric_tokens(anchor.evidence_excerpt)
    }
    # OCR/Markdown conversion can drop a percent sign while preserving the
    # numeric value.  Treat ``85.98`` and ``85.98%`` as the same canonical
    # value for the reading-to-excerpt check; the source-fact gate still checks
    # the verbatim claim against its source fragment.
    all_excerpt_numbers_with_units = {
        variant
        for number in all_excerpt_numbers
        for variant in _numeric_token_variants(number)
    }
    mappings: list[ReadingSectionEvidence] = []
    for section_name, section in reading_analysis.sections().items():
        if not section.text.strip():
            continue
        valid_anchor_ids: list[str] = []
        for anchor_id in section.evidence_block_ids:
            durable = durable_anchors.get(anchor_id)
            block = evidence_blocks.get(anchor_id)
            allowed = expected_facets.get(section_name)
            if (
                durable is None
                or anchor_id not in accepted_anchor_ids
                or block is None
                or not block.eligible_for_fact
                or (allowed is not None and not allowed.intersection(block.supported_facets))
            ):
                issues.append(
                    QualityIssue(
                        code="reading_section_anchor_not_durable",
                        severity="blocking",
                        message="A reading section may cite only same-version durable anchors used by approved source facts.",
                        anchor_id=anchor_id,
                    )
                )
                continue
            valid_anchor_ids.append(anchor_id)

        valid_visuals: list[ReadingVisualEvidence] = []
        for visual in getattr(section, "visuals", ()):
            block_id = getattr(visual, "block_id", None)
            role = getattr(visual, "role", None)
            explanation = getattr(visual, "explanation", None)
            block = evidence_blocks.get(block_id) if isinstance(block_id, str) else None
            if (
                not isinstance(block_id, str)
                or not isinstance(role, str)
                or not isinstance(explanation, str)
                or block is None
                or block.candidate.kind not in {"formula", "table", "figure"}
                or not explanation.strip()
            ):
                issues.append(
                    QualityIssue(
                        code="reading_visual_not_resolvable",
                        severity="review",
                        message="A selected visual is not resolvable in the current evidence run.",
                        anchor_id=block_id if isinstance(block_id, str) else None,
                    )
                )
                continue
            asset_path = next(
                (path for path, source in visual_assets.items() if getattr(block.candidate, "image_source_path", None) == source),
                None,
            ) if block.candidate.kind == "figure" else None
            try:
                valid_visuals.append(
                    ReadingVisualEvidence(
                        block_id=block_id,
                        kind=block.candidate.kind,
                        role=role,
                        explanation=explanation,
                        asset_path=asset_path if isinstance(asset_path, str) else None,
                    )
                )
            except Exception as error:
                issues.append(
                    QualityIssue(
                        code="reading_visual_invalid",
                        severity="review",
                        message=str(error),
                        anchor_id=block_id,
                    )
                )
        mappings.append(
            ReadingSectionEvidence(
                section_name,
                tuple(dict.fromkeys(valid_anchor_ids)),
                tuple(valid_visuals),
            )
        )  # type: ignore[arg-type]

        if not valid_anchor_ids:
            issues.append(
                QualityIssue(
                    code="reading_section_missing_evidence",
                    severity="blocking",
                    message="Every non-empty reading section needs at least one valid same-version durable anchor.",
                )
            )

        for number in _numeric_tokens(section.text):
            if not _numeric_token_variants(number).intersection(all_excerpt_numbers_with_units):
                issues.append(
                    QualityIssue(
                        code="reading_number_not_in_durable_excerpt",
                        severity="blocking",
                        message=f"The reading section numeric token '{number}' is absent from its durable evidence excerpts.",
                    )
                )

        referenced_blocks = [evidence_blocks[anchor_id] for anchor_id in valid_anchor_ids]
        visual_blocks = [
            evidence_blocks[visual.block_id]
            for visual in getattr(section, "visuals", ())
            if getattr(visual, "block_id", None) in evidence_blocks
        ]
        if _claims_formula_meaning(section.text) and not any(
            block.candidate.kind == "formula" and block.eligible_for_fact
            for block in referenced_blocks
        ) and not any(block.candidate.kind == "formula" for block in visual_blocks):
            issues.append(
                QualityIssue(
                    code="reading_formula_not_durable",
                    severity="blocking",
                    message="A formula interpretation requires an eligible formula block in this reading section.",
                )
            )

        table_excerpts = [
            durable_anchors[anchor_id].evidence_excerpt
            for anchor_id in valid_anchor_ids
            if evidence_blocks[anchor_id].candidate.kind == "table"
        ]
        if table_excerpts and not any(_table_comparison_evidence_complete(excerpt) for excerpt in table_excerpts):
            issues.append(
                QualityIssue(
                    code="reading_table_comparison_incomplete",
                    severity="blocking",
                    message="A table comparison requires metric, comparison objects, and values in one durable excerpt.",
                )
            )
    return tuple(mappings)


def _numeric_tokens(value: str) -> tuple[str, ...]:
    # Parsers frequently render the same number in different continuous-text
    # forms: ``1,020`` vs ``1020`` and ``4 . 55 × 10 − 4`` vs ``4.55e-4``.
    # Canonicalize those typography-only differences before enforcing that a
    # reading paragraph's numbers occur in durable evidence.
    normalized = value.replace(",", "")
    normalized = re.sub(r"(?<=\d)\s*\.\s*(?=\d)", ".", normalized)
    normalized = re.sub(
        r"(\d+(?:\.\d+)?)\s*[×x]\s*10\s*[−–-]\s*(\d+)",
        r"\1e-\2",
        normalized,
        flags=re.IGNORECASE,
    )
    return tuple(
        re.findall(
            r"(?<![\w.])(?:\d{1,3}(?:,\d{3})+|\d+)"
            r"(?:\.\d+)?(?:[eE][+-]?\d+)?[%％]?(?![\w.)、:，；])",
            normalized,
        )
    )


def _numeric_token_variants(value: str) -> set[str]:
    normalized = value.replace("％", "%")
    if normalized.endswith("%"):
        return {normalized, normalized[:-1]}
    return {normalized, f"{normalized}%"}


def _claims_formula_meaning(value: str) -> bool:
    # Mentioning an unavailable formula as a limitation is safe; only an
    # affirmative interpretation of that formula should require a durable
    # formula block.  Evaluate sentence-sized spans so a reproduction note
    # such as “the exact formula is unknown” cannot trigger the gate merely
    # because the same paragraph also mentions parameters.
    uncertainty_markers = (
        "unknown",
        "unavailable",
        "not recovered",
        "cannot recover",
        "未知",
        "未能",
        "未恢复",
        "无法",
        "不完整",
        "缺失",
    )
    meaning_markers = (
        "parameter",
        "deriv",
        "controls",
        "proves",
        "means",
        "defines",
        "参数",
        "推导",
        "表示",
        "定义",
        "控制",
    )
    for sentence in re.split(r"[。.!?；;\n]", value.casefold()):
        if not any(marker in sentence for marker in ("formula", "equation", "公式", "方程")):
            continue
        if any(marker in sentence for marker in uncertainty_markers):
            continue
        if any(marker in sentence for marker in meaning_markers):
            return True
    return False


def _table_comparison_evidence_complete(value: str) -> bool:
    normalized = normalize_evidence_excerpt(value)
    numbers = _numeric_tokens(normalized)
    labels = re.findall(r"[A-Za-z][A-Za-z0-9._-]*|[\u4e00-\u9fff]{2,}", normalized)
    return normalized.count("|") >= 6 and len(numbers) >= 2 and len(labels) >= 3


def _check_numeric_tokens(
    claim: KnowledgeClaim,
    fragment: str,
    anchor_id: str,
    issues: list[QualityIssue],
) -> None:
    """Reject a fact which introduces a number absent from all of its evidence."""

    anchored_numbers = set(_numeric_tokens(fragment))
    for number in _numeric_tokens(claim.text):
        if number not in anchored_numbers:
            issues.append(
                QualityIssue(
                    code="number_not_in_anchor",
                    severity="blocking",
                    message=f"The numeric token '{number}' is absent from the anchored source fragment.",
                    claim_id=claim.claim_id,
                    anchor_id=anchor_id,
                )
            )
