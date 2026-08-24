"""Command-line entrypoint for the Research Pulse worker."""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path
import json

from worker.analyze import analyze_abstract, api_key_from_environment, render_markdown, write_markdown
from worker.discover import Subscription, fetch_candidates, write_candidates
from worker.fulltext import parse_fulltext, write_parse_receipt


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description="Research Pulse worker")
    subparsers = parser.add_subparsers(dest="command", required=True)
    discover_parser = subparsers.add_parser("discover", help="Fetch arXiv paper candidates")
    discover_parser.add_argument(
        "--subscription",
        type=Path,
        required=True,
        help="Path to a subscription JSON file.",
    )
    discover_parser.add_argument(
        "--output",
        type=Path,
        help="Optional JSON output path. Defaults to data/candidates/<name>-<date>.json.",
    )
    analyze_parser = subparsers.add_parser(
        "analyze-abstract",
        help="Create an explicitly abstract-only Markdown draft with DeepSeek.",
    )
    analyze_parser.add_argument("--candidates", type=Path, required=True, help="Path to a candidate JSON output.")
    analyze_parser.add_argument("--source-id", required=True, help="arXiv source ID to analyze.")
    analyze_parser.add_argument("--model", default="deepseek-v4-flash", help="DeepSeek chat model.")
    analyze_parser.add_argument("--output", type=Path, help="Optional Markdown output path.")
    fulltext_parser = subparsers.add_parser(
        "parse-fulltext",
        help="Temporarily parse an arXiv PDF with Docling and save a metadata-only receipt.",
    )
    fulltext_parser.add_argument("--candidates", type=Path, required=True, help="Path to a candidate JSON output.")
    fulltext_parser.add_argument("--source-id", required=True, help="arXiv source ID to parse.")
    fulltext_parser.add_argument(
        "--with-formulas",
        action="store_true",
        help="Enable Docling formula enrichment; it may fetch local model assets on first use.",
    )
    fulltext_parser.add_argument("--timeout", type=int, default=120, help="PDF download and Docling timeout in seconds.")
    fulltext_parser.add_argument("--output", type=Path, help="Optional metadata receipt JSON path.")
    args = parser.parse_args()

    if args.command == "discover":
        return _discover(args.subscription, args.output)
    if args.command == "analyze-abstract":
        return _analyze_abstract(args.candidates, args.source_id, args.model, args.output)
    if args.command == "parse-fulltext":
        return _parse_fulltext(
            args.candidates, args.source_id, args.with_formulas, args.timeout, args.output
        )
    raise AssertionError("argparse should reject unknown commands")


def _discover(subscription_path: Path, output_path: Path | None) -> int:
    subscription = Subscription.from_file(subscription_path)
    candidates = fetch_candidates(subscription)
    resolved_output = output_path or (
        PROJECT_ROOT / "data" / "candidates" / f"{subscription.name}-{date.today().isoformat()}.json"
    )
    write_candidates(subscription, candidates, resolved_output)
    print(f"Found {len(candidates)} arXiv candidates.")
    print(f"Saved review queue to {resolved_output}")
    return 0


def _analyze_abstract(candidates_path: Path, source_id: str, model: str, output_path: Path | None) -> int:
    candidate = _candidate_from_file(candidates_path, source_id)
    analysis = analyze_abstract(candidate, api_key_from_environment(), model)
    markdown = render_markdown(candidate, analysis)
    resolved_output = output_path or (
        PROJECT_ROOT / "knowledge" / "generated" / f"{candidate.source_id}-abstract-only.md"
    )
    write_markdown(markdown, resolved_output)
    print(f"Saved abstract-only Markdown draft to {resolved_output}")
    return 0


def _parse_fulltext(
    candidates_path: Path,
    source_id: str,
    with_formulas: bool,
    timeout_seconds: int,
    output_path: Path | None,
) -> int:
    candidate = _candidate_from_file(candidates_path, source_id)
    parsed = parse_fulltext(candidate, with_formulas=with_formulas, timeout_seconds=timeout_seconds)
    resolved_output = output_path or (PROJECT_ROOT / "data" / "parse-receipts" / f"{candidate.source_id}.json")
    write_parse_receipt(parsed, resolved_output)
    formula_message = "已启用公式增强" if with_formulas else "未启用公式增强"
    print(f"Parsed {parsed.character_count} characters with Docling ({formula_message}).")
    print("Source PDF and parsed full text were deleted after this run.")
    print(f"Saved metadata-only parse receipt to {resolved_output}")
    return 0


def _candidate_from_file(candidates_path: Path, source_id: str):
    payload = json.loads(candidates_path.read_text(encoding="utf-8"))
    matching = [item for item in payload.get("candidates", []) if item.get("source_id") == source_id]
    if len(matching) != 1:
        raise ValueError(f"Expected exactly one candidate with source_id='{source_id}'.")
    from worker.discover import PaperCandidate

    return PaperCandidate(**matching[0])


if __name__ == "__main__":
    raise SystemExit(main())
