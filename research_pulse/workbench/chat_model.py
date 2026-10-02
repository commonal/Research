"""Configured single-call model adapter for fixed-context workbench chat."""

from langchain_openai import ChatOpenAI


class OpenAICompatibleChatModel:
    def __init__(self, *, model: str, api_key: str, base_url: str) -> None:
        self.model = ChatOpenAI(model=model, api_key=api_key, base_url=base_url, temperature=0, max_completion_tokens=1600, max_retries=1, timeout=60)

    def complete(self, prompt: str) -> str:
        response = self.model.invoke(prompt)
        content = response.content
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("model returned an empty response")
        return content.strip()
