"""Explicit command entrypoint for one real Research Pulse production batch."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from dotenv import load_dotenv

from research_pulse.production.adapters import (
    ArxivCandidateFinder,
    DeepSeekEntailmentJudge,
    DeepSeekStructuredExtractor,
    DEFAULT_DEEPSEEK_TEXT_MODEL,
    DoclingSourceParser,
    FilesystemKnowledgePublisher,
    NormalizedSourceParser,
    PostgresProcessedPaperRegistry,
)
from research_pulse.production.pipeline import ProductionService
from research_pulse.rag.postgres import PostgresResearchRAG
from research_pulse.workflows.production import ProductionGraphDependencies, build_production_graph


def main() -> int:
    load_dotenv(_project_root() / ".env", override=False)
    parser = argparse.ArgumentParser(description="Run one source-bounded Research Pulse production batch.")
    parser.add_argument("--topic", required=True, help="arXiv search topic, for example 'LLM agent memory'.")
    parser.add_argument("--domain", required=True, help="Stable domain tag used for retrieval filtering.")
    parser.add_argument("--limit", type=int, default=3, help="Maximum papers to process (default: 3).")
    parser.add_argument("--model", default=DEFAULT_DEEPSEEK_TEXT_MODEL, help="DeepSeek model for extraction and verification.")
    parser.add_argument("--with-formulas", action="store_true", help="Enable Docling formula enrichment.")
    parser.add_argument(
        "--normalized-root",
        type=Path,
        default=None,
        help="Opt-in source cache root containing <source_id>/normalized/blocks.jsonl; skips PDF download and parsing.",
    )
    parser.add_argument(
        "--reader-root",
        type=Path,
        default=None,
        help="Run the PaperReader reading route (note-only publish) instead of the DeepSeek deep-reader, "
        "reading server-normalized blocks from <root>/<source_id>/normalized/blocks.jsonl.",
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

    if args.reader_root is not None:
        from research_pulse.reader_production import ReaderConfig, ReaderProductionService
        vault_root = args.vault_root or (_project_root() / "knowledge")
        service = ReaderProductionService(ReaderConfig(normalized_root=args.reader_root), vault_root)
        candidates = ArxivCandidateFinder().discover(topic=args.topic, domain=args.domain, limit=args.limit)
        for candidate in candidates:
            receipt = service.process(candidate)
            print(f"{receipt['source_id']}: {receipt['receipt_status']} -> {receipt['published_path']}")
        return 0

    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        parser.error("DATABASE_URL is required; copy the local development value from .env.example.")

    rag = PostgresResearchRAG(database_url)
    registry = PostgresProcessedPaperRegistry(database_url)
    rag.initialize()
    registry.initialize()
    source_parser = (
        NormalizedSourceParser(args.normalized_root)
        if args.normalized_root is not None
        else DoclingSourceParser(with_formulas=args.with_formulas)
    )
    service = ProductionService(
        parser=source_parser,
        extractor=DeepSeekStructuredExtractor.from_environment(model=args.model),
        publisher=FilesystemKnowledgePublisher(
            vault_root=_project_root() / "knowledge", rag=rag, processed_registry=registry
        ),
        processed_registry=registry,
        entailment_judge=DeepSeekEntailmentJudge.from_environment(model=args.model),
        run_deadline_seconds=float(os.getenv("DEEPSEEK_RUN_DEADLINE_SECONDS", "420")),
    )
    graph = build_production_graph(
        ProductionGraphDependencies(candidate_finder=ArxivCandidateFinder(), production_service=service)
    )
    result = graph.invoke(
        {"run_id": "manual", "topic": args.topic, "domain": args.domain, "limit": args.limit}
    )
    for receipt in result.get("receipts", []):
        detail = f" ({receipt['reason']})" if receipt.get("reason") else ""
        print(f"{receipt['source_id']}: {receipt['status']}{detail}")
    return 0


def _project_root():
    from pathlib import Path

    return Path(__file__).resolve().parents[2]


if __name__ == "__main__":
    raise SystemExit(main())
