"""S7 Evidence Gate + S8 Blind Reader Gate（P2 质量门禁）。

契约见 SPEC §9：Evidence Gate 先于 Blind Reader；Blind Reader 输入**仅 RenderedNote**
（最终读者能看到的 artifact = 正文 + 已渲染可访问资产），evidence 只能引用笔记内部，
**不注入 PaperModel / 原文 / 中间产物**，不做参考答案对照。

两者都经 ``call_json`` 调 LLM 评判，结果落回 ``EvidenceGateResult`` / ``BlindReaderResult``。
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from .contracts import (
    BlindReaderDimension,
    BlindReaderResult,
    EvidenceGateResult,
    PaperModel,
    RenderedNote,
)

BLIND_DIMENSIONS = (
    "background",
    "prior_gap",
    "mechanism",
    "formalism",
    "experiment",
    "visual",
    "boundary",
)
BLIND_SCORES = ("clear", "partial", "missing")


class EvidenceGate:
    """S7：这个笔记讲的是真的吗 —— 校验 claims 是否被证据支持（无编造符号/数值/机制）。"""

    def __init__(self, model: Any) -> None:
        self.model = model

    def evaluate(self, note: RenderedNote, paper_model: PaperModel) -> EvidenceGateResult:
        prompt = json.dumps({
            "operation": "evidence_gate",
            "task": "逐条检查教学化笔记里的结论/数值/符号/机制是否为原论文证据支持（不允许编造或过度引申）。",
            "rules": [
                "只允许指出 '确实无证据支持/与论文不符' 的硬问题；不确定/概括性表述不算 issue。",
                "引用笔记内部行文；禁止因为'没引用原文'而判 issue（这是盲读的职责，不是证据门禁）。",
                "不逐字对账；只要关键结论有源即可（'claim可概括但锚点须逐字'的自由度保留）。",
            ],
            "note_markdown": note.markdown,
            "paper_facts": _paper_facts(paper_model),
            "return": {"passed": True, "issues": []},
        }, ensure_ascii=False)
        for _attempt in range(2):
            response = self.model.call_json("evidence_gate", self.model.text_model, prompt)
            issues = tuple(
                str(issue) for issue in (response.get("issues") or ()) if str(issue).strip()
            )
            passed = bool(response.get("passed"))
            if issues:
                return EvidenceGateResult(passed=False, issues=issues)
            if passed:
                return EvidenceGateResult(passed=True, issues=())
        return EvidenceGateResult(
            passed=False,
            issues=(
                "evidence_gate_invalid_response: judge 连续两次返回 passed=false 但未提供可定位 issue",
            ),
        )


class BlindReader:
    """S8：真的但读者看懂了吗 —— 独立 context 只读最终笔记，评 7 维可懂度。"""

    def __init__(self, model: Any, *, prompt_version: str = "v1") -> None:
        self.model = model
        self.prompt_version = prompt_version

    def read(self, note: RenderedNote) -> BlindReaderResult:
        # 扁平打分 schema：避免模型把嵌套预填模板回显。7 维直接作键，解析最稳。
        prompt = json.dumps({
            "operation": "blind_reader",
            "task": ("你是一个**没读过原论文**的技术读者，只凭下面这篇中文教学化笔记，判断它是否让一个"
                     "新手读者真正读懂。**只允许引用笔记内部内容**，不得假设你知道原论文信息。"),
            "rules": [
                "按 7 个维度打分：background（背景）/ prior_gap（与既有工作的差距）/ mechanism（机制）/ formalism（公式与形式化）/ experiment（实验证据）/ visual（图表是否被正文引入解释并参与论证）/ boundary（局限与边界）。",
                "每维取值 clear（讲透）/ partial（部分）/ missing（没讲）。",
                "visual 只评'是否被正文正确引入、解释并参与论证'，不声称判断图像本身像素。",
                "overall：无 missing 且 ≤2 partial → pass；否则 needs_targeted_revision；≥3 missing → fail。",
            ],
            "note": note.markdown,
            "please_output_json_exactly": {
                "overall": "pass",
                "background": "clear",
                "prior_gap": "clear",
                "mechanism": "clear",
                "formalism": "clear",
                "experiment": "clear",
                "visual": "clear",
                "boundary": "clear",
                "critical_missing_information": [],
            },
        }, ensure_ascii=False)
        response = self.model.call_json("blind_reader", self.model.text_model, prompt)
        return _blind_result(response, self.model.text_model, self.prompt_version)


def _paper_facts(paper_model: PaperModel) -> Mapping[str, Any]:
    from dataclasses import asdict

    facts = asdict(paper_model)
    return facts


def _blind_result(response: Mapping[str, Any], model: str, prompt_version: str) -> BlindReaderResult:
    dims: list[BlindReaderDimension] = []
    # 优先扁平 schema：{background: "clear", ...}；兼容嵌套 {dimensions: [{dimension, score}]}。
    if "dimensions" in response:
        for item in (response.get("dimensions") or ()):
            if not isinstance(item, Mapping):
                continue
            dim = str(item.get("dimension", "")).strip()
            score = str(item.get("score", "")).strip()
            if dim in BLIND_DIMENSIONS and score in BLIND_SCORES:
                dims.append(BlindReaderDimension(
                    dimension=dim,
                    score=score,
                    evidence=str(item.get("evidence", "")).strip(),
                    note=str(item.get("note", "")).strip(),
                ))
    else:
        for dim in BLIND_DIMENSIONS:
            score = str(response.get(dim, "")).strip()
            if score not in BLIND_SCORES:
                continue
            dims.append(BlindReaderDimension(dimension=dim, score=score))
    overall = str(response.get("overall", "fail")).strip()
    if overall not in ("pass", "needs_targeted_revision", "fail"):
        overall = "fail"
    if dims:
        missing = sum(1 for d in dims if d.score == "missing")
        partial = sum(1 for d in dims if d.score == "partial")
        # 以分数重推 overall（守 SPEC 口径）：pass = 无 missing 且 ≤2 partial；≥3 missing = fail；其余 needs_targeted_revision。
        if missing >= 3:
            overall = "fail"
        elif missing == 0 and partial <= 2:
            overall = "pass"
        else:
            overall = "needs_targeted_revision"
    return BlindReaderResult(
        overall=overall,
        dimensions=tuple(dims),
        critical_missing_information=tuple(
            {str(k): str(v) for k, v in item.items()} for item in (response.get("critical_missing_information") or ())
            if isinstance(item, Mapping)
        ),
        model=model,
        prompt_version=prompt_version,
    )
