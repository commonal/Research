from __future__ import annotations

"""Real target-paper replay through the production full-paper B path."""

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import json
import os
import sys


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from research_pulse.production.normalized import load_normalized_jsonl
from research_pulse.production.pipeline import PaperCandidate
from research_pulse.production.reading import CanonicalPaperIR, DeepSeekPaperReadingModel, PaperReader, ReadingIntent, _jsonable


SOURCE_ID = "2608.18351v1"
NORMALIZED = ROOT / "tmp" / "reading-experiment-root" / SOURCE_ID / "normalized" / "blocks.jsonl"
IMAGE_ROOT = ROOT / "tmp" / "reading-experiment-root" / SOURCE_ID
OUTPUT = ROOT / "evals" / "real-e2e" / "full-paper-b-production-v43-wholenote"
GOLDEN = ROOT / "evals" / "real-e2e" / "2608.18351v1-golden-human-note.md"


def main() -> int:
    _load_dotenv()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    started = datetime.now(timezone.utc).isoformat()
    candidate = PaperCandidate(
        SOURCE_ID,
        "Task-Conditioned Least-Privilege Learning for Executable Terminal and MCP Agents",
        "https://arxiv.org/abs/2608.18351v1",
        "security",
    )
    normalized = load_normalized_jsonl(NORMALIZED)
    paper = CanonicalPaperIR.from_normalized(
        source_id=SOURCE_ID,
        title=candidate.title,
        source_url=candidate.source_url,
        blocks=normalized,
    )
    paper = replace(paper, blocks=tuple(
        replace(block, image_path=str(resolved))
        if block.image_path and (resolved := _resolve_image(block.image_path)) else block
        for block in paper.blocks
    ))

    model = DeepSeekPaperReadingModel.from_environment()
    _write("provider-probe.json", model.probe())
    cache_path_value = os.getenv("READING_FULL_RESPONSE_CACHE", "").strip()
    cache_path = Path(cache_path_value).resolve() if cache_path_value else None
    if cache_path is not None:
        cached_response = json.loads(cache_path.read_text(encoding="utf-8"))

        def cached_full_paper_read(_request: object) -> dict:
            model.last_full_paper_response = cached_response
            return dict(cached_response)

        model.read_full_paper = cached_full_paper_read  # type: ignore[method-assign]
    planner_input: dict[str, object] = {}
    writer_input: dict[str, object] = {}

    def planner(value: dict) -> dict:
        planner_input.update(value)
        return dict(model.plan_note(value))

    def writer(value: dict) -> str:
        writer_input.update(value)
        return model.write_note(value)

    result = PaperReader(model, note_planner=planner, writer=writer).read(
        candidate,
        paper,
        ReadingIntent(language="zh-CN", depth="deep", max_targets=6, max_expansions_per_target=1, max_text_calls=20, max_vision_calls=4),
    )
    _write("paper-model.json", result.trace.paper_model)
    _write("raw-paper-model-response.json", model.last_full_paper_response)
    _write("visual-decisions.json", result.trace.visual_decisions)
    _write("last-visual-response.json", model.last_visual_response)
    _write("reading-trace.json", result.trace)
    _write("reading-receipt.json", result.receipt)
    _write("reading-draft.json", result.draft)
    _write("planner-input.json", planner_input)
    _write("writer-input.json", writer_input)
    (OUTPUT / "reading-note.md").write_text(result.draft.markdown, encoding="utf-8")
    comparison = _compare(result.draft.markdown)
    _write("comparison.json", comparison)
    _write("run-meta.json", {
        "started_at": started,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "blocks": len(paper.blocks),
        "strategy": result.receipt.strategy,
        "text_calls": result.receipt.text_calls,
        "vision_calls": result.receipt.vision_calls,
        "target_count": result.receipt.target_count,
        "degradations": result.receipt.degradations,
        "resolved_text_model": result.receipt.resolved_text_model,
        "resolved_vision_model": result.receipt.resolved_vision_model,
        "full_paper_cache_hit": cache_path is not None,
        "full_paper_cache_source": str(cache_path) if cache_path is not None else None,
    })
    print(json.dumps({"output": str(OUTPUT), "receipt": _jsonable(result.receipt), "comparison": comparison}, ensure_ascii=False, indent=2))
    return 0


def _compare(note: str) -> dict[str, object]:
    expected = (
        "Qwen3.5-4B", "broker", "64.36%", "98.48%", "4.56%", "0.79%",
        "P", "U", "H", "B", "F_u", "F_r", "权限门控", "沙箱",
    )
    wrong_meanings = (
        "P表示持久性", "P 表示持久性", "U表示未使用工具", "U 表示未使用工具",
        "H表示帮助性", "H 表示帮助性", "B表示破坏性", "B 表示破坏性",
    )
    return {
        "characters": len(note),
        "expected_hits": {item: item.casefold() in note.casefold() for item in expected},
        "wrong_reward_meanings": [item for item in wrong_meanings if item in note],
        "raw_block_ids": "normalized:" in note,
        "golden_characters": len(GOLDEN.read_text(encoding="utf-8")) if GOLDEN.is_file() else 0,
    }


def _resolve_image(value: str) -> Path | None:
    candidates = (IMAGE_ROOT / value, IMAGE_ROOT / "mineru" / "source" / "auto" / value)
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


def _write(name: str, value: object) -> None:
    (OUTPUT / name).write_text(json.dumps(_jsonable(value), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
