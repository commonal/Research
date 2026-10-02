"""PedagogicalReadingPipeline 编排器（P0 最小教学闭环）。

依赖方向：CanonicalPaperIR → S1 PaperModel → S2 TeachingPlan → S5 NoteDraftWithAnchors → S6 RenderedNote。
S1/S2 为 injectable 适配器（P0 最小/兼容，P3 再强化）；Writer/Renderer 已实现。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from ..production.reading import CanonicalPaperIR
from .contracts import (
    AssetUsageLedger,
    NoteDraftWithAnchors,
    PaperModel,
    PedagogicalResult,
    RenderedNote,
    TeachingPlan,
    validate_contracts,
)
from .renderer import DeterministicRenderer, PublishedAsset
from .writer import PedagogicalWriter


@dataclass(frozen=True)
class PedagogicalReadingPipeline:
    paper_model_builder: Callable[[CanonicalPaperIR], PaperModel]
    teaching_plan_builder: Callable[[PaperModel], TeachingPlan]
    writer: PedagogicalWriter
    renderer: DeterministicRenderer

    def run(self, paper: CanonicalPaperIR) -> RenderedNote:
        paper_model = self.paper_model_builder(paper)
        teaching_plan = self.teaching_plan_builder(paper_model)
        validate_contracts(paper_model, teaching_plan)
        draft = self.writer.write(paper_model, teaching_plan)
        return self.renderer.render(draft)


def pipeline_from_parts(
    *,
    paper_model_builder: Callable[[CanonicalPaperIR], PaperModel],
    teaching_plan_builder: Callable[[PaperModel], TeachingPlan],
    writer_model: Any,
    assets: Mapping[str, PublishedAsset],
    asset_base_url: str = "assets",
) -> PedagogicalReadingPipeline:
    """用给定 builders + 一个 writer model + 已发布资产，组装 P0 最小闭环。"""
    renderer = DeterministicRenderer(assets=assets, asset_base_url=asset_base_url)
    writer = PedagogicalWriter(writer_model)
    return PedagogicalReadingPipeline(paper_model_builder, teaching_plan_builder, writer, renderer)


class PedagogicalService:
    """按论文实时构建教学管线 + 两道门禁并运行 —— 生产线注入用。

    ``ReaderProductionService(mode=pedagogical)`` 无需预先知道每篇论文的资产：
    这里拿到 ``CanonicalPaperIR`` 后现场 ``assets_from_blocks``、驱动
    S1(S2)→S5→S6，再跑 S7 Evidence Gate 与 S8 Blind Reader，返回 ``PedagogicalResult``。
    门禁未过时做**最多一次有界 targeted repair**（只把问题清单喂给 writer 重写一次），
    仍不过则由上层回退 generic。

    注入一个 DeepSeek 读取模型即可；``evidence_gate``/``blind_reader`` 可注入假 judging 便于测试。
    """

    def __init__(
        self,
        model: Any,
        *,
        asset_base_url: str = "assets",
        asset_root: Path | None = None,
        source_quality_gate: Any | None = None,
        evidence_gate: Any | None = None,
        artifact_gate: Any | None = None,
        blind_reader: Any | None = None,
        max_repairs: int = 1,
    ) -> None:
        self.model = model
        self.asset_base_url = asset_base_url
        self.asset_root = asset_root
        self.max_repairs = max_repairs
        self._source_quality_gate = source_quality_gate
        self._evidence_gate = evidence_gate
        self._artifact_gate = artifact_gate
        self._blind_reader = blind_reader

    def _gates(self) -> tuple[Any, Any, Any]:
        from .artifact_quality import ArtifactQualityGate
        from .gates import BlindReader, EvidenceGate

        return (
            self._evidence_gate or EvidenceGate(self.model),
            self._artifact_gate or ArtifactQualityGate(),
            self._blind_reader or BlindReader(self.model),
        )

    def run(self, paper: CanonicalPaperIR) -> PedagogicalResult:
        from dataclasses import asdict

        from .asset_preparation import AssetPreparation
        from .deepseek_adapters import DeepSeekPedagogicalModel
        from .interpreter import interpret_assets
        from .planner import SemanticAssetPlanner
        from .renderer import DeterministicRenderer
        from .source_quality import SourceQualityGate, SourceQualityRejected
        from .writer import PedagogicalWriter

        source_quality = (self._source_quality_gate or SourceQualityGate()).evaluate(paper)
        if not source_quality.passed:
            raise SourceQualityRejected(source_quality)

        catalog = AssetPreparation.prepare(paper.blocks, asset_root=self.asset_root)
        assets = catalog.publishable_assets
        planner_candidates = catalog.planner_candidates

        # S1/S2 必须先于 S3：Planner 需要知道论文主张和教学目标，不能只按素材出现顺序选。
        model_builder = DeepSeekPedagogicalModel(
            self.model,
            assets=assets,
            candidates=planner_candidates,
        )
        paper_model = model_builder.build_paper_model(paper)
        teaching_plan = model_builder.build_teaching_plan(paper_model)
        validate_contracts(paper_model, teaching_plan)
        plan = SemanticAssetPlanner(self.model).plan(
            paper_model,
            teaching_plan,
            planner_candidates,
        )
        ped = DeepSeekPedagogicalModel(
            self.model,
            assets=assets,
            candidates=planner_candidates,
            plan=plan,
        )
        evidence_gate, artifact_gate, blind_reader = self._gates()
        writer = PedagogicalWriter(ped)
        renderer = DeterministicRenderer(assets=assets, asset_base_url=self.asset_base_url)

        asset_briefs = interpret_assets(self.model, plan, assets)   # S4：公式/表 briefs（图 caption 模式）
        briefs = {
            "selected_asset_ids": tuple(
                choice.asset_id for choice in plan.choices if choice.decision in {"inline", "reference"}
            ),
            "asset_briefs": asdict(asset_briefs),
        }
        draft = writer.write(paper_model, teaching_plan, briefs=briefs)
        note = renderer.render(draft)

        evidence = evidence_gate.evaluate(note, paper_model)
        artifact = artifact_gate.evaluate(note)
        blind = blind_reader.read(note)
        repaired = False

        # 零锚定判定：论文有资产候选（图/表/公式）但正文一个都没锚 → 视为门禁未过。
        # 这是教学化管线区别于纯文字的底线（S3/S4 的职责），盲读对"没有图表可讲"偏宽容，
        # 不能让它把零锚定放行（2312 实测 v4-flash 偶发不写 asset anchor）。
        def _usage(draft_: NoteDraftWithAnchors, note_: RenderedNote) -> AssetUsageLedger:
            selected = tuple(
                choice.asset_id for choice in plan.choices if choice.decision in {"inline", "reference"}
            )
            anchored = tuple(dict.fromkeys(anchor.asset_id for anchor in draft_.anchors))
            rendered = tuple(dict.fromkeys(
                result.anchor_id for result in note_.render_results if result.status == "rendered"
            ))
            return AssetUsageLedger(
                selected_ids=selected,
                anchored_ids=anchored,
                rendered_ids=rendered,
                unreferenced_selected_ids=tuple(asset_id for asset_id in selected if asset_id not in anchored),
                failed_render_ids=tuple(asset_id for asset_id in anchored if asset_id not in rendered),
            )

        usage = _usage(draft, note)
        zero_anchor = bool(usage.selected_ids) and not usage.anchored_ids

        # 有界 targeted repair：门禁未过 → 只把问题清单喂给 writer 重写一次。
        if (
            not evidence.passed
            or not artifact.passed
            or blind.overall in ("needs_targeted_revision", "fail")
            or zero_anchor
            or usage.unreferenced_selected_ids
            or usage.failed_render_ids
        ) and self.max_repairs > 0:
            issues = list(evidence.issues)
            issues.extend(
                f"artifact:{issue.code}@line={issue.line or 'unknown'}:{issue.message}"
                for issue in artifact.issues
                if issue.severity == "error"
            )
            issues.extend(f"blind:{dim.dimension}={dim.score}" for dim in blind.dimensions if dim.score == "missing")
            if zero_anchor:
                issues.append(
                    "rendered:zero_asset_anchors —— 正文没有引用任何资产锚点；"
                    "必须在正文中用 {{asset:<asset_id>}} 标记图片/表格/公式的位置"
                )
            if usage.unreferenced_selected_ids:
                issues.append(
                    "writer:unreferenced_selected_assets="
                    + ",".join(usage.unreferenced_selected_ids)
                    + " —— Planner 已选中的每个素材都必须在正文中完成引用与解释"
                )
            if usage.failed_render_ids:
                issues.append("rendered:failed_assets=" + ",".join(usage.failed_render_ids))
            draft = writer.write(
                paper_model, teaching_plan,
                briefs={"repair": issues, **briefs},      # repair 轮保留 S4 briefs
            )
            note = renderer.render(draft)
            repaired = True
            evidence = evidence_gate.evaluate(note, paper_model)
            artifact = artifact_gate.evaluate(note)
            blind = blind_reader.read(note)
            usage = _usage(draft, note)
            zero_anchor = bool(usage.selected_ids) and not usage.anchored_ids

        return PedagogicalResult(
            note=note,
            evidence=evidence,
            blind=blind,
            artifact=artifact,
            source_quality=source_quality,
            repaired=repaired,
            zero_anchors=zero_anchor,
            asset_usage=usage,
        )
