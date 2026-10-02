"""Orchestration for the independent MinerU traceable-reading path."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from shutil import copy2
from time import monotonic
from typing import Any, Mapping, Protocol, Sequence
import json
import re

from .asset_quality import table_html_to_markdown
from .contracts import (
    AssetDecision, AssetRef, AssetUse, EvidenceAssessment, EvidenceRequirement, Finding, MaterialDocument,
    DEFAULT_WRITER_POLICY, NoteBlock, NoteDraft, NotePlan, NoteSection, PaperMap, ReadingAnswer, ReadingCoverage,
    ReadingObligation, ReadingPacket, ReadingPlan, RequirementResult, RunReceipt,
    ExperimentUnitPlan, SectionNotePlan, SourceBlockRef, SourceSpan, SurveyResult, WorkedExamplePlan, WriterPolicy,
)


class TraceableReadingError(RuntimeError):
    """Safe stage failure without prompts, source text, or provider responses."""


class JsonOperationModel(Protocol):
    def call_json(self, operation: str, prompt: str) -> Mapping[str, Any]: ...


@dataclass
class DeepSeekOperationAdapter:
    """Narrow adapter around the existing DeepSeek provider boundary."""

    provider: Any

    def call_json(self, operation: str, prompt: str) -> Mapping[str, Any]:
        return self.provider.call_json(operation, self.provider.text_model, prompt)


def build_survey_view(document: MaterialDocument, *, max_chars: int = 120_000) -> str:
    markdown = document.full_markdown()
    outline = "\n".join(f"- [{s.section_id}] {'  ' * max(0, s.level - 1)}{s.title}" for s in document.sections)
    assets = "\n".join(f"- [{a.asset_id}] {a.kind}: {a.caption}" for a in document.assets)
    if len(markdown) <= max_chars:
        body = markdown
    else:
        selected: list[str] = []
        for section in document.sections:
            blocks = document.blocks_for_section(section.section_id, neighbor_count=0)
            sample = "\n".join(block.text for block in blocks[:3])
            selected.append(f"## {section.title}\n{sample}")
        body = "\n\n".join(selected)
        body = body[:max_chars]
    return f"论文目录：\n{outline}\n\n可用资产：\n{assets or '- 无'}\n\n论文阅读视图：\n{body}"


class SurveyReader:
    def __init__(self, model: JsonOperationModel, *, max_chars: int = 120_000) -> None:
        self.model = model
        self.max_chars = max_chars

    def read(self, document: MaterialDocument) -> SurveyResult:
        prompt = json.dumps({
            "instruction": "整体速读论文，返回论文导航图与可验收阅读计划。evidence_requirements 描述要核查的维度，不得预设结论。核心 facet 必须包含 problem、method、experiments、limitations；problem 的导航描述必须同时交代研究背景、已有方法缺口和本文要解决的问题，不能只写一个结论标签。只使用给定 section_id 和 asset_id。",
            "material_version": document.material_version,
            "survey_view": build_survey_view(document, max_chars=self.max_chars),
            "required_json": {
                "paper_map": {"paper_type": "", "problem": "", "approach": "", "experiments": "", "limitations": "", "important_asset_ids": [], "open_questions": []},
                "obligations": [{
                    "obligation_id": "", "question": "", "purpose": "",
                    "facet": "problem|method|experiments|limitations", "core": True,
                    "target_section_ids": [], "target_asset_ids": [],
                    "evidence_requirements": [{"requirement_id": "", "description": "", "required": True}],
                }],
            },
        }, ensure_ascii=False)
        payload = self.model.call_json("paper_survey", prompt)
        return _parse_survey(payload, document)


def build_reading_packets(document: MaterialDocument, plan: ReadingPlan) -> tuple[ReadingPacket, ...]:
    packets: list[ReadingPacket] = []
    grouped: dict[tuple[tuple[str, ...], tuple[str, ...]], list[ReadingObligation]] = {}
    for obligation in plan.obligations:
        grouped.setdefault((obligation.target_section_ids, obligation.target_asset_ids), []).append(obligation)
    for (section_ids, asset_ids), obligations in grouped.items():
        selected: dict[str, Any] = {}
        for section_id in section_ids:
            for block in document.blocks_for_section(section_id, neighbor_count=1):
                selected[block.block_id] = block
        assets = tuple(asset for asset in document.assets if asset.asset_id in asset_ids)
        for asset in assets:
            if asset.block_id:
                selected[asset.block_id] = document.block(asset.block_id)
        if not selected:
            raise TraceableReadingError(f"reading_packet_empty:{obligations[0].obligation_id}")
        blocks = tuple(sorted(selected.values(), key=lambda item: item.order))
        identity = "|".join(item.obligation_id for item in obligations)
        packets.append(ReadingPacket(f"packet:{sha256(identity.encode()).hexdigest()[:12]}", document.material_version, tuple(obligations), blocks, assets))
    return tuple(packets)


class FindingReader:
    def __init__(self, model: JsonOperationModel, *, deadline_seconds: float = 600.0) -> None:
        self.model = model
        self.deadline_seconds = deadline_seconds

    def read_all(self, packets: Sequence[ReadingPacket]) -> tuple[ReadingAnswer, ...]:
        deadline = monotonic() + self.deadline_seconds
        answers: list[ReadingAnswer] = []
        for packet in packets:
            if monotonic() >= deadline:
                raise TraceableReadingError("finding_read_deadline")
            answers.extend(self._read_packet(packet))
        return tuple(answers)

    def _read_packet(self, packet: ReadingPacket) -> tuple[ReadingAnswer, ...]:
        allowed_ids = {block.block_id for block in packet.blocks}
        material = "\n\n".join(
            f"[{block.block_id} | page:{block.page} | {block.kind}]\n{block.text}" for block in packet.blocks
        )
        prompt = json.dumps({
            "instruction": "精读限定材料。先对每个问题形成完整自然答案，再把答案中关于论文的关键事实拆成 Findings。若材料包给出图片、表格或公式，用中文说明其在回答中的作用并返回 asset_uses；asset_uses 只能引用给定 asset_id 和当前 packet 的 Finding。每个 block_refs 值必须从下方材料标签逐字符复制完整 block_id（material:...），不得使用序号、页码、locator、缩写或自行生成 ID。没有说明时返回 not_stated 或 unclear，不得补造。",
            "material_version": packet.material_version,
            "obligations": [asdict(item) for item in packet.obligations],
            "assets": [{"asset_id": item.asset_id, "kind": item.kind, "caption": item.caption, "block_id": item.block_id} for item in packet.assets],
            "material": material,
            "required_json": {"answers": [{"answer_id": "", "obligation_id": "", "status": "answered|partially_answered|not_stated|unclear", "answer": "", "findings": [{"finding_id": "", "statement": "", "answer_quote": "", "requirement_id": "", "support_kind": "direct|synthesis|reader_inference", "block_refs": []}], "requirement_results": [{"requirement_id": "", "status": "answered|partially_answered|not_stated|unclear", "finding_ids": []}], "asset_uses": [{"asset_id": "", "explanation": "", "finding_refs": []}]}]},
        }, ensure_ascii=False)
        try:
            payload = self.model.call_json("finding_read", prompt)
            if payload.get("_fallback"):
                raise TraceableReadingError(f"finding_provider_{payload['_fallback']}")
            return _parse_answers(payload, packet, allowed_ids)
        except TraceableReadingError:
            raise
        except Exception as exc:
            raise TraceableReadingError(f"finding_read_failed:{type(exc).__name__}") from exc


def resolve_source_spans(document: MaterialDocument, answers: Sequence[ReadingAnswer]) -> tuple[SourceSpan, ...]:
    spans: list[SourceSpan] = []
    for answer in answers:
        for finding in answer.findings:
            resolved = [document.block(block_id) for block_id in finding.block_refs]
            groups: list[list[Any]] = []
            for block in sorted(resolved, key=lambda item: item.order):
                if not groups or block.order > groups[-1][-1].order + 1:
                    groups.append([block])
                else:
                    groups[-1].append(block)
            for index, group in enumerate(groups):
                refs = tuple(SourceBlockRef(block.block_id, block.material_version, block.locator, block.page, block.bbox, block.text[:1000]) for block in group)
                identity = "|".join(ref.block_id for ref in refs)
                spans.append(SourceSpan(f"span:{sha256((finding.finding_id + identity).encode()).hexdigest()[:16]}:{index}", finding.finding_id, document.source_id, document.material_version, refs))
    return tuple(spans)


class BasicEvidenceGate:
    def evaluate(self, document: MaterialDocument, packet_ids: set[str], answers: Sequence[ReadingAnswer], spans: Sequence[SourceSpan]) -> EvidenceAssessment:
        issues: list[str] = []
        span_findings = {span.finding_id for span in spans}
        for answer in answers:
            for finding in answer.findings:
                if not finding.block_refs:
                    issues.append(f"empty_block_refs:{finding.finding_id}")
                if finding.finding_id not in span_findings:
                    issues.append(f"missing_source_span:{finding.finding_id}")
                for block_id in finding.block_refs:
                    if block_id not in packet_ids:
                        issues.append(f"block_outside_packet:{finding.finding_id}")
                        continue
                    try:
                        block = document.block(block_id)
                    except KeyError:
                        issues.append(f"unknown_block:{finding.finding_id}")
                        continue
                    if block.material_version != document.material_version:
                        issues.append(f"material_version_mismatch:{finding.finding_id}")
                    if len(block.bbox) != 4:
                        issues.append(f"invalid_bbox:{finding.finding_id}")
        return EvidenceAssessment("invalid" if issues else "valid", "not_evaluated", tuple(sorted(set(issues))))


def build_coverage(plan: ReadingPlan, answers: Sequence[ReadingAnswer], assessment: EvidenceAssessment) -> ReadingCoverage:
    by_id = {answer.obligation_id: answer for answer in answers}
    obligation_statuses = {item.obligation_id: by_id[item.obligation_id].status if item.obligation_id in by_id else "unclear" for item in plan.obligations}
    requirement_statuses: dict[str, Any] = {}
    for answer in answers:
        requirement_statuses.update({item.requirement_id: item.status for item in answer.requirement_results})
    core_complete = assessment.reference_status == "valid" and all(
        obligation_statuses[item.obligation_id] in {"answered", "partially_answered"}
        for item in plan.obligations if item.core
    )
    return ReadingCoverage(obligation_statuses, requirement_statuses, core_complete)


def plan_asset_decisions(document: MaterialDocument, survey: SurveyResult, answers: Sequence[ReadingAnswer]) -> tuple[AssetDecision, ...]:
    important = set(survey.paper_map.important_asset_ids)
    obligation_assets = {item.obligation_id: set(item.target_asset_ids) for item in survey.reading_plan.obligations}
    targeted = set().union(*obligation_assets.values()) if obligation_assets else set()
    required_ids = important | targeted
    asset_facets: dict[str, set[str]] = {}
    for obligation in survey.reading_plan.obligations:
        for asset_id in obligation.target_asset_ids:
            asset_facets.setdefault(asset_id, set()).add(obligation.facet)
    order = {item.asset_id: index for index, item in enumerate(document.assets)}
    budgets = {"image": 2, "table": 4, "equation": 6}
    candidates: dict[str, list[AssetRef]] = {key: [] for key in budgets}
    for asset in document.assets:
        if asset.asset_id not in required_ids:
            continue
        group = "table" if asset.kind == "table" else "equation" if asset.kind in {"equation", "formula"} else "image"
        candidates[group].append(asset)
    selected_ids: set[str] = set()
    for group, items in candidates.items():
        preferred_facet = "experiments" if group == "table" else "method" if group == "equation" else "problem"
        ranked = sorted(items, key=lambda item: (preferred_facet not in asset_facets.get(item.asset_id, set()), item.asset_id not in targeted, item.asset_id not in important, order[item.asset_id]))
        selected_ids.update(item.asset_id for item in ranked[:budgets[group]])
    decisions: list[AssetDecision] = []
    for asset in document.assets:
        if asset.asset_id not in required_ids:
            continue
        finding_refs = tuple(dict.fromkeys(
            finding.finding_id
            for answer in answers
            for finding in answer.findings
            if asset.block_id in finding.block_refs or asset.asset_id in obligation_assets.get(answer.obligation_id, set())
        ))
        explained_refs = tuple(dict.fromkeys(ref for answer in answers for use in answer.asset_uses if use.asset_id == asset.asset_id for ref in use.finding_refs))
        if explained_refs:
            finding_refs = explained_refs
        # HTML is retained in the evidence asset for diagnostics, but it is
        # never selected as the publication representation.  The reader does
        # not opt into arbitrary raw HTML; simple tables are converted to GFM
        # when the material document is loaded and complex ones use an image.
        preference = ("markdown", "image") if asset.kind == "table" else ("latex", "image") if asset.kind in {"equation", "formula"} else ("image",)
        preferred = next((item for kind in preference for item in asset.representations if item.representation_type == kind), None)
        role = "evidence" if asset.kind == "table" else "mechanism" if asset.kind in {"equation", "formula"} else "intuition"
        if preferred is None:
            # 结构化内容退化且无可用图片（或反之）：显式 omit，让缺文件/坏表示在回执中可见。
            decisions.append(AssetDecision(asset.asset_id, "omit", None, role, asset.block_id or "", finding_refs, "no_publishable_representation", True))
        elif asset.asset_id not in selected_ids:
            decisions.append(AssetDecision(asset.asset_id, "omit", preferred.representation_type, role, asset.block_id or "", finding_refs, "presentation_budget", True))
        elif not finding_refs or not explained_refs:
            decisions.append(AssetDecision(asset.asset_id, "omit", preferred.representation_type, role, asset.block_id or "", (), "not_explained_by_reading_answer", True))
        else:
            decisions.append(AssetDecision(asset.asset_id, "inline", preferred.representation_type, role, asset.block_id or "", finding_refs, "selected_by_survey_or_reading_plan", True))
    missing = sorted(required_ids - {item.asset_id for item in decisions})
    if missing:
        raise TraceableReadingError("asset_decision_unknown_asset:" + missing[0])
    return tuple(decisions)


class TraceableNotePlanner:
    def __init__(self, model: JsonOperationModel, policy: WriterPolicy = DEFAULT_WRITER_POLICY) -> None:
        self.model = model
        self.policy = policy

    def plan(self, paper_map: PaperMap, coverage: ReadingCoverage, answers: Sequence[ReadingAnswer], decisions: Sequence[AssetDecision]) -> NotePlan:
        request = {
            "instruction": "先规划再成文，所有字段保持简洁。只组织已有阅读答案、Finding 和素材决策，不新增论文事实。解释职责按章节形成自然链，不把 claim、mechanism、evidence、interpretation、boundary 固定为五个段落。problem 章节必须安排一段面向读者的背景与研究缺口说明，依次回答研究场景、已有方法为何不足、本文具体要解决什么；该说明必须绑定已有 finding_refs。方法章节必须给出一个 source_example 或 abstract_walkthrough，沿既有机制跟踪一个对象，禁止补造数字、性能或条件。实验章节只为核心实验主张给出少量 experiment_units，每个包含主张、比较、结果、含义、边界；未测试范围写成无法判断，不编造失败结论。只能使用给定 finding_id 和 asset_id。",
            "writer_policy": asdict(self.policy),
            "paper_map": asdict(paper_map),
            "coverage": asdict(coverage),
            "reading_answers": [asdict(item) for item in answers],
            "asset_decisions": [asdict(item) for item in decisions],
            "required_json": {"policy_version": self.policy.version, "sections": [{"section_id": "", "facet": "problem|method|experiments|limitations", "title": "", "reader_question": "", "direct_answer": "", "mechanism_sequence": [], "evidence_focus": [], "interpretation_goal": "", "boundary": "", "finding_refs": [], "asset_refs": [], "worked_example": {"mode": "source_example|abstract_walkthrough", "setup": "", "steps": [], "takeaway": "", "finding_refs": []}, "experiment_units": [{"unit_id": "", "claim": "", "comparison": "", "result": "", "meaning": "", "boundary": "", "finding_refs": []}]}]},
        }
        sections: list[Any] = []
        for operation, facets in (
            ("traceable_note_plan_method", ("problem", "method")),
            ("traceable_note_plan_experiments", ("experiments", "limitations")),
        ):
            partial_request = {**request, "target_facets": facets}
            payload = self.model.call_json(operation, json.dumps(partial_request, ensure_ascii=False))
            if payload.get("_fallback"):
                raise TraceableReadingError(f"note_planner_provider_{payload['_fallback']}")
            if payload.get("policy_version") != self.policy.version or not isinstance(payload.get("sections"), list):
                raise TraceableReadingError("note_plan_invalid_partial")
            returned_facets = {item.get("facet") for item in payload["sections"] if isinstance(item, Mapping)}
            if returned_facets != set(facets):
                raise TraceableReadingError("note_plan_partial_facet_mismatch")
            sections.extend(payload["sections"])
        return _parse_note_plan({"policy_version": self.policy.version, "sections": sections}, answers, decisions, self.policy)


class TraceableWriter:
    def __init__(self, model: JsonOperationModel, policy: WriterPolicy = DEFAULT_WRITER_POLICY) -> None:
        self.model = model
        self.policy = policy

    def write(self, note_plan: NotePlan, paper_map: PaperMap, coverage: ReadingCoverage, answers: Sequence[ReadingAnswer], spans: Sequence[SourceSpan], assets: Sequence[AssetRef], decisions: Sequence[AssetDecision]) -> NoteDraft:
        if not coverage.core_complete:
            raise TraceableReadingError("core_reading_incomplete")
        prompt = json.dumps({
            "instruction": "严格按 NotePlan 写一篇问题驱动、可教学的中文论文笔记，而不是章节摘要或 Findings 清单。问题章节开头必须有一个 explanation_role=context 的背景段，清楚说明研究场景、已有方法缺口、本文问题，并绑定已有 finding_refs。以章节级解释链自然成文，不按五种角色各写一段；一个段落最多承载相邻职责，单个正文段落尽量不超过 260 字，超过时拆成多个 paragraph blocks。方法章节必须真正写出 worked_example：用一个简短 setup 段和连续的 list blocks 展开步骤，最后给出 takeaway，并在对应正文 block 的 plan_item_refs 标记 worked_example。实验章节逐个落实 experiment_units 的主张、比较、结果、含义和边界；字段可自然合并成短段落，但每个 unit_id 必须出现在至少一个正文 block 的 plan_item_refs。数学表达式行内统一使用 $...$，独立公式使用 $$...$$，不要输出裸 LaTeX 命令、下标或未包裹的 min_θ 等字符串；表格只引用 asset_ref，不把 HTML/LaTeX 表格粘进正文。不得补造未回答内容。每个论文事实保留 finding_refs；finding_refs 只能作为 JSON 元数据，禁止写入正文文本。",
            "writer_policy": asdict(self.policy),
            "note_plan": asdict(note_plan),
            "paper_map": asdict(paper_map),
            "coverage": asdict(coverage),
            "reading_answers": [asdict(item) for item in answers],
            "source_spans": [{"span_id": item.span_id, "finding_id": item.finding_id, "pages": sorted({block.page for block in item.blocks})} for item in spans],
            "assets": [{"asset_id": item.asset_id, "kind": item.kind, "caption": item.caption, "block_id": item.block_id, "representation_types": [rep.representation_type for rep in item.representations]} for item in assets if item.asset_id in {decision.asset_id for decision in decisions}],
            "asset_decisions": [asdict(item) for item in decisions],
            "required_json": {"title": "", "sections": [{"section_id": "", "title": "", "reader_question": "", "blocks": [{"block_id": "", "kind": "paragraph|heading|list|figure|table|equation", "text": "", "finding_refs": [], "asset_ref": None, "caption": "", "explanation": "", "explanation_role": "context|intuition|mechanism|evidence|interpretation|limitation", "paragraph_purpose": "", "plan_item_refs": []}]}]},
        }, ensure_ascii=False)
        payload = self.model.call_json("traceable_note_write", prompt)
        if payload.get("_fallback"):
            raise TraceableReadingError(f"writer_provider_{payload['_fallback']}")
        draft = _ensure_background_block(_parse_note(payload, answers, assets, note_plan), note_plan, paper_map)
        draft = _bind_unlinked_blocks_to_plan(draft, note_plan)
        draft = _normalize_chapter_roles(
            _split_long_prose_blocks(
                _materialize_plan_items(
                    _ensure_planned_interpretations(_materialize_asset_blocks(draft, answers, assets, decisions), note_plan),
                    note_plan,
                )
            ),
            note_plan,
        )
        if "missing_explanation_role:interpretation" in _draft_structure_issues(draft):
            draft = self._add_interpretation(draft, paper_map, answers)
        issues = _draft_structure_issues(draft) + _reverse_outline_issues(draft, note_plan, self.policy)
        if not issues:
            return draft
        repair_prompt = json.dumps({
            "instruction": "只修复现有 NoteDraft 的结构、来源绑定与语言问题，不重新阅读论文、不新增事实。不得删除或改写已有 Finding refs、asset refs、plan_item_refs、公式、表格数值和事实含义。问题章节必须有绑定证据的背景段；方法 worked_example 用 setup、连续 list blocks 和 takeaway 展开；正文段落尽量不超过 260 字。按章节级解释链修复，不拆成固定五段；确保方法 worked_example 与每个实验 unit_id 都在正文 plan_item_refs 中落实，并保持方法 mechanism 先于 interpretation、实验 evidence 先于 interpretation。数学表达式行内统一使用 $...$，独立公式使用 $$...$$，不要留下裸 LaTeX 命令或下标；表格保持 asset_ref 表示，不输出原始 HTML/LaTeX 表格。finding_refs 只能保留在 JSON 元数据，不能写入正文文本。返回完整 NoteDraft JSON。",
            "issues": issues,
            "draft": asdict(draft),
            "writer_policy": asdict(self.policy),
            "note_plan": asdict(note_plan),
            "reading_answers": [asdict(item) for item in answers],
            "asset_decisions": [asdict(item) for item in decisions],
            "required_json": {"title": "", "sections": [{"section_id": "", "title": "", "reader_question": "", "blocks": [{"block_id": "", "kind": "paragraph|heading|list|figure|table|equation", "text": "", "finding_refs": [], "asset_ref": None, "caption": "", "explanation": "", "explanation_role": "context|intuition|mechanism|evidence|interpretation|limitation", "paragraph_purpose": "", "plan_item_refs": []}]}]},
        }, ensure_ascii=False)
        repaired = self.model.call_json("traceable_note_repair", repair_prompt)
        if repaired.get("_fallback"):
            raise TraceableReadingError(f"writer_repair_provider_{repaired['_fallback']}")
        repaired_draft = _ensure_background_block(_parse_note(repaired, answers, assets, note_plan), note_plan, paper_map)
        repaired_draft = _normalize_chapter_roles(
            _split_long_prose_blocks(
                _materialize_plan_items(
                    _ensure_planned_interpretations(
                        _materialize_asset_blocks(_bind_unlinked_blocks_to_plan(repaired_draft, note_plan), answers, assets, decisions),
                        note_plan,
                    ),
                    note_plan,
                )
            ),
            note_plan,
        )
        final_issues = _draft_structure_issues(repaired_draft) + _reverse_outline_issues(repaired_draft, note_plan, self.policy)
        if final_issues:
            raise TraceableReadingError("writer_repair_failed:" + ",".join(sorted(set(final_issues))))
        return repaired_draft

    def _add_interpretation(self, draft: NoteDraft, paper_map: PaperMap, answers: Sequence[ReadingAnswer]) -> NoteDraft:
        prompt = json.dumps({
            "instruction": "只基于现有 ReadingAnswers 写一个简洁的教学解释段，回答这些机制或实验结果意味着什么、能够支持什么结论、边界在哪里。不得新增论文事实。必须引用一个或多个已有 finding_id，并选择一个已有 section_id。",
            "paper_map": asdict(paper_map),
            "sections": [{"section_id": item.section_id, "title": item.title, "reader_question": item.reader_question} for item in draft.sections],
            "reading_answers": [asdict(item) for item in answers],
            "required_json": {"target_section_id": "", "text": "", "finding_refs": []},
        }, ensure_ascii=False)
        payload = self.model.call_json("traceable_interpretation_write", prompt)
        section_id = _required(payload, "target_section_id")
        text = _required(payload, "text")
        refs = tuple(_string_list(payload.get("finding_refs")))
        allowed = {finding.finding_id for answer in answers for finding in answer.findings}
        if not refs or any(ref not in allowed for ref in refs):
            raise TraceableReadingError("writer_interpretation_invalid_findings")
        section_index = next((index for index, section in enumerate(draft.sections) if section.section_id == section_id), None)
        if section_index is None:
            raise TraceableReadingError("writer_interpretation_unknown_section")
        sections = list(draft.sections)
        section = sections[section_index]
        block = NoteBlock(f"note:interpretation:{sha256(text.encode()).hexdigest()[:12]}", "paragraph", text, refs, None, "", "", "interpretation", "解释机制或实验结果意味着什么及其证据边界")
        sections[section_index] = NoteSection(section.section_id, section.title, section.blocks + (block,), section.reader_question)
        return NoteDraft(draft.title, tuple(sections))


def _ensure_background_block(draft: NoteDraft, note_plan: NotePlan, paper_map: PaperMap) -> NoteDraft:
    """Ensure every reader-facing note starts with a grounded context bridge.

    The survey already reads the problem evidence, but the old writer contract
    allowed that evidence to be represented only by a figure.  A deterministic
    fallback keeps the note useful when a model omits the context block while
    binding the generated prose to the same audited findings as the problem
    section.
    """

    problem_plan = next((item for item in note_plan.sections if item.facet == "problem"), None)
    if problem_plan is None:
        return draft
    sections = list(draft.sections)
    section_index = next((index for index, item in enumerate(sections) if item.section_id == problem_plan.section_id), None)
    if section_index is None:
        return draft
    section = sections[section_index]
    problem = paper_map.problem.strip()
    approach = paper_map.approach.strip()
    context_markers = ("研究背景", "研究场景", "已有方法", "研究缺口", "本文要解决", "本文旨在", "动机")
    context_index = next((
        index
        for index, block in enumerate(section.blocks)
        if (
            block.kind in {"paragraph", "list"}
            and block.explanation_role == "context"
            and block.text.strip()
            and (
                (problem and problem in block.text)
                or sum(marker in block.text for marker in context_markers) >= 2
            )
        )
    ), None)
    if context_index is not None:
        if context_index == 0:
            return draft
        context_block = section.blocks[context_index]
        reordered = (context_block, *section.blocks[:context_index], *section.blocks[context_index + 1:])
        sections[section_index] = NoteSection(section.section_id, section.title, reordered, section.reader_question)
        return NoteDraft(draft.title, tuple(sections), draft.original_title)

    parts = [f"研究背景与动机：{problem}。"]
    if approach:
        parts.append(f"本文的核心思路是：{approach}。")
    parts.append("下面先说明问题，再展开方法、实验结果和结论边界。")
    context = "".join(parts)
    block = NoteBlock(
        f"note:background:{sha256(problem_plan.section_id.encode()).hexdigest()[:12]}",
        "paragraph",
        context,
        problem_plan.finding_refs,
        None,
        "",
        "",
        "context",
        "说明研究背景、已有方法缺口与本文要解决的问题",
    )
    sections[section_index] = NoteSection(section.section_id, section.title, (block, *section.blocks), section.reader_question)
    return NoteDraft(draft.title, tuple(sections), draft.original_title)


def _materialize_asset_blocks(draft: NoteDraft, answers: Sequence[ReadingAnswer], assets: Sequence[AssetRef], decisions: Sequence[AssetDecision]) -> NoteDraft:
    asset_map = {item.asset_id: item for item in assets}
    explanations = {use.asset_id: use.explanation for answer in answers for use in answer.asset_uses}
    omitted_ids = {item.asset_id for item in decisions if item.decision == "omit"}
    sections = [NoteSection(section.section_id, section.title, tuple(block for block in section.blocks if block.asset_ref not in omitted_ids), section.reader_question) for section in draft.sections]
    if not sections:
        return draft
    for decision in decisions:
        if decision.decision == "omit":
            continue
        asset = asset_map[decision.asset_id]
        existing_location = next(((section_index, block_index) for section_index, section in enumerate(sections) for block_index, item in enumerate(section.blocks) if item.asset_ref == decision.asset_id), None)
        if existing_location is not None:
            section_index, block_index = existing_location
            target = sections[section_index]
            current = target.blocks[block_index]
            canonical_kind = _note_kind_for_asset(asset.kind)
            enriched = NoteBlock(
                current.block_id, canonical_kind, current.text, current.finding_refs or decision.finding_refs, current.asset_ref,
                current.caption or asset.caption, current.explanation or explanations.get(decision.asset_id, "该素材用于辅助理解相关论证。"),
                current.explanation_role or decision.explanation_role, current.paragraph_purpose or f"解释{asset.caption or '关键素材'}在论文论证中的作用", current.plan_item_refs,
            )
            preferred_index = next((index for index, section in enumerate(sections) if any(item.kind not in {"figure", "table", "equation"} and set(item.finding_refs) & set(decision.finding_refs) for item in section.blocks)), section_index)
            updated = list(target.blocks)
            updated.pop(block_index)
            sections[section_index] = NoteSection(target.section_id, target.title, tuple(updated), target.reader_question)
            destination = sections[preferred_index]
            sections[preferred_index] = NoteSection(destination.section_id, destination.title, destination.blocks + (enriched,), destination.reader_question)
            continue
        kind = _note_kind_for_asset(asset.kind)
        block = NoteBlock(
            f"note:asset:{sha256(decision.asset_id.encode()).hexdigest()[:12]}", kind,
            asset.caption or "关键论文素材", decision.finding_refs, decision.asset_id,
            asset.caption, explanations.get(decision.asset_id, "该素材用于辅助理解相关论证。"), decision.explanation_role,
            f"解释{asset.caption or '关键素材'}展示什么以及为何重要",
        )
        target_index = next((index for index, section in enumerate(sections) if any(set(item.finding_refs) & set(decision.finding_refs) for item in section.blocks)), 0)
        target = sections[target_index]
        sections[target_index] = NoteSection(target.section_id, target.title, target.blocks + (block,), target.reader_question)
    normalized = NoteDraft(draft.title, tuple(sections))
    return _infer_interpretation_role(normalized)


def _materialize_plan_items(draft: NoteDraft, note_plan: NotePlan) -> NoteDraft:
    """Render only already validated plan items that Writer failed to realize."""
    plans = {item.section_id: item for item in note_plan.sections}
    sections: list[NoteSection] = []
    for section in draft.sections:
        plan = plans.get(section.section_id)
        blocks = list(section.blocks)
        allowed_items = set()
        if plan is not None and plan.worked_example is not None:
            allowed_items.add("worked_example")
        if plan is not None:
            allowed_items.update(item.unit_id for item in plan.experiment_units)
        blocks = [replace(block, plan_item_refs=tuple(ref for ref in block.plan_item_refs if ref in allowed_items)) for block in blocks]
        present = {ref for block in blocks if block.kind in {"paragraph", "list"} for ref in block.plan_item_refs}
        if plan is not None and plan.facet == "method" and plan.worked_example is not None:
            example = plan.worked_example
            has_structured_example = any(
                block.kind == "list" and "worked_example" in block.plan_item_refs
                for block in blocks
            )
            if not has_structured_example:
                matched = next(
                    (
                        i
                        for i, block in enumerate(blocks)
                        if block.kind in {"paragraph", "list"}
                        and (
                            "worked_example" in block.plan_item_refs
                            or _plan_text_matches(
                                block.text,
                                (example.setup, *example.steps, example.takeaway),
                                minimum=min(3, len(example.steps) + 2),
                            )
                        )
                    ),
                    None,
                )
                example_blocks = _worked_example_blocks(section.section_id, example, blocks[matched] if matched is not None else None)
                if matched is not None:
                    blocks[matched : matched + 1] = example_blocks
                else:
                    insert_at = next((i for i, block in enumerate(blocks) if block.explanation_role == "interpretation"), len(blocks))
                    blocks[insert_at:insert_at] = example_blocks
        if plan is not None and plan.facet == "experiments":
            for unit in plan.experiment_units:
                if unit.unit_id in present:
                    continue
                matched = next((i for i, block in enumerate(blocks) if block.kind in {"paragraph", "list"} and _plan_text_matches(block.text, (unit.claim, unit.comparison, unit.result, unit.meaning, unit.boundary), minimum=5)), None)
                if matched is not None:
                    blocks[matched] = replace(blocks[matched], plan_item_refs=tuple(dict.fromkeys((*blocks[matched].plan_item_refs, unit.unit_id))))
                else:
                    text = (
                        f"实验主张：{unit.claim}。比较方式：{unit.comparison}。"
                        f"观察结果：{unit.result}。这意味着：{unit.meaning}。"
                        f"结论边界：{unit.boundary}。"
                    )
                    unit_block = NoteBlock(
                        f"note:experiment-unit:{sha256(unit.unit_id.encode()).hexdigest()[:12]}",
                        "paragraph", text, unit.finding_refs, None, "", "", "evidence",
                        "把实验的主张、比较、结果、含义与边界组织为完整论证单元", (unit.unit_id,),
                    )
                    insert_at = next((i for i, block in enumerate(blocks) if block.explanation_role == "interpretation"), len(blocks))
                    blocks.insert(insert_at, unit_block)
        sections.append(NoteSection(section.section_id, section.title, tuple(blocks), section.reader_question))
    return NoteDraft(draft.title, tuple(sections), draft.original_title)


def _plan_text_matches(text: str, candidates: Sequence[str], *, minimum: int = 1) -> bool:
    compact = re.sub(r"\s+", "", text).casefold()
    matches = 0
    for candidate in candidates:
        value = re.sub(r"\s+", "", candidate).casefold()
        if len(value) >= 12 and (value in compact or compact in value):
            matches += 1
    return matches >= minimum


def _worked_example_blocks(section_id: str, example: WorkedExamplePlan, source: NoteBlock | None) -> list[NoteBlock]:
    """Materialise a validated walkthrough as readable setup/steps/takeaway blocks."""

    digest = sha256(f"{section_id}|worked_example".encode()).hexdigest()[:12]
    refs = example.finding_refs or (source.finding_refs if source is not None else ())
    role = source.explanation_role if source is not None and source.explanation_role else "mechanism"
    blocks = [
        NoteBlock(
            f"note:worked-example:{digest}:heading",
            "heading",
            "贯穿示例",
            (),
            None,
            "",
            "",
            role,
            "引出一个具体对象，帮助读者跟随方法流程",
            (),
        ),
        NoteBlock(
            f"note:worked-example:{digest}:setup",
            "paragraph",
            f"先设定一个贯穿示例：{example.setup}",
            refs,
            None,
            "",
            "",
            role,
            "说明贯穿示例的输入和观察对象",
            ("worked_example",),
        ),
    ]
    for index, step in enumerate(example.steps, start=1):
        blocks.append(
            NoteBlock(
                f"note:worked-example:{digest}:step-{index}",
                "list",
                step,
                refs,
                None,
                "",
                "",
                role,
                f"展开贯穿示例的第 {index} 步",
                ("worked_example",),
            )
        )
    blocks.append(
        NoteBlock(
            f"note:worked-example:{digest}:takeaway",
            "paragraph",
            f"这个过程帮助理解：{example.takeaway}",
            refs,
            None,
            "",
            "",
            role,
            "总结贯穿示例与方法目标之间的关系",
            ("worked_example",),
        )
    )
    return blocks


def _split_long_prose_blocks(draft: NoteDraft, *, max_chars: int = 280) -> NoteDraft:
    """Split dense generated prose without changing its evidence bindings."""

    sections: list[NoteSection] = []
    for section in draft.sections:
        expanded: list[NoteBlock] = []
        for block in section.blocks:
            if block.kind != "paragraph" or len(block.text) <= max_chars:
                expanded.append(block)
                continue
            parts = _readable_sentences(block.text, max_chars=max_chars)
            if len(parts) <= 1:
                expanded.append(block)
                continue
            for index, part in enumerate(parts, start=1):
                expanded.append(
                    NoteBlock(
                        block.block_id if index == 1 else f"{block.block_id}:part-{index}",
                        block.kind,
                        part,
                        block.finding_refs,
                        block.asset_ref,
                        block.caption,
                        block.explanation,
                        block.explanation_role,
                        block.paragraph_purpose,
                        block.plan_item_refs,
                    )
                )
        sections.append(NoteSection(section.section_id, section.title, tuple(expanded), section.reader_question))
    return NoteDraft(draft.title, tuple(sections), draft.original_title)


def _readable_sentences(text: str, *, max_chars: int) -> list[str]:
    clauses = [item.strip() for item in re.split(r"(?<=[。！？!?；;])\s*", text) if item.strip()]
    if len(clauses) <= 1:
        return [text.strip()]
    parts: list[str] = []
    current = ""
    for clause in clauses:
        candidate = f"{current}{clause}" if current else clause
        if current and len(candidate) > max_chars:
            parts.append(current)
            current = clause
        else:
            current = candidate
    if current:
        parts.append(current)
    return parts


def _normalize_chapter_roles(draft: NoteDraft, note_plan: NotePlan) -> NoteDraft:
    plans = {item.section_id: item for item in note_plan.sections}
    sections: list[NoteSection] = []
    for section in draft.sections:
        plan = plans.get(section.section_id)
        blocks = list(section.blocks)
        if plan is not None and plan.facet == "limitations" and not any(block.explanation_role == "limitation" for block in blocks):
            index = next((i for i, block in enumerate(blocks) if block.kind in {"paragraph", "list"}), None)
            if index is not None:
                blocks[index] = replace(blocks[index], explanation_role="limitation")
        sections.append(NoteSection(section.section_id, section.title, tuple(blocks), section.reader_question))
    return NoteDraft(draft.title, tuple(sections), draft.original_title)


def _ensure_planned_interpretations(draft: NoteDraft, note_plan: NotePlan) -> NoteDraft:
    """Fill only per-section explanation duties already grounded in NotePlan."""
    plans = {item.section_id: item for item in note_plan.sections}
    sections: list[NoteSection] = []
    for section in draft.sections:
        plan = plans.get(section.section_id)
        if plan is None or plan.facet not in {"method", "experiments"} or any(block.explanation_role == "interpretation" for block in section.blocks):
            sections.append(section)
            continue
        text = f"理解这一部分时，重点在于：{plan.interpretation_goal}。结论的适用边界是：{plan.boundary}。"
        block = NoteBlock(
            f"note:planned-interpretation:{sha256(section.section_id.encode()).hexdigest()[:12]}",
            "paragraph", text, plan.finding_refs, None, "", "", "interpretation",
            f"解释{plan.facet}证据意味着什么及其适用边界",
        )
        sections.append(NoteSection(section.section_id, section.title, section.blocks + (block,), section.reader_question))
    return NoteDraft(draft.title, tuple(sections))


def _bind_unlinked_blocks_to_plan(draft: NoteDraft, note_plan: NotePlan) -> NoteDraft:
    """Inherit only the current planned section's audited Finding allow-list."""
    allowed = {item.section_id: item.finding_refs for item in note_plan.sections}
    sections: list[NoteSection] = []
    for section in draft.sections:
        refs = allowed.get(section.section_id, ())
        blocks = tuple(
            NoteBlock(
                block.block_id, block.kind, block.text,
                refs if block.kind in {"paragraph", "list"} and not block.finding_refs else block.finding_refs,
                block.asset_ref, block.caption, block.explanation, block.explanation_role, block.paragraph_purpose,
                block.plan_item_refs,
            )
            for block in section.blocks
        )
        sections.append(NoteSection(section.section_id, section.title, blocks, section.reader_question))
    return NoteDraft(draft.title, tuple(sections))


def _infer_interpretation_role(draft: NoteDraft) -> NoteDraft:
    if any(block.explanation_role == "interpretation" for section in draft.sections for block in section.blocks):
        return draft
    markers = ("这说明", "这表明", "意味着", "因此", "由此可见", "说明了", "suggests", "indicates", "this means")
    for section_index, section in enumerate(draft.sections):
        for block_index, block in enumerate(section.blocks):
            if block.kind not in {"paragraph", "list"} or not any(marker in block.text.casefold() for marker in markers):
                continue
            changed = NoteBlock(block.block_id, block.kind, block.text, block.finding_refs, block.asset_ref, block.caption, block.explanation, "interpretation", block.paragraph_purpose, block.plan_item_refs)
            blocks = list(section.blocks)
            blocks[block_index] = changed
            sections = list(draft.sections)
            sections[section_index] = NoteSection(section.section_id, section.title, tuple(blocks), section.reader_question)
            return NoteDraft(draft.title, tuple(sections))
    return draft


def _draft_structure_issues(draft: NoteDraft) -> list[str]:
    issues: list[str] = []
    if not _has_cjk(draft.title):
        issues.append("non_chinese_note_title")
    roles = {block.explanation_role for section in draft.sections for block in section.blocks if block.explanation_role}
    for role in ("mechanism", "evidence", "interpretation"):
        if role not in roles:
            issues.append(f"missing_explanation_role:{role}")
    for section in draft.sections:
        if not _has_cjk(section.title):
            issues.append(f"non_chinese_section_title:{section.section_id}")
        if not section.reader_question.strip():
            issues.append(f"missing_reader_question:{section.section_id}")
        elif not _has_cjk(section.reader_question):
            issues.append(f"non_chinese_reader_question:{section.section_id}")
        for block in section.blocks:
            if block.kind in {"paragraph", "list"} and block.text.strip() and not block.finding_refs:
                issues.append(f"unlinked_note_block:{block.block_id}")
            if block.kind in {"figure", "table", "equation"} and not block.explanation.strip():
                issues.append(f"incomplete_asset_block:{block.block_id}")
            elif block.kind in {"figure", "table", "equation"} and not _has_cjk(block.explanation):
                issues.append(f"non_chinese_asset_explanation:{block.block_id}")
    prose = [block for section in draft.sections for block in section.blocks if block.kind in {"paragraph", "list"}]
    if prose and sum(_has_cjk(block.text) for block in prose) / len(prose) < 0.6:
        issues.append("non_chinese_note_body")
    return issues


def _reverse_outline_issues(draft: NoteDraft, note_plan: NotePlan, policy: WriterPolicy) -> list[str]:
    issues: list[str] = []
    plan_by_id = {item.section_id: item for item in note_plan.sections}
    if {item.section_id for item in draft.sections} != set(plan_by_id):
        issues.append("reverse_outline_section_mismatch")
    generic = {item.casefold() for item in policy.generic_purposes}
    for section in draft.sections:
        plan = plan_by_id.get(section.section_id)
        if plan is None:
            continue
        roles = {block.explanation_role for block in section.blocks if block.explanation_role}
        for block in section.blocks:
            if block.kind not in {"paragraph", "list"}:
                continue
            purpose = re.sub(r"[\s，。；：、,.!?！？:;]+", "", block.paragraph_purpose).casefold()
            if not purpose:
                issues.append(f"reverse_outline_missing_purpose:{block.block_id}")
                continue
            if purpose in generic or any(purpose == f"{word}论文" for word in generic):
                issues.append(f"reverse_outline_generic_purpose:{block.block_id}")
        required = {"method": {"mechanism", "interpretation"}, "experiments": {"evidence", "interpretation"}, "limitations": {"limitation"}}.get(plan.facet, set())
        for role in required - roles:
            issues.append(f"reverse_outline_missing_{role}:{section.section_id}")
        ordered_roles = [block.explanation_role for block in section.blocks if block.kind in {"paragraph", "list"}]
        if plan.facet == "method" and "mechanism" in ordered_roles and "interpretation" in ordered_roles:
            if ordered_roles.index("mechanism") > ordered_roles.index("interpretation"):
                issues.append(f"reverse_outline_broken_method_chain:{section.section_id}")
        if plan.facet == "experiments" and "evidence" in ordered_roles and "interpretation" in ordered_roles:
            if ordered_roles.index("evidence") > ordered_roles.index("interpretation"):
                issues.append(f"reverse_outline_broken_experiment_chain:{section.section_id}")
        plan_refs = {ref for block in section.blocks if block.kind in {"paragraph", "list"} for ref in block.plan_item_refs}
        if plan.facet == "method" and plan.worked_example is not None and "worked_example" not in plan_refs:
            issues.append(f"reverse_outline_missing_worked_example:{section.section_id}")
        if plan.facet == "experiments":
            for unit in plan.experiment_units:
                if unit.unit_id not in plan_refs:
                    issues.append(f"reverse_outline_missing_experiment_unit:{unit.unit_id}")
    return issues


def _has_cjk(value: str) -> bool:
    return bool(re.search(r"[\u3400-\u9fff]", value))


def basic_publication_check(draft: NoteDraft, findings: Sequence[Finding], spans: Sequence[SourceSpan], assets: Sequence[AssetRef], coverage: ReadingCoverage, decisions: Sequence[AssetDecision] = (), note_plan: NotePlan | None = None, policy: WriterPolicy = DEFAULT_WRITER_POLICY) -> tuple[str, ...]:
    issues: list[str] = list(_draft_structure_issues(draft))
    if not draft.original_title.strip():
        issues.append("missing_original_title")
    if note_plan is not None:
        issues.extend(_reverse_outline_issues(draft, note_plan, policy))
    finding_ids = {item.finding_id for item in findings}
    span_findings = {item.finding_id for item in spans}
    asset_ids = {item.asset_id for item in assets}
    asset_map = {item.asset_id: item for item in assets}
    if not coverage.core_complete:
        issues.append("core_coverage_incomplete")
    used_assets = {block.asset_ref for section in draft.sections for block in section.blocks if block.asset_ref}
    blocks_by_asset = {
        asset_id: [block for section in draft.sections for block in section.blocks if block.asset_ref == asset_id]
        for asset_id in used_assets
    }
    for decision in decisions:
        if decision.required and decision.decision in {"inline", "reference"} and decision.asset_id not in used_assets:
            issues.append(f"required_asset_missing:{decision.asset_id}")
        if decision.decision in {"inline", "reference"} and decision.asset_id in used_assets:
            blocks = blocks_by_asset[decision.asset_id]
            if len(blocks) != 1:
                issues.append(f"asset_block_multiplicity:{decision.asset_id}")
            else:
                asset = asset_map.get(decision.asset_id)
                expected = _note_kind_for_asset(asset.kind) if asset else None
                if blocks[0].kind != expected:
                    issues.append(f"asset_block_kind_mismatch:{decision.asset_id}")
                if asset is not None and not _decision_representation_is_renderable(asset, decision):
                    issues.append(f"asset_not_renderable:{decision.asset_id}")
        if decision.decision == "omit" and decision.asset_id in used_assets:
            issues.append(f"omitted_asset_rendered:{decision.asset_id}")
    for section in draft.sections:
        for block in section.blocks:
            if block.kind in {"paragraph", "list"} and block.text.strip() and not block.finding_refs:
                issues.append(f"unlinked_note_block:{block.block_id}")
            for finding_id in block.finding_refs:
                if finding_id not in finding_ids:
                    issues.append(f"unknown_finding_ref:{block.block_id}")
                elif finding_id not in span_findings:
                    issues.append(f"finding_without_span:{block.block_id}")
            if block.asset_ref and block.asset_ref not in asset_ids:
                issues.append(f"unknown_asset_ref:{block.block_id}")
            if block.kind in {"figure", "table", "equation"} and not block.asset_ref:
                issues.append(f"incomplete_asset_block:{block.block_id}")
    return tuple(sorted(set(issues)))


def _note_kind_for_asset(asset_kind: str) -> str:
    return "table" if asset_kind == "table" else "equation" if asset_kind in {"equation", "formula"} else "figure"


def _decision_representation_is_renderable(asset: AssetRef, decision: AssetDecision) -> bool:
    if not decision.representation_type:
        return False
    representations = [item for item in asset.representations if item.representation_type == decision.representation_type]
    if not representations:
        return False
    if decision.representation_type == "image":
        return any(item.path.strip() for item in representations) or bool(asset.path.strip())
    return any(item.content.strip() for item in representations)


class KnowledgePublisher:
    def __init__(self, vault_root: Path) -> None:
        self.vault_root = Path(vault_root)

    def publish(
        self,
        document: MaterialDocument,
        survey: SurveyResult,
        coverage: ReadingCoverage,
        note_plan: NotePlan,
        policy: WriterPolicy,
        draft: NoteDraft,
        answers: Sequence[ReadingAnswer],
        spans: Sequence[SourceSpan],
        assets: Sequence[AssetRef],
        decisions: Sequence[AssetDecision],
        receipt: RunReceipt,
        *,
        domain: str,
        publication_status: str = "published",
    ) -> RunReceipt:
        version = datetime.now(timezone.utc).isoformat()
        safe_version = re.sub(r"[^A-Za-z0-9._-]+", "-", version)
        root = self.vault_root / "papers" / document.source_id
        root.mkdir(parents=True, exist_ok=True)
        selected_assets = {block.asset_ref for section in draft.sections for block in section.blocks if block.asset_ref}
        asset_paths: dict[str, str] = {}
        for asset in assets:
            if asset.asset_id not in selected_assets:
                continue
            if not asset.path:
                continue
            source = document.package.root / Path(asset.path)
            if not source.is_file():
                raise TraceableReadingError(f"missing_asset:{asset.asset_id}")
            target_name = sha256(asset.asset_id.encode()).hexdigest()[:16] + source.suffix.lower()
            target = root / "assets" / target_name
            target.parent.mkdir(parents=True, exist_ok=True)
            copy2(source, target)
            asset_paths[asset.asset_id] = f"assets/{target_name}"
        body = _render_markdown(draft, asset_paths, {item.asset_id: item for item in assets})
        render_issues = _publication_render_issues(body)
        if render_issues:
            raise TraceableReadingError("publication_render_failed:" + ",".join(render_issues))
        markdown_path = root / f"{safe_version}.md"
        evidence_path = root / f"{safe_version}.evidence.json"
        note_path = root / f"{safe_version}.note.json"
        receipt_path = self.vault_root / "receipts" / document.source_id / f"{safe_version}.json"
        evidence_payload = {
            "schema_version": 2,
            "source_id": document.source_id,
            "original_title": draft.original_title,
            "material_version": document.material_version,
            "semantic_evidence_status": "not_evaluated",
            "paper_map": asdict(survey.paper_map),
            "reading_plan": asdict(survey.reading_plan),
            "reading_coverage": asdict(coverage),
            "answers": [asdict(item) for item in answers],
            "source_spans": [asdict(item) for item in spans],
            "asset_decisions": [asdict(item) for item in decisions],
            "writer_policy_version": policy.version,
            "note_plan": asdict(note_plan),
        }
        evidence_path.write_text(json.dumps(evidence_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        note_path.write_text(json.dumps(asdict(draft), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        header = {
            "knowledge_id": f"kp:arxiv:{document.source_id}",
            "knowledge_version": version,
            "publication_status": publication_status,
            "evidence_level": "source_linked_unverified",
            "rag_eligible": False,
            "source_urls": [document.source_url],
            "domain": domain,
            "title": draft.title,
            "original_title": draft.original_title,
            "schema_version": 1,
            "evidence_index": evidence_path.name,
        }
        front = "---\n" + "\n".join(f"{key}: {json.dumps(value, ensure_ascii=False)}" for key, value in header.items()) + "\n---\n"
        markdown_path.write_text(front + body, encoding="utf-8")
        final = RunReceipt(**{
            **receipt.to_payload(),
            "publication_status": publication_status,
            "published_path": str(markdown_path),
            "evidence_index_path": str(evidence_path),
            "receipt_path": str(receipt_path),
        })
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_path.write_text(json.dumps(final.to_payload(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return final


class TraceableReadingPipeline:
    def __init__(self, model: JsonOperationModel, vault_root: Path, *, deadline_seconds: float = 600.0) -> None:
        self.survey = SurveyReader(model)
        self.finding_reader = FindingReader(model, deadline_seconds=deadline_seconds)
        self.policy = DEFAULT_WRITER_POLICY
        self.note_planner = TraceableNotePlanner(model, self.policy)
        self.writer = TraceableWriter(model, self.policy)
        self.publisher = KnowledgePublisher(vault_root)

    def run(
        self,
        document: MaterialDocument,
        *,
        domain: str,
        publication_status: str = "published",
    ) -> RunReceipt:
        survey = self.survey.read(document)
        packets = build_reading_packets(document, survey.reading_plan)
        answers = self.finding_reader.read_all(packets)
        spans = resolve_source_spans(document, answers)
        packet_ids = {block.block_id for packet in packets for block in packet.blocks}
        assessment = BasicEvidenceGate().evaluate(document, packet_ids, answers, spans)
        if assessment.reference_status != "valid":
            raise TraceableReadingError("invalid_evidence_links:" + ",".join(assessment.issues))
        coverage = build_coverage(survey.reading_plan, answers, assessment)
        decisions = plan_asset_decisions(document, survey, answers)
        note_plan = self.note_planner.plan(survey.paper_map, coverage, answers, decisions)
        draft = self.writer.write(note_plan, survey.paper_map, coverage, answers, spans, document.assets, decisions)
        try:
            draft = replace(draft, original_title=document.original_title())
        except ValueError as exc:
            raise TraceableReadingError("material_missing_original_title") from exc
        findings = tuple(finding for answer in answers for finding in answer.findings)
        issues = basic_publication_check(draft, findings, spans, document.assets, coverage, decisions, note_plan, self.policy)
        if issues:
            raise TraceableReadingError("publication_check_failed:" + ",".join(issues))
        receipt = RunReceipt(document.source_id, "valid", "completed", "completed", "valid", "not_evaluated", "pending", "source_linked_unverified", False)
        return self.publisher.publish(
            document,
            survey,
            coverage,
            note_plan,
            self.policy,
            draft,
            answers,
            spans,
            document.assets,
            decisions,
            receipt,
            domain=domain,
            publication_status=publication_status,
        )


def _parse_survey(payload: Mapping[str, Any], document: MaterialDocument) -> SurveyResult:
    raw_map = payload.get("paper_map")
    raw_obligations = payload.get("obligations")
    if not isinstance(raw_map, Mapping) or not isinstance(raw_obligations, list) or not raw_obligations:
        raise TraceableReadingError("survey_invalid_output")
    section_ids = {item.section_id for item in document.sections}
    asset_ids = {item.asset_id for item in document.assets}
    paper_map = PaperMap(
        *(_required(raw_map, key) for key in ("paper_type", "problem", "approach", "experiments", "limitations")),
        tuple(_string_list(raw_map.get("important_asset_ids"))),
        tuple(_string_list(raw_map.get("open_questions"))),
    )
    obligations: list[ReadingObligation] = []
    for raw in raw_obligations:
        if not isinstance(raw, Mapping):
            raise TraceableReadingError("survey_invalid_obligation")
        facet = _required(raw, "facet")
        if facet not in {"problem", "method", "experiments", "limitations"}:
            raise TraceableReadingError("survey_invalid_facet")
        targets = tuple(_string_list(raw.get("target_section_ids")))
        if not targets or any(item not in section_ids for item in targets):
            raise TraceableReadingError("survey_unknown_section")
        target_assets = tuple(_string_list(raw.get("target_asset_ids")))
        if any(item not in asset_ids for item in target_assets):
            raise TraceableReadingError("survey_unknown_asset")
        raw_requirements = raw.get("evidence_requirements")
        if not isinstance(raw_requirements, list) or not raw_requirements:
            raise TraceableReadingError("survey_missing_evidence_requirements")
        requirements = tuple(EvidenceRequirement(_required(item, "requirement_id"), _required(item, "description"), bool(item.get("required", True))) for item in raw_requirements if isinstance(item, Mapping))
        obligations.append(ReadingObligation(_required(raw, "obligation_id"), _required(raw, "question"), _required(raw, "purpose"), targets, target_assets, requirements, facet, bool(raw.get("core", True))))
    core_facets = {item.facet for item in obligations if item.core}
    if core_facets != {"problem", "method", "experiments", "limitations"}:
        raise TraceableReadingError("survey_missing_core_obligations")
    return SurveyResult(paper_map, ReadingPlan(tuple(obligations)))


def _parse_answers(payload: Mapping[str, Any], packet: ReadingPacket, allowed_ids: set[str]) -> tuple[ReadingAnswer, ...]:
    raw_answers = payload.get("answers")
    if not isinstance(raw_answers, list):
        raise TraceableReadingError("finding_invalid_output")
    obligation_map = {item.obligation_id: item for item in packet.obligations}
    packet_asset_ids = {item.asset_id for item in packet.assets}
    packet_output_finding_ids = {
        str(item.get("finding_id")).strip()
        for raw in raw_answers if isinstance(raw, Mapping)
        for item in raw.get("findings", []) if isinstance(item, Mapping) and str(item.get("finding_id") or "").strip()
    }
    answers: list[ReadingAnswer] = []
    for raw in raw_answers:
        if not isinstance(raw, Mapping):
            raise TraceableReadingError("finding_invalid_answer")
        obligation_id = _required(raw, "obligation_id")
        if obligation_id not in obligation_map:
            raise TraceableReadingError("finding_unknown_obligation")
        status = _answer_status(raw.get("status"))
        findings: list[Finding] = []
        for item in raw.get("findings", []):
            if not isinstance(item, Mapping):
                raise TraceableReadingError("finding_invalid_finding")
            refs = tuple(_string_list(item.get("block_refs")))
            if not refs:
                raise TraceableReadingError("finding_empty_block_refs")
            unknown = next((ref for ref in refs if ref not in allowed_ids), None)
            if unknown is not None:
                raise TraceableReadingError(f"finding_unknown_block:{unknown[:120]}")
            support = _required(item, "support_kind")
            if support not in {"direct", "synthesis", "reader_inference"}:
                raise TraceableReadingError("finding_invalid_support_kind")
            findings.append(Finding(_required(item, "finding_id"), _required(item, "statement"), _required(item, "answer_quote"), _required(item, "requirement_id"), support, refs))
        results = tuple(RequirementResult(_required(item, "requirement_id"), _answer_status(item.get("status")), tuple(_string_list(item.get("finding_ids")))) for item in raw.get("requirement_results", []) if isinstance(item, Mapping))
        asset_uses: list[AssetUse] = []
        asset_use_issues: list[str] = []
        for item in raw.get("asset_uses", []):
            if not isinstance(item, Mapping):
                raise TraceableReadingError("finding_invalid_asset_use")
            asset_id = _required(item, "asset_id")
            if asset_id not in packet_asset_ids:
                raise TraceableReadingError("finding_unknown_asset")
            refs = tuple(_string_list(item.get("finding_refs")))
            if not refs:
                asset_use_issues.append(f"asset_use_empty_refs:{asset_id}")
                continue
            unknown_ref = next((ref for ref in refs if ref not in packet_output_finding_ids), None)
            if unknown_ref is not None:
                raise TraceableReadingError(f"finding_asset_use_unknown_finding:{unknown_ref[:120]}")
            asset_uses.append(AssetUse(asset_id, _required(item, "explanation"), refs))
        answers.append(ReadingAnswer(_required(raw, "answer_id"), obligation_id, status, _required(raw, "answer"), tuple(findings), results, tuple(asset_uses), tuple(asset_use_issues)))
    if {item.obligation_id for item in answers} != set(obligation_map):
        raise TraceableReadingError("finding_missing_answer")
    return tuple(answers)


def _parse_note_plan(payload: Mapping[str, Any], answers: Sequence[ReadingAnswer], decisions: Sequence[AssetDecision], policy: WriterPolicy) -> NotePlan:
    if payload.get("policy_version") != policy.version:
        raise TraceableReadingError("note_plan_policy_version_mismatch")
    raw_sections = payload.get("sections")
    if not isinstance(raw_sections, list) or not raw_sections:
        raise TraceableReadingError("note_plan_invalid_sections")
    finding_ids = {finding.finding_id for answer in answers for finding in answer.findings}
    asset_ids = {item.asset_id for item in decisions}
    sections: list[SectionNotePlan] = []
    for raw in raw_sections:
        if not isinstance(raw, Mapping):
            raise TraceableReadingError("note_plan_invalid_section")
        facet = _required(raw, "facet")
        if facet not in {"problem", "method", "experiments", "limitations"}:
            raise TraceableReadingError("note_plan_invalid_facet")
        refs = tuple(_string_list(raw.get("finding_refs")))
        asset_refs = tuple(_string_list(raw.get("asset_refs")))
        if not refs or any(ref not in finding_ids for ref in refs):
            raise TraceableReadingError("note_plan_unknown_finding")
        if any(ref not in asset_ids for ref in asset_refs):
            raise TraceableReadingError("note_plan_unknown_asset")
        mechanisms = tuple(_string_list(raw.get("mechanism_sequence")))
        evidence = tuple(_string_list(raw.get("evidence_focus")))
        worked_example = None
        raw_example = raw.get("worked_example")
        if facet == "method":
            if not isinstance(raw_example, Mapping):
                raise TraceableReadingError("note_plan_missing_worked_example")
            mode = _required(raw_example, "mode")
            if mode not in {"source_example", "abstract_walkthrough"}:
                raise TraceableReadingError("note_plan_invalid_worked_example_mode")
            example_refs = tuple(_string_list(raw_example.get("finding_refs")))
            steps = tuple(_string_list(raw_example.get("steps")))
            if not steps or not example_refs or any(ref not in refs for ref in example_refs):
                raise TraceableReadingError("note_plan_invalid_worked_example")
            worked_example = WorkedExamplePlan(
                mode, _required(raw_example, "setup"), steps,
                _required(raw_example, "takeaway"), example_refs,
            )
            if not mechanisms:
                mechanisms = worked_example.steps
        raw_units = raw.get("experiment_units")
        experiment_units: list[ExperimentUnitPlan] = []
        if facet == "experiments":
            if not isinstance(raw_units, list) or not raw_units:
                raise TraceableReadingError("note_plan_missing_experiment_units")
            for unit in raw_units:
                if not isinstance(unit, Mapping):
                    raise TraceableReadingError("note_plan_invalid_experiment_unit")
                unit_refs = tuple(_string_list(unit.get("finding_refs")))
                if not unit_refs or any(ref not in refs for ref in unit_refs):
                    raise TraceableReadingError("note_plan_invalid_experiment_unit_refs")
                experiment_units.append(ExperimentUnitPlan(
                    _required(unit, "unit_id"), _required(unit, "claim"),
                    _required(unit, "comparison"), _required(unit, "result"),
                    _required(unit, "meaning"), _required(unit, "boundary"), unit_refs,
                ))
            if len({item.unit_id for item in experiment_units}) != len(experiment_units):
                raise TraceableReadingError("note_plan_duplicate_experiment_unit")
            if not evidence:
                evidence = tuple(f"{item.comparison}；观察{item.result}" for item in experiment_units)
        sections.append(SectionNotePlan(
            _required(raw, "section_id"), facet, _required(raw, "title"), _required(raw, "reader_question"),
            _required(raw, "direct_answer"), mechanisms, evidence, _required(raw, "interpretation_goal"),
            _required(raw, "boundary"), refs, asset_refs, worked_example, tuple(experiment_units),
        ))
    if {item.facet for item in sections} != {"problem", "method", "experiments", "limitations"}:
        raise TraceableReadingError("note_plan_missing_core_facets")
    if len({item.section_id for item in sections}) != len(sections):
        raise TraceableReadingError("note_plan_duplicate_section")
    return NotePlan(policy.version, tuple(sections))


def _parse_note(payload: Mapping[str, Any], answers: Sequence[ReadingAnswer], assets: Sequence[AssetRef], note_plan: NotePlan | None = None) -> NoteDraft:
    title = _required(payload, "title")
    raw_sections = payload.get("sections")
    if not isinstance(raw_sections, list) or not raw_sections:
        raise TraceableReadingError("writer_invalid_sections")
    finding_ids = {finding.finding_id for answer in answers for finding in answer.findings}
    asset_ids = {asset.asset_id for asset in assets}
    sections: list[NoteSection] = []
    planned_ids = {item.section_id for item in note_plan.sections} if note_plan else set()
    for raw in raw_sections:
        if not isinstance(raw, Mapping):
            raise TraceableReadingError("writer_invalid_section")
        blocks: list[NoteBlock] = []
        for item in raw.get("blocks", []):
            if not isinstance(item, Mapping):
                raise TraceableReadingError("writer_invalid_block")
            kind = _required(item, "kind")
            if kind not in {"paragraph", "heading", "list", "figure", "table", "equation"}:
                raise TraceableReadingError("writer_invalid_block_kind")
            refs = tuple(_string_list(item.get("finding_refs")))
            if any(ref not in finding_ids for ref in refs):
                raise TraceableReadingError("writer_unknown_finding")
            asset_ref = item.get("asset_ref")
            if asset_ref is not None and (not isinstance(asset_ref, str) or asset_ref not in asset_ids):
                raise TraceableReadingError("writer_unknown_asset")
            role = item.get("explanation_role")
            if role not in {"context", "intuition", "mechanism", "evidence", "interpretation", "limitation"}:
                raise TraceableReadingError("writer_invalid_explanation_role")
            caption = str(item.get("caption") or "").strip()
            explanation = str(item.get("explanation") or "").strip()
            text_value = str(item.get("text") or "").strip()
            if not text_value and kind in {"figure", "table", "equation"}:
                text_value = caption or explanation
            if not text_value:
                raise TraceableReadingError("missing_field:text")
            blocks.append(NoteBlock(
                _required(item, "block_id"), kind, text_value, refs, asset_ref,
                caption, explanation, role, _required(item, "paragraph_purpose") if kind in {"paragraph", "list"} else str(item.get("paragraph_purpose") or "").strip(),
                tuple(_string_list(item.get("plan_item_refs"))),
            ))
        section_id = _required(raw, "section_id")
        if planned_ids and section_id not in planned_ids:
            raise TraceableReadingError("writer_unknown_planned_section")
        sections.append(NoteSection(section_id, _required(raw, "title"), tuple(blocks), _required(raw, "reader_question")))
    if planned_ids and {item.section_id for item in sections} != planned_ids:
        raise TraceableReadingError("writer_missing_planned_section")
    return NoteDraft(title, tuple(sections))


def _render_markdown(draft: NoteDraft, asset_paths: Mapping[str, str], assets: Mapping[str, AssetRef]) -> str:
    lines = [f"# {_visible_note_text(draft.title)}", "", f"> 原论文标题：{_visible_note_text(draft.original_title)}", ""]
    for section in draft.sections:
        lines.extend((f"## {_visible_note_text(section.title)}", "", f"> 阅读问题：{_visible_note_text(section.reader_question)}", ""))
        pending_list: list[NoteBlock] = []

        def flush_list() -> None:
            if not pending_list:
                return
            for index, item in enumerate(pending_list, start=1):
                lines.append(f"{index}. {_visible_note_text(item.text)}")
            lines.append("")
            pending_list.clear()

        for block in section.blocks:
            if block.kind == "list":
                pending_list.append(block)
                continue
            flush_list()
            if block.kind == "figure" and block.asset_ref in asset_paths:
                lines.extend((f"![{_visible_note_text(block.caption or block.text)}]({asset_paths[block.asset_ref]})", "", _visible_note_text(block.explanation), ""))
            elif block.kind in {"table", "equation"} and block.asset_ref in assets:
                asset = assets[block.asset_ref]
                # Never emit arbitrary HTML from a paper parser.  Tables are
                # published as GFM; a complex table falls back to its source
                # image below.
                wanted = ("latex",) if block.kind == "equation" else ("markdown",)
                representation = next((item for kind in wanted for item in asset.representations if item.representation_type == kind), None)
                converted_table = None
                if representation is None and block.kind == "table":
                    html_representation = next((item for item in asset.representations if item.representation_type == "html"), None)
                    if html_representation is not None:
                        converted_table = table_html_to_markdown(html_representation.content)
                if block.caption:
                    lines.extend((f"**{_visible_note_text(block.caption)}**", ""))
                if representation is not None:
                    if block.kind == "equation":
                        formula = _visible_note_text(representation.content.strip())
                        if formula.startswith("$$") and formula.endswith("$$"):
                            formula = formula[2:-2].strip()
                        lines.extend(("$$", formula, "$$", ""))
                    else:
                        lines.extend((_visible_note_text(representation.content.strip()), ""))
                elif converted_table:
                    lines.extend((converted_table, ""))
                elif asset_paths.get(block.asset_ref):
                    # 结构化表示缺失/退化时确定性回退论文原图，不静默丢块。
                    lines.extend((
                        f"![{_visible_note_text(block.caption or '论文原图')}]({asset_paths[block.asset_ref]})",
                        "",
                        "> 结构化解析质量不足，此处保留论文原图；精确内容以原图为准。",
                        "",
                    ))
                if block.explanation:
                    lines.extend((_visible_note_text(block.explanation), ""))
            elif block.kind == "heading":
                lines.extend((f"### {_visible_note_text(block.text)}", ""))
            else:
                # Finding IDs are provenance metadata, not reader-facing
                # footnotes.  They remain in the evidence sidecar JSON.
                lines.extend((_visible_note_text(block.text), ""))
        flush_list()
    return "\n".join(lines).rstrip() + "\n"


_INTERNAL_FINDING_MARKER = re.compile(r"\s*\[\^finding:[^\]\r\n]+\]")
_INTERNAL_FINDING_LABEL = re.compile(
    r"[（(]\s*对应\s+Findings?\s+[A-Za-z0-9:_-]+"
    r"(?:\s*[、,]\s*(?:Findings?\s+)?[A-Za-z0-9:_-]+)*\s*[）)]",
    flags=re.IGNORECASE,
)
_REPEATED_READER_PUNCTUATION = re.compile(r"([。！？；])\1+")


def _visible_note_text(value: str) -> str:
    """Remove internal provenance syntax and obvious generated punctuation noise."""

    text = _INTERNAL_FINDING_MARKER.sub("", str(value or ""))
    text = _INTERNAL_FINDING_LABEL.sub("", text)
    return _REPEATED_READER_PUNCTUATION.sub(r"\1", text).strip()


def _publication_render_issues(markdown: str) -> tuple[str, ...]:
    """Reject reader-visible implementation syntax at the publication boundary."""

    issues: list[str] = []
    if _INTERNAL_FINDING_MARKER.search(markdown):
        issues.append("internal_finding_marker")
    if re.search(r"<table\b", markdown, flags=re.IGNORECASE):
        issues.append("raw_html_table")
    return tuple(issues)


def _required(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise TraceableReadingError(f"missing_field:{key}")
    return value.strip()


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        return []
    return [item.strip() for item in value]


def _answer_status(value: Any) -> Any:
    if value not in {"answered", "partially_answered", "not_stated", "unclear"}:
        raise TraceableReadingError("invalid_answer_status")
    return value
