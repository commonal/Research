"""确定性的资产 Renderer（S6）。契约见 SPEC §9.2。

Renderer 只许做四件事：`resolve asset → validate availability → render markdown → record render result`。
**不做任何语义判断、不调用 LLM、不猜测位置**。纯字符串替换，稳定可测。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .contracts import NoteDraftWithAnchors, RenderResult, RenderedNote


@dataclass(frozen=True)
class PublishedAsset:
    """一个已选中的可发布资产（图片或表格的成品 markdown）。"""

    asset_id: str
    kind: str                       # formula | table | figure
    markdown: str | None = None     # 预渲染 markdown（如表格）
    image_path: str | None = None   # 图片源路径
    caption: str = ""
    selected_format: str | None = None  # markdown_table | latex | image；None 保持旧行为
    image_verified: bool = False    # 已由 AssetPreparation 验证边界、存在性、解码与尺寸


@dataclass(frozen=True)
class DeterministicRenderer:
    """把 ``{{asset:<asset_id>}}`` 标记替换为真实成品，并逐条记录 render result。"""

    assets: Mapping[str, PublishedAsset]
    asset_base_url: str = "assets"

    def render(self, draft: NoteDraftWithAnchors) -> RenderedNote:
        results: list[RenderResult] = []
        markdown = draft.markdown
        markers = list(dict.fromkeys(re.findall(r"\{\{asset:([A-Za-z0-9_.-]+)\}\}", markdown)))
        for asset_id in markers:
            asset = self.assets.get(asset_id)
            if asset is None:
                results.append(RenderResult(anchor_id=asset_id, status="missing"))
                continue
            rendered = self._render_markdown(asset)
            if rendered is None:
                results.append(RenderResult(anchor_id=asset_id, status="unavailable"))
                continue
            replacement = f"\n\n{rendered}\n\n"
            marker = re.escape(f"{{{{asset:{asset_id}}}}}")
            # 兼容 Writer 偶发把 anchor 当图片 URL 包装的情况；必须整体替换，避免嵌套坏 Markdown。
            # replacement 可能含 LaTeX（如 ``\pm``）；callable replacement 可避免
            # ``re.sub`` 把素材正文再次当作 replacement escape 解析。
            markdown = re.sub(
                rf"!\[[^\]]*\]\(\s*{marker}\s*\)",
                lambda _match, value=replacement: value,
                markdown,
            )
            markdown = markdown.replace(f"{{{{asset:{asset_id}}}}}", replacement)
            results.append(RenderResult(anchor_id=asset_id, status="rendered", markdown=rendered))
        return RenderedNote(markdown=markdown, render_results=tuple(results))

    def _render_markdown(self, asset: PublishedAsset) -> str | None:
        if asset.selected_format == "image":
            return self._render_image(asset, fallback=asset.kind in {"table", "formula"})
        if asset.markdown:
            return asset.markdown
        if asset.image_path:
            return self._render_image(asset, fallback=asset.kind in {"table", "formula"})
        return None

    def _render_image(self, asset: PublishedAsset, *, fallback: bool) -> str | None:
        if not asset.image_path:
            return None
        base = self.asset_base_url.rstrip("/")
        url = Path(asset.image_path).name
        # 原始 caption 是 Writer/Interpreter 的理解材料，不是 Renderer 应再次展示的正文。
        # 完整 caption 同时出现在 alt 与斜体会造成英文重复；这里只保留稳定的中文素材标签。
        rendered = f"![{_image_alt(asset)}]({base}/{url})"
        if fallback:
            rendered += "\n\n> 结构化解析质量不足，此处保留论文原图；精确内容以原图为准。"
        return rendered


def _image_alt(asset: PublishedAsset) -> str:
    labels = {"figure": "图", "table": "表", "formula": "公式"}
    label = labels.get(asset.kind, "论文素材")
    caption = asset.caption or ""
    patterns = {
        "figure": r"(?:figure|fig\.?|图)\s*([A-Za-z]?\d+)",
        "table": r"(?:table|tab\.?|表)\s*([A-Za-z]?\d+)",
        "formula": r"(?:equation|eq\.?|formula|公式)\s*([A-Za-z]?\d+)",
    }
    match = re.search(patterns.get(asset.kind, r"$^"), caption, re.IGNORECASE)
    return f"{label} {match.group(1)}" if match else f"论文{label}"
