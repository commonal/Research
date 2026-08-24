from __future__ import annotations

"""Run Mamba or PaperBench through the production PaperReader Interface."""

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import argparse
import json
import os
import sys


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from research_pulse.production.normalized import load_normalized_jsonl
from research_pulse.production.pipeline import PaperCandidate
from research_pulse.production.reading import CanonicalPaperIR, DeepSeekPaperReadingModel, PaperReader, ReadingIntent, _jsonable


PAPERS = {
    "mamba": {
        "source_id": "2312.00752v2",
        "title": "Mamba: Linear-Time Sequence Modeling with Selective State Spaces",
        "source_url": "https://arxiv.org/abs/2312.00752v2",
        "expected": ("Mamba", "selective", "state space", "linear", "Transformer"),
    },
    "paperbench": {
        "source_id": "2504.01848v3",
        "title": "PaperBench: Evaluating AI's Ability to Replicate AI Research",
        "source_url": "https://arxiv.org/abs/2504.01848v3",
        "expected": ("PaperBench", "rubric", "judge", "human", "replication"),
    },
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--paper", choices=tuple(PAPERS), required=True)
    parser.add_argument("--response-cache", default="")
    parser.add_argument("--writer-cache", default="")
    parser.add_argument("--output-suffix", default="v1")
    args = parser.parse_args()
    config = PAPERS[args.paper]
    source_id = str(config["source_id"])
    source_root = ROOT / "tmp" / "reading-experiment-root" / source_id
    output = ROOT / "evals" / "real-e2e" / f"production-generalization-{args.paper}-{args.output_suffix}"
    output.mkdir(parents=True, exist_ok=True)
    _load_dotenv()

    candidate = PaperCandidate(source_id, str(config["title"]), str(config["source_url"]), "generalization")
    normalized = load_normalized_jsonl(source_root / "normalized" / "blocks.jsonl")
    paper = CanonicalPaperIR.from_normalized(
        source_id=source_id,
        title=candidate.title,
        source_url=candidate.source_url,
        blocks=normalized,
    )
    paper = replace(paper, blocks=tuple(
        replace(block, image_path=str(resolved))
        if block.image_path and (resolved := _resolve_image(source_root, block.image_path)) else block
        for block in paper.blocks
    ))

    model = DeepSeekPaperReadingModel.from_environment()
    _write(output, "provider-probe.json", model.probe())
    cache_path = Path(args.response_cache).resolve() if args.response_cache else None
    if cache_path is not None:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))

        def cached_full_read(_request: object) -> dict:
            model.last_full_paper_response = cached
            return dict(cached)

        model.read_full_paper = cached_full_read  # type: ignore[method-assign]

    writer_input: dict[str, object] = {}
    writer_cache_path = Path(args.writer_cache).resolve() if args.writer_cache else None
    cached_writer_markdown = writer_cache_path.read_text(encoding="utf-8") if writer_cache_path is not None else None

    def writer(value: dict) -> str:
        writer_input.update(value)
        return cached_writer_markdown if cached_writer_markdown is not None else model.write_note(value)

    started = datetime.now(timezone.utc).isoformat()
    result = PaperReader(model, writer=writer).read(
        candidate,
        paper,
        ReadingIntent(language="zh-CN", depth="deep", max_targets=6, max_expansions_per_target=1, max_text_calls=20, max_vision_calls=4),
    )
    _write(output, "paper-model.json", result.trace.paper_model)
    _write(output, "raw-paper-model-response.json", model.last_full_paper_response)
    _write(output, "last-visual-response.json", model.last_visual_response)
    _write(output, "reading-trace.json", result.trace)
    _write(output, "reading-receipt.json", result.receipt)
    _write(output, "reading-draft.json", result.draft)
    _write(output, "writer-input.json", writer_input)
    (output / "reading-note.md").write_text(result.draft.markdown, encoding="utf-8")
    note_folded = result.draft.markdown.casefold()
    _write(output, "generalization-check.json", {
        "paper": args.paper,
        "status": result.receipt.status,
        "expected_hits": {term: str(term).casefold() in note_folded for term in config["expected"]},
        "formula_block_count": sum(block.kind == "formula" for block in paper.ordered_blocks),
        "table_block_count": sum(block.kind == "table" for block in paper.ordered_blocks),
        "figure_block_count": sum(block.is_visual for block in paper.ordered_blocks),
        "raw_block_ids": "normalized:" in result.draft.markdown,
        "markdown_image_links": "![" in result.draft.markdown,
        "characters": len(result.draft.markdown),
    })
    _write(output, "run-meta.json", {
        "started_at": started,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "source_id": source_id,
        "ordered_blocks": len(paper.ordered_blocks),
        "ordered_characters": sum(len(block.text) for block in paper.ordered_blocks),
        "full_paper_cache_hit": cache_path is not None,
        "full_paper_cache_source": str(cache_path) if cache_path is not None else None,
        "writer_cache_hit": writer_cache_path is not None,
        "writer_cache_source": str(writer_cache_path) if writer_cache_path is not None else None,
    })
    print(json.dumps({"output": str(output), "receipt": _jsonable(result.receipt)}, ensure_ascii=False, indent=2))
    return 0


def _resolve_image(source_root: Path, value: str) -> Path | None:
    candidates = (
        source_root / value,
        source_root / "mineru" / "source" / "auto" / value,
        source_root / "mineru" / "source" / "auto" / "images" / Path(value).name,
    )
    return next((item.resolve() for item in candidates if item.is_file()), None)


def _load_dotenv() -> None:
    path = ROOT / ".env"
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        os.environ.setdefault(name.strip(), value.strip().strip('"').strip("'"))


def _write(output: Path, name: str, value: object) -> None:
    (output / name).write_text(json.dumps(_jsonable(value), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
