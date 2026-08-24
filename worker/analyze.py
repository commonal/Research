"""Create explicitly scoped, abstract-only paper-reading Markdown with DeepSeek."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import json
import os

from worker.discover import PaperCandidate


DEEPSEEK_CHAT_COMPLETIONS_URL = "https://api.deepseek.com/chat/completions"
ANALYSIS_LEVEL = "abstract_only"


@dataclass(frozen=True)
class AbstractAnalysis:
    one_sentence_summary: str
    problem: str
    approach_from_abstract: str
    reported_results: str
    limitations_and_unknowns: list[str]
    next_reading_questions: list[str]


class DeepSeekError(RuntimeError):
    """A safe, actionable error without exposing a credential."""


def analyze_abstract(candidate: PaperCandidate, api_key: str, model: str) -> AbstractAnalysis:
    """Ask DeepSeek for JSON that is constrained to title and abstract evidence."""

    response = _post_json(
        DEEPSEEK_CHAT_COMPLETIONS_URL,
        payload=_request_payload(candidate, model),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    content = _response_content(response)
    try:
        structured = json.loads(content)
    except json.JSONDecodeError as error:
        raise DeepSeekError("DeepSeek returned invalid JSON for the abstract analysis.") from error
    return _validate_analysis(structured)


def api_key_from_environment() -> str:
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        raise DeepSeekError(
            "DEEPSEEK_API_KEY is not set. Set it only in your local environment; never commit it."
        )
    return api_key


def render_markdown(candidate: PaperCandidate, analysis: AbstractAnalysis) -> str:
    """Render a transparent Markdown asset; no claim is stronger than its evidence."""

    front_matter = {
        "title": candidate.title,
        "entry_type": "paper_reading",
        "analysis_level": ANALYSIS_LEVEL,
        "source": candidate.source,
        "source_id": candidate.source_id,
        "source_url": candidate.source_url,
        "published_at": candidate.published_at,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    authors = ", ".join(candidate.authors)
    limitations = _bullets(analysis.limitations_and_unknowns)
    questions = _bullets(analysis.next_reading_questions)
    return f"""---
{_yaml_lines(front_matter)}
---

# {candidate.title}

> **证据等级：摘要级解读。** 本文仅依据 arXiv 元数据与摘要生成，未读取论文全文；图表、公式、实验细节、作者未明示的局限和复现结论均未分析。

- 作者：{authors}
- 发表时间：{candidate.published_at}
- 分类：{', '.join(candidate.categories)}
- 原文：[arXiv]({candidate.source_url})

## 一分钟结论

{analysis.one_sentence_summary}

## 它试图解决什么问题？

{analysis.problem}

## 摘要中描述的方法

{analysis.approach_from_abstract}

## 摘要中报告的结果

{analysis.reported_results}

## 图表、公式与实验细节

未分析。下一阶段需要临时获取并解析全文，才能讨论具体公式、图号、表格、实验设置和消融结果。

## 尚未验证的局限与阅读边界

{limitations}

## 建议带着这些问题读原文

{questions}
"""


def write_markdown(markdown: str, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(markdown, encoding="utf-8")


def _request_payload(candidate: PaperCandidate, model: str) -> dict[str, Any]:
    system_prompt = """You create Chinese research notes from paper abstracts.
Return JSON only. All explanatory values must be in Simplified Chinese; preserve paper titles,
proper names, benchmarks, technical terms, and numerical values in their original form when useful.
You may use only the supplied title and abstract.
Never claim to have read the full paper, figures, equations, tables, code, or appendices.
If an item is not supported by the abstract, state in Chinese that it needs full-text verification.
Return this exact JSON shape:
{
  "one_sentence_summary": "string",
  "problem": "string",
  "approach_from_abstract": "string",
  "reported_results": "string",
  "limitations_and_unknowns": ["string"],
  "next_reading_questions": ["string"]
}"""
    user_prompt = f"""Title: {candidate.title}
Authors: {', '.join(candidate.authors)}
Published: {candidate.published_at}
Categories: {', '.join(candidate.categories)}
Abstract:
{candidate.abstract}
"""
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "response_format": {"type": "json_object"},
        "max_tokens": 1200,
        "stream": False,
    }


def _post_json(url: str, payload: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")[:500]
        raise DeepSeekError(f"DeepSeek request failed with HTTP {error.code}: {body}") from error
    except URLError as error:
        raise DeepSeekError(f"Cannot reach DeepSeek: {error.reason}") from error


def _response_content(response: dict[str, Any]) -> str:
    try:
        content = response["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as error:
        raise DeepSeekError("DeepSeek response did not contain choices[0].message.content.") from error
    if not isinstance(content, str) or not content.strip():
        raise DeepSeekError("DeepSeek returned an empty JSON response; retry the request.")
    return content


def _validate_analysis(payload: Any) -> AbstractAnalysis:
    if not isinstance(payload, dict):
        raise DeepSeekError("DeepSeek JSON response must be an object.")
    string_fields = ("one_sentence_summary", "problem", "approach_from_abstract", "reported_results")
    values: dict[str, str] = {}
    for field in string_fields:
        value = payload.get(field)
        if not isinstance(value, str) or not value.strip():
            raise DeepSeekError(f"DeepSeek JSON response has invalid '{field}'.")
        values[field] = value.strip()
    return AbstractAnalysis(
        **values,
        limitations_and_unknowns=_string_list(payload.get("limitations_and_unknowns"), "limitations_and_unknowns"),
        next_reading_questions=_string_list(payload.get("next_reading_questions"), "next_reading_questions"),
    )


def _string_list(value: Any, field_name: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise DeepSeekError(f"DeepSeek JSON response has invalid '{field_name}'.")
    if not all(isinstance(item, str) and item.strip() for item in value):
        raise DeepSeekError(f"DeepSeek JSON response has invalid items in '{field_name}'.")
    return [item.strip() for item in value]


def _yaml_lines(values: dict[str, str]) -> str:
    return "\n".join(f'{key}: {json.dumps(value, ensure_ascii=False)}' for key, value in values.items())


def _bullets(values: list[str]) -> str:
    return "\n".join(f"- {value}" for value in values)
