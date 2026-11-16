"""Provider clients. Anthropic uses the official SDK; the others are plain HTTPS so no
vendor SDK is needed. Same shapes as Build-With-AI's provider layer."""

from __future__ import annotations

from dataclasses import dataclass

import httpx

TIMEOUT = httpx.Timeout(120.0, connect=15.0)


@dataclass
class LLMResult:
    text: str
    model: str
    provider: str
    input_tokens: int = 0
    output_tokens: int = 0


class LLMError(Exception):
    pass


def _check(resp: httpx.Response, who: str) -> None:
    if resp.status_code in (401, 403):
        raise LLMError(f"{who} rejected the credentials ({resp.status_code})")
    if resp.status_code >= 400:
        raise LLMError(f"{who} error {resp.status_code}: {resp.text[:300]}")


class Provider:
    type = "base"
    who = "Provider"

    def __init__(self, base_url: str = "", api_key: str | None = None, options: dict | None = None):
        self.base_url, self.api_key, self.options = (base_url or "").rstrip("/"), api_key, options or {}

    def complete(self, *, system: str, prompt: str, model: str, max_tokens: int = 2000) -> LLMResult:
        raise NotImplementedError

    def list_models(self) -> list[str]:
        return []


class AnthropicProvider(Provider):
    type, who = "anthropic", "Anthropic"

    def _client(self):
        try:
            import anthropic
        except ImportError as e:
            raise LLMError("Install the Anthropic SDK: pip install 'operatewithai[llm]'") from e
        if not self.api_key:
            raise LLMError("ANTHROPIC_API_KEY is not set")
        kw = {"api_key": self.api_key, "max_retries": 2}
        if self.base_url:
            kw["base_url"] = self.base_url
        return anthropic, anthropic.Anthropic(**kw)

    def complete(self, *, system, prompt, model, max_tokens=2000) -> LLMResult:
        anthropic, client = self._client()
        try:
            msg = client.messages.create(model=model, max_tokens=max_tokens, system=system, messages=[{"role": "user", "content": prompt}])
        except anthropic.AuthenticationError as e:
            raise LLMError("Anthropic rejected the API key (401)") from e
        except anthropic.NotFoundError as e:
            raise LLMError(f"Model {model} not found or not available to this key") from e
        except anthropic.APIStatusError as e:
            raise LLMError(f"Anthropic API error {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise LLMError("Cannot reach the Anthropic API") from e
        if msg.stop_reason == "refusal":
            raise LLMError("The model declined the request")
        return LLMResult("".join(b.text for b in msg.content if b.type == "text"), msg.model, self.type,
                         msg.usage.input_tokens, msg.usage.output_tokens)

    def list_models(self) -> list[str]:
        anthropic, client = self._client()
        try:
            return [m.id for m in client.models.list()]
        except anthropic.APIError as e:
            raise LLMError(f"Cannot list models: {e}") from e


class OpenAICompatibleProvider(Provider):
    type, who, default_base, token_field = "openai_compatible", "OpenAI-compatible endpoint", "http://localhost:11434/v1", "max_tokens"

    def _headers(self) -> dict:
        return {"Content-Type": "application/json"} | ({"Authorization": f"Bearer {self.api_key}"} if self.api_key else {})

    def _post(self, url: str, body: dict) -> dict:
        try:
            r = httpx.post(url, headers=self._headers(), json=body, timeout=TIMEOUT)
        except httpx.HTTPError as e:
            raise LLMError(f"Cannot reach {self.who}: {e}") from e
        _check(r, self.who)
        return r.json()

    def complete(self, *, system, prompt, model, max_tokens=2000) -> LLMResult:
        d = self._post((self.base_url or self.default_base) + "/chat/completions",
                       {"model": model, self.token_field: max_tokens,
                        "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}]})
        try:
            text = d["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError) as e:
            raise LLMError(f"Unexpected response from {self.who}") from e
        u = d.get("usage") or {}
        return LLMResult(text, d.get("model", model), self.type, u.get("prompt_tokens", 0), u.get("completion_tokens", 0))

    def list_models(self) -> list[str]:
        try:
            r = httpx.get((self.base_url or self.default_base) + "/models", headers=self._headers(), timeout=30)
        except httpx.HTTPError as e:
            raise LLMError(f"Cannot reach {self.who}: {e}") from e
        _check(r, self.who)
        return sorted(m["id"] for m in r.json().get("data", []))


class OpenAIProvider(OpenAICompatibleProvider):
    type, who, default_base, token_field = "openai", "OpenAI", "https://api.openai.com/v1", "max_completion_tokens"


class AzureOpenAIProvider(OpenAICompatibleProvider):
    type, who = "azure_openai", "Azure OpenAI"

    def _headers(self) -> dict:
        return {"Content-Type": "application/json", "api-key": self.api_key or ""}

    def complete(self, *, system, prompt, model, max_tokens=2000) -> LLMResult:
        if not self.base_url or "<" in self.base_url:
            raise LLMError("Azure OpenAI needs the resource endpoint as base URL")
        d = self._post(f"{self.base_url}/openai/deployments/{model}/chat/completions?api-version={self.options.get('api_version', '2024-10-21')}",
                       {"max_completion_tokens": max_tokens, "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}]})
        u = d.get("usage") or {}
        return LLMResult(d["choices"][0]["message"]["content"] or "", model, self.type, u.get("prompt_tokens", 0), u.get("completion_tokens", 0))

    def list_models(self) -> list[str]:
        return []


class GeminiProvider(Provider):
    type, who = "google_gemini", "Gemini"

    def _base(self) -> str:
        return self.base_url or "https://generativelanguage.googleapis.com/v1beta"

    def complete(self, *, system, prompt, model, max_tokens=2000) -> LLMResult:
        body = {"systemInstruction": {"parts": [{"text": system}]}, "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {"maxOutputTokens": max_tokens}}
        try:
            r = httpx.post(f"{self._base()}/models/{model}:generateContent", json=body, timeout=TIMEOUT,
                           headers={"x-goog-api-key": self.api_key or "", "Content-Type": "application/json"})
        except httpx.HTTPError as e:
            raise LLMError(f"Cannot reach Gemini: {e}") from e
        _check(r, self.who)
        d = r.json()
        cands = d.get("candidates") or []
        if not cands:
            raise LLMError("Gemini returned no candidates")
        u = d.get("usageMetadata") or {}
        return LLMResult("".join(p.get("text", "") for p in cands[0].get("content", {}).get("parts", [])), model, self.type,
                         u.get("promptTokenCount", 0), u.get("candidatesTokenCount", 0))

    def list_models(self) -> list[str]:
        try:
            r = httpx.get(f"{self._base()}/models", headers={"x-goog-api-key": self.api_key or ""}, timeout=30)
        except httpx.HTTPError as e:
            raise LLMError(f"Cannot reach Gemini: {e}") from e
        _check(r, self.who)
        return sorted(m["name"].removeprefix("models/") for m in r.json().get("models", []) if "generateContent" in m.get("supportedGenerationMethods", []))


class BedrockProvider(Provider):
    """Amazon Bedrock Converse API with a Bedrock API key (bearer token)."""
    type, who = "aws_bedrock", "Bedrock"

    def complete(self, *, system, prompt, model, max_tokens=2000) -> LLMResult:
        base = self.base_url or f"https://bedrock-runtime.{self.options.get('region', 'us-east-1')}.amazonaws.com"
        try:
            r = httpx.post(f"{base}/model/{model}/converse", timeout=TIMEOUT,
                           headers={"Authorization": f"Bearer {self.api_key or ''}", "Content-Type": "application/json"},
                           json={"system": [{"text": system}], "messages": [{"role": "user", "content": [{"text": prompt}]}],
                                 "inferenceConfig": {"maxTokens": max_tokens}})
        except httpx.HTTPError as e:
            raise LLMError(f"Cannot reach Bedrock: {e}") from e
        _check(r, self.who)
        d = r.json()
        u = d.get("usage") or {}
        return LLMResult("".join(p.get("text", "") for p in d.get("output", {}).get("message", {}).get("content", [])), model, self.type,
                         u.get("inputTokens", 0), u.get("outputTokens", 0))


CLASSES = {c.type: c for c in (AnthropicProvider, OpenAIProvider, AzureOpenAIProvider, GeminiProvider, BedrockProvider, OpenAICompatibleProvider)}
