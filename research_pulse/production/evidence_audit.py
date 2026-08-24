"""Safe, transient diagnostics for evidence-block coverage before model calls."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from dataclasses import asdict
import json
from pathlib import Path
from typing import Mapping

from research_pulse.production.adapters import DoclingSourceParser
from research_pulse.production.evidence import EvidenceBlock, EvidenceFacet
from research_pulse.production.pipeline import PaperCandidate, SourceMaterial


REQUIRED_FACETS: tuple[EvidenceFacet, ...] = ("problem", "method", "experiment", "limitation")


@dataclass(frozen=True)
class EvidenceAudit:
    """Counts and IDs only; raw parser text deliberately has no path into this value."""

    total_blocks: int
    eligible_block_ids: tuple[str, ...]
    eligible_by_facet: Mapping[EvidenceFacet, tuple[str, ...]]
    blocks_by_kind: Mapping[str, int]
    rejection_counts: Mapping[str, int]

    @property
    def missing_facets(self) -> tuple[EvidenceFacet, ...]:
        return tuple(facet for facet in REQUIRED_FACETS if not self.eligible_by_facet.get(facet))

    @property
    def automatic_publication_possible(self) -> bool:
        return not self.missing_facets


def audit_material(material: SourceMaterial) -> EvidenceAudit:
    """Summarize one in-memory parse without retaining a PDF or parser text."""

    eligible: list[str] = []
    by_facet: dict[EvidenceFacet, list[str]] = {facet: [] for facet in REQUIRED_FACETS}
    by_kind: dict[str, int] = {}
    rejections: dict[str, int] = {}
    for block in material.evidence_blocks.values():
        _record_block(block, eligible, by_facet, by_kind, rejections)
    return EvidenceAudit(
        total_blocks=len(material.evidence_blocks),
        eligible_block_ids=tuple(eligible),
        eligible_by_facet={facet: tuple(ids) for facet, ids in by_facet.items()},
        blocks_by_kind=dict(sorted(by_kind.items())),
        rejection_counts=dict(sorted(rejections.items())),
    )


def _record_block(
    block: EvidenceBlock,
    eligible: list[str],
    by_facet: dict[EvidenceFacet, list[str]],
    by_kind: dict[str, int],
    rejections: dict[str, int],
) -> None:
    kind = block.candidate.kind
    by_kind[kind] = by_kind.get(kind, 0) + 1
    if not block.eligible_for_fact:
        reason = block.rejection_reason or "unknown_rejection"
        rejections[reason] = rejections.get(reason, 0) + 1
        return
    eligible.append(block.candidate.block_id)
    for facet in block.supported_facets:
        by_facet[facet].append(block.candidate.block_id)


def write_audit(audit: EvidenceAudit, output_path: Path) -> None:
    """Write only safe evidence metadata atomically for an opt-in inspection."""

    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        **asdict(audit),
        "missing_facets": list(audit.missing_facets),
        "automatic_publication_possible": audit.automatic_publication_possible,
        "persistence": "metadata_only; no PDF, parser text, Markdown, image, or model response",
    }
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(output_path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect one arXiv PDF's evidence-block coverage without calling an LLM.")
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--source-url", default=None)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    source_url = args.source_url or f"https://arxiv.org/abs/{args.source_id}"
    material = DoclingSourceParser().parse(
        PaperCandidate(args.source_id, "Evidence audit", source_url, "evidence_audit")
    )
    write_audit(audit_material(material), args.output)
    print(f"audit_output={args.output.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
