"""P0 最小闭环在真实论文上的 CLI 入口。

用法::

    python -m research_pulse.pedagogical.runner --source-id 2412.19437v1

解析 ``--normalized-root/<source_id>/normalized/blocks.jsonl`` 为 ``CanonicalPaperIR``；
用真实 ``DeepSeekPaperReadingModel.from_environment()`` 依次跑 S1(PaperModel)→S2(TeachingPlan)→S5(带锚正文)→S6(确定性渲染)，
把最终 ``RenderedNote`` 写到 ``<out>/<source_id>/reading-note.md`` 并把被引用的图片拷贝到 ``<out>/<source_id>/assets/``。
"""

from __future__ import annotations

import argparse
import re
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from dotenv import load_dotenv

from research_pulse.production.normalized import load_complete_normalized
from research_pulse.production.reading import CanonicalPaperIR, DeepSeekPaperReadingModel
from .asset_preparation import AssetPreparation
from .deepseek_adapters import DeepSeekPedagogicalModel
from .pipeline import pipeline_from_parts


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _load_blocks(blocks_path: Path) -> list[SimpleNamespace]:
    import json

    blocks: list[SimpleNamespace] = []
    with blocks_path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            raw = json.loads(line)
            blocks.append(SimpleNamespace(
                block_id=str(raw.get("block_id", "")),
                kind=str(raw.get("kind", "text")),
                text=str(raw.get("text", "")),
                caption=raw.get("caption"),
                latex=raw.get("latex"),
                table_html=raw.get("table_html"),
                image_path=raw.get("image_path"),
                parse_status=str(raw.get("parse_status", "available")),
                section_path=tuple(raw.get("section_path") or ()),
                supported_facets=(),
            ))
    return blocks


def _load_title(blocks: list[SimpleNamespace], manifest: Path | None) -> str:
    try:
        p = manifest
        if p is not None and p.is_file():
            import json
            title = json.loads(p.read_text(encoding="utf-8")).get("title")
            if title:
                return str(title)
    except Exception:
        pass
    for block in blocks:
        if len(block.text.strip()) >= 40 and not block.section_path:
            return block.text.strip()[:120]
    return blocks[0].text.strip()[:120]


def resolve_paper(normalized_root: Path, source_id: str, *, image_subpath: str = "mineru/source/auto") -> CanonicalPaperIR:
    base = normalized_root / source_id / "normalized"
    blocks_path = base / "blocks.jsonl"
    manifest_path = base / "manifest.json"
    if not blocks_path.is_file():
        raise SystemExit(f"No normalized blocks at {blocks_path}")
    source_root = normalized_root / source_id
    blocks = list(load_complete_normalized(
        base,
        expected_source_id=source_id,
        image_roots=(base, source_root / image_subpath),
    ))
    title = _load_title(blocks, manifest_path if manifest_path.is_file() else None)
    paper = CanonicalPaperIR.from_normalized(
        source_id=source_id,
        title=title,
        blocks=blocks,
        source_url=manifest_path.read_text(encoding="utf-8") if manifest_path.is_file() else "",
    )
    # 镜像 ReaderMaterialResolver._resolve_image：先按 <source_id>/<image_path>，再按
    # <source_id>/<image_subpath>/<image_path> 把相对 image_path 解析成绝对本地路径。
    # 否则图即使本地存在（mineru/source/auto/images/）也拷贝不到。
    return replace(paper, blocks=tuple(
        replace(block, image_path=str(resolved))
        if block.image_path and (resolved := _resolve_image(normalized_root, source_id, block.image_path, image_subpath)) else block
        for block in paper.blocks
    ))


def _resolve_image(normalized_root: Path, source_id: str, value: str, image_subpath: str) -> Path | None:
    for candidate in (
        normalized_root / source_id / "normalized" / value,
        normalized_root / source_id / value,
        normalized_root / source_id / image_subpath / value,
    ):
        if candidate.is_file():
            return candidate.resolve()
    return None


def main() -> int:
    load_dotenv(_project_root() / ".env", override=False)
    parser = argparse.ArgumentParser(description="Run the P0 pedagogical pipeline on one real normalized paper.")
    parser.add_argument("--source-id", required=True, help="arXiv source id, e.g. 2412.19437v1")
    parser.add_argument("--normalized-root", type=Path, default=Path("tmp/reading-experiment-root"))
    parser.add_argument("--out", type=Path, default=None, help="Output root (default: tmp/pedagogical-out)")
    args = parser.parse_args()

    model = DeepSeekPaperReadingModel.from_environment()
    print(f"[model] text={model.text_model} vision={model.vision_model}")

    paper = resolve_paper(args.normalized_root, args.source_id)
    print(f"[paper] {paper.source_id} blocks={len(paper.blocks)} title={paper.title[:80]!r}")

    catalog = AssetPreparation.prepare(paper.blocks, asset_root=args.normalized_root)
    assets = catalog.publishable_assets
    candidates = catalog.planner_candidates
    print(f"[assets] renderable={len(assets)} candidate={len(candidates)}")

    out_root = args.out or _project_root() / "tmp" / "pedagogical-out"
    assets_dir = out_root / paper.source_id / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)

    ped_model = DeepSeekPedagogicalModel(model, assets=assets, candidates=candidates)
    pipeline = pipeline_from_parts(
        paper_model_builder=ped_model.build_paper_model,
        teaching_plan_builder=ped_model.build_teaching_plan,
        writer_model=ped_model,
        assets=assets,
        asset_base_url="assets",
    )

    note = pipeline.run(paper)

    out_path = out_root / paper.source_id / "reading-note.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(note.markdown, encoding="utf-8")

    # 拷贝被引用的图片到 assets/ （renderer 按真实文件名引用）
    copied = 0
    referenced = {
        Path(value).name
        for value in re.findall(r"\]\((?:\.?/)?assets/([^)]+)\)", note.markdown)
    }
    for asset in assets.values():
        if asset.image_path and Path(asset.image_path).name in referenced:
            src = Path(asset.image_path)
            if not src.is_absolute():
                src = args.normalized_root / paper.source_id / asset.image_path
            if src.is_file():
                dest = assets_dir / Path(asset.image_path).name
                import shutil
                shutil.copyfile(src, dest)
                copied += 1

    print(f"[written] {out_path}")
    print(f"[images] copied={copied}")
    print(f"[render] rendered={sum(1 for r in note.render_results if r.status == 'rendered')} "
          f"missing={sum(1 for r in note.render_results if r.status == 'missing')} "
          f"unavailable={sum(1 for r in note.render_results if r.status == 'unavailable')}")
    print(f"[markdown] chars={len(note.markdown)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
