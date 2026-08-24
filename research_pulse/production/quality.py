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
    normalize_evidence_excerpt,
)
from research_pulse.production.evidence import EvidenceBlock


EntailmentVerdict = Literal["supported", "ambiguous", "unsupported"]
IssueSeverity = Literal["blocking", "review"]


class EntailmentJudge(Protocol):
    """A separately prompted judge which may only assess one supplied claim."""

    def assess(self, *, claim: str, evidence: str) -> EntailmentVerdict: ...


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

    approved = not any(issue.severity == "blocking" for issue in issues)
    bundle = None
    if approved:
        try:
            bundle = KnowledgeBundle(
                asset=asset,
                claims=tuple(claims),
                anchors=tuple(durable_anchors.values()),
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