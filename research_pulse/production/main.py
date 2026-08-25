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

from research_pulse.production.adapters import ArxivCandidateFinder
from research_pulse.reader_production import ReaderConfig, ReaderProductionService


def main() -> int:
    load_dotenv(_project_root() / ".env", override=False)
    parser = argparse.ArgumentParser(description="Run one source-bounded Research Pulse production batch.")
    parser.add_argument("--topic", required=True, help="arXiv search topic, for example 'LLM agent memory'.")
    parser.add_argument("--domain", required=True, help="Stable domain tag used for retrieval filtering.")
    parser.add_argument("--limit", type=int, default=3, help="Maximum papers to process (default: 3).")
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
    service = ReaderProductionService(ReaderConfig(normalized_root=args.normalized_root), vault_root)
    candidates = ArxivCandidateFinder().discover(topic=args.topic, domain=args.domain, limit=args.limit)
    for candidate in candidates:
        receipt = service.process(candidate)
        path = receipt["published_path"]
        print(f"{receipt['source_id']}: {receipt['receipt_status']} -> {path or '(no file written)'}")
    return 0


def _project_root():
    from pathlib import Path

    return Path(__file__).resolve().parents[2]


if __name__ == "__main__":
    raise SystemExit(main())
