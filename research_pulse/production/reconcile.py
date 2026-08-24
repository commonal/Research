"""Explicit CLI for rebuilding ResearchRAG from canonical knowledge bundles."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Callable

from dotenv import load_dotenv

from research_pulse.production.adapters import PostgresProcessedPaperRegistry
from research_pulse.production.publication import ManifestStore, PublicationReconciler
from research_pulse.rag.postgres import PostgresResearchRAG


ReconcilerFactory = Callable[[str, Path], PublicationReconciler]


def main(argv: list[str] | None = None, *, reconciler_factory: ReconcilerFactory | None = None) -> int:
    project_root = Path(__file__).resolve().parents[2]
    load_dotenv(project_root / ".env", override=False)
    parser = argparse.ArgumentParser(
        description="Reconcile canonical Research Pulse Markdown bundles into the rebuildable index."
    )
    parser.add_argument(
        "--vault-root",
        type=Path,
        default=Path(os.getenv("RESEARCH_PULSE_VAULT_ROOT", project_root / "knowledge")),
        help="Canonical knowledge vault (default: RESEARCH_PULSE_VAULT_ROOT or ./knowledge).",
    )
    parser.add_argument(
        "--check-indexed",
        action="store_true",
        help="Idempotently verify manifests already marked indexed as well as pending/failed items.",
    )
    args = parser.parse_args(argv)
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        parser.error("DATABASE_URL is required; copy the local development value from .env.example.")

    reconciler = (reconciler_factory or _build_reconciler)(database_url, args.vault_root)
    summary = reconciler.reconcile(include_indexed=args.check_indexed)
    print(json.dumps(summary.to_payload(), ensure_ascii=False, sort_keys=True))
    return 1 if summary.has_failures else 0


def _build_reconciler(database_url: str, vault_root: Path) -> PublicationReconciler:
    rag = PostgresResearchRAG(database_url)
    registry = PostgresProcessedPaperRegistry(database_url)
    rag.initialize()
    registry.initialize()
    return PublicationReconciler(ManifestStore(vault_root), rag, registry)


if __name__ == "__main__":
    raise SystemExit(main())
