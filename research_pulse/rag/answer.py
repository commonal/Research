"""Grounded answer generators; only EvidenceHit text reaches the LLM prompt."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Callable, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import json
import os

from research_pulse.rag.contracts import EvidenceHit


DEEPSEEK_CHAT_COMPLETIONS_URL = "https://api.deepseek.com/chat/completions"
THINKING_MODE = {"type": "enabled"}


@dataclass(frozen=True)
class DeepSeekGroundedAnswerGenerator:
    api_key: str
    model: str = "deepseek-v4-flash"
    post_json: Callable[[str, dict[str, Any], dict[str, str]], dict[str, Any]] | None = None
    max_tokens: int = 1_200

    def generate(self, *, query: str, evidence: Sequence[EvidenceHit]) -> str:
        answer_evidence = [hit for hit in evidence if hit.claim_type != "reading_question"]
        records = [
            {
                "knowledge_id": hit.knowledge_id,
                "knowledge_version": hit.knowledge_version,
                "anchor_id": hit.anchor_id,
                "source_url": hit.source_url,
                "text": hit.text,
                "claim_id": hit.claim_id,
                "claim_type": hit.claim_type,
                "source_anchors": [asdict(anchor) for anchor in hit.source_anchors],
            }
            for hit in answer_evidence
        ]
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "用简体中文回答。只能依据提供的 evidence，不能补充外部事实或推测。"
                        "每个关键结论末尾必须标注 [knowledge_id@version | anchor_id]。"
                        "claim_type=agent_inference 的内容必须明确写为“系统推断”，"
                        "claim_type=reading_question 不得作为答案事实。"
                        "如果证据不支持问题，明确说证据不足。"
                    ),
                },
                {"role": "user", "content": json.dumps({"question": query, "evidence": records}, ensure_ascii=False)},
            ],
            "temperature": 0.1,
            "thinking": THINKING_MODE,
            "max_tokens": self.max_tokens,
            "stream": False,
        }
        response = (self.post_json or _post_json)(
            DEEPSEEK_CHAT_COMPLETIONS_URL,
            payload,
            {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
        )
        try:
            answer = response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as error:
            raise RuntimeError("DeepSeek answer response was incomplete.") from error
        if not isinstance(answer, str) or not answer.strip():
            raise RuntimeError("DeepSeek returned an empty answer.")
        answer = answer.strip()
        if any(hit.claim_type == "agent_inference" for hit in answer_evidence) and "系统推断" not in answer:
            answer = "以下回答包含系统推断：\n" + answer
        return answer


def _post_json(url: str, payload: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
    request = Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    try:
        with urlopen(request, timeout=90) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        raise RuntimeError(f"DeepSeek answer request failed with HTTP {error.code}.") from error
    except URLError as error:
        raise RuntimeError(f"Cannot reach DeepSeek for answer: {error.reason}") from error
