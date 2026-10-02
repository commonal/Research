"""Explicit command entrypoint for one real Research Pulse production batch.

The production reading route is the single entry: candidate discovery ->
PaperReader (reader_production.ReaderProductionService) -> note-only publish.
It consumes server-normalized blocks from a normalized root and writes a
schema-v1 note asset; it does not require a database or the legacy
bundle/Postgres-RAG production chain.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from dotenv import load_dotenv

from research_pulse.pedagogical.pipeline import PedagogicalService
from research_pulse.production.adapters import ArxivCandidateFinder
from research_pulse.production.pipeline import PaperCandidate
from research_pulse.production.reading import DeepSeekPaperReadingModel
from research_pulse.reader_production import ReaderConfig, ReaderProductionService


def main() -> int:
    load_dotenv(_project_root() / ".env", override=False)
    parser = argparse.ArgumentParser(description="Run one source-bounded Research Pulse production batch.")
    parser.add_argument(
        "--topic",
        default=None,
        help="arXiv search topic (discovery mode; required unless --source-id is given).",
    )
    parser.add_argument("--domain", required=True, help="Stable domain tag used for retrieval filtering.")
    parser.add_argument(
        "--source-id",
        default=None,
        help="Run a single already-normalized paper by source_id (skips discovery).",
    )
    parser.add_argument("--title", default=None, help="Optional paper title for --source-id mode.")
    parser.add_argument("--source-url", default=None, help="Optional source URL for --source-id mode.")
    parser.add_argument("--limit", type=int, default=3, help="Maximum papers to process (default: 3).")
    parser.add_argument(
        "--mode",
        choices=["generic", "pedagogical"],
        default="pedagogical",
        help="Reading mode. Default pedagogical (Phase2); generic is the fallback. Pass --mode generic to opt out.",
    )
    parser.add_argument(
        "--normalized-root",
        type=Path,
        default=Path("data/normalized"),
        help="Normalized block cache root: <root>/<source_id>/normalized/blocks.jsonl (default: data/normalized).",
    )
    parser.add_argument(
        "--vault-root",
        type=Path,
        default=None,
        help="Vault root for note-only publish (defaults to ./knowledge under the project root).",
    )
    args = parser.parse_args()
    if args.limit < 1 or args.limit > 10:
        parser.error("--limit must be between 1 and 10.")

    vault_root = args.vault_root or (_project_root() / "knowledge")
    model = DeepSeekPaperReadingModel.from_environment()
    config = ReaderConfig(normalized_root=args.normalized_root, mode=args.mode)
    pedagogical = PedagogicalService(model, asset_root=args.normalized_root) if args.mode == "pedagogical" else None
    service = ReaderProductionService(config, vault_root, model=model, pedagogical=pedagogical)

    if args.source_id:
        if args.topic:
            parser.error("use either --source-id or --topic, not both.")
        candidates = [_single_candidate(args)]
    else:
        if not args.topic:
            parser.error("--topic is required unless --source-id is provided.")
        candidates = ArxivCandidateFinder().discover(topic=args.topic, domain=args.domain, limit=args.limit)

    for candidate in candidates:
        receipt = service.process(candidate)
        path = receipt["published_path"]
        requested = receipt.get("requested_route", args.mode)
        delivered = receipt.get("delivered_route") or "none"
        fallbacks = receipt.get("pedagogical_fallbacks") or []
        fallback = ",".join(fallbacks) if fallbacks else "none"
        print(
            f"{receipt['source_id']}: {receipt['receipt_status']} "
            f"status={receipt.get('status', receipt.get('publication_status', 'unknown'))} "
            f"route={requested}->{delivered} fallback={fallback} "
            f"stop={receipt.get('stop_reason', 'unknown')} -> {path or '(no file written)'}"
        )
        print(f"  receipt={receipt.get('receipt_path') or '(not persisted)'}")
        if receipt.get("audit_candidate_path"):
            print(f"  audit={receipt['audit_candidate_path']}")
    return 0


def _single_candidate(args):
    """Build one PaperCandidate from a normalized source id (single-paper CLI mode)."""
    import json

    source_id = args.source_id
    blocks_path = args.normalized_root / source_id / "normalized" / "blocks.jsonl"
    manifest_path = args.normalized_root / source_id / "normalized" / "manifest.json"
    if not blocks_path.exists():
        raise SystemExit(f"No normalized blocks at {blocks_path}")

    source_url = args.source_url or _manifest_value(manifest_path, "source_url") or f"https://arxiv.org/abs/{source_id}"
    title = args.title or _derive_title(blocks_path)
    if not title:
        raise SystemExit("Could not derive a paper title; pass --title.")
    return PaperCandidate(source_id=source_id, title=title, source_url=source_url, domain=args.domain)


def _manifest_value(path: Path, key: str):
    import json

    try:
        return json.loads(path.read_text(encoding="utf-8")).get(key)
    except Exception:
        return None


def _derive_title(blocks_path: Path) -> str:
    """Use the top-level section path of the first substantial text block as the title."""
    import json

    try:
        with blocks_path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                block = json.loads(line)
                text = (block.get("text") or "").strip()
                section = block.get("section_path") or []
                if section:
                    first = str(section[0]).strip()
                    if len(first) > 8 and not first.casefold().startswith("abstract"):
                        return first
                if len(text) >= 40:
                    return text[:120]
    except Exception:
        pass
    return ""


def _project_root():
    from pathlib import Path

    return Path(__file__).resolve().parents[2]


if __name__ == "__main__":
    raise SystemExit(main())
