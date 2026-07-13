# Ruta: src/rag/llm_adapter.py
"""
LLM provider abstraction (Adapter pattern) for XookHub.

`LLMAdapter` is the port; `GeminiAdapter` is the concrete implementation,
backed by Google's unified Gen AI SDK (`google-genai`). Everything else in
the codebase (the RAG service, the worker's embedding step) depends only on
the abstract interface, so switching providers is a change here plus
`get_llm_adapter()` — never a rewrite of business logic (DIP). This is
exactly the payoff of having built against the port: the OpenAI concrete
class was swapped out without touching a single caller.

Two Gemini-specific translations live in this file, hidden from callers:
  1. Roles. Gemini's chat history uses 'user'/'model' (not 'assistant') and
     does NOT take a 'system' turn inline — the system prompt is passed as
     `system_instruction` in the config. `_split_messages` handles both.
  2. Embedding dimensionality. `gemini-embedding-001` defaults to a
     different width, but the pgvector column is fixed at vector(1536), so
     we pin `output_dimensionality` to the configured dimension on every
     embed call to keep the vectors schema-compatible.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence

from abc import ABC, abstractmethod

from google import genai
from google.genai import types
from google.genai.errors import APIError

from src.config import get_settings

settings = get_settings()


class ChatMessage:
    """Minimal provider-agnostic chat message.

    Deliberately not tied to any provider's wire shape so the interface
    below stays neutral; the concrete adapter is responsible for translating
    into the provider's format.
    """

    __slots__ = ("role", "content")

    def __init__(self, role: str, content: str) -> None:
        self.role = role
        self.content = content

    def as_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


class LLMAdapter(ABC):
    """Port: the capabilities the RAG/generation pipelines need from any provider."""

    @abstractmethod
    async def embed(self, text: str) -> list[float]:
        """Return the embedding vector for a single piece of text."""
        raise NotImplementedError

    @abstractmethod
    async def embed_batch(self, texts: Sequence[str]) -> list[list[float]]:
        """Return embedding vectors for a batch (one call, N vectors)."""
        raise NotImplementedError

    @abstractmethod
    async def stream_chat(
        self, messages: Sequence[ChatMessage], *, temperature: float = 0.2
    ) -> AsyncIterator[str]:
        """Yield the assistant's reply token-by-token (delta strings)."""
        raise NotImplementedError
        yield ""  # pragma: no cover - marks this as an async generator

    @abstractmethod
    async def complete_json(
        self, messages: Sequence[ChatMessage], *, temperature: float = 0.2
    ) -> str:
        """Return a single, complete (non-streamed) assistant reply.

        Used by the `generation` module for structured JSON output — the
        provider is asked to return JSON and the raw string is returned for
        the caller to parse."""
        raise NotImplementedError


class GeminiError(RuntimeError):
    """Wraps google.genai errors so callers don't couple to the SDK's types."""


def _split_messages(
    messages: Sequence[ChatMessage],
) -> tuple[str | None, list[types.Content]]:
    """Translate neutral ChatMessages into Gemini's (system_instruction, contents).

    - 'system' messages are concatenated and returned separately, to be
      passed as `system_instruction` (Gemini has no inline system turn).
    - 'assistant' is mapped to Gemini's 'model' role; everything else maps
      to 'user'.
    """
    system_parts: list[str] = []
    contents: list[types.Content] = []
    for message in messages:
        if message.role == "system":
            system_parts.append(message.content)
            continue
        gemini_role = "model" if message.role == "assistant" else "user"
        contents.append(
            types.Content(
                role=gemini_role,
                parts=[types.Part.from_text(text=message.content)],
            )
        )
    system_instruction = "\n\n".join(system_parts) if system_parts else None
    return system_instruction, contents


class GeminiAdapter(LLMAdapter):
    """Concrete adapter backed by Google's unified Gen AI SDK (google-genai).

    Models and the API key are read from Settings, so they're configurable
    per environment rather than hardcoded.
    """

    def __init__(self, client: genai.Client | None = None) -> None:
        self._client = client or genai.Client(api_key=settings.GEMINI_API_KEY)
        self._embedding_model = settings.GEMINI_EMBEDDING_MODEL
        self._chat_model = settings.GEMINI_CHAT_MODEL
        # Pin embedding width to the pgvector column dimension (1536).
        self._embedding_dim = settings.GEMINI_EMBEDDING_DIM

    async def embed(self, text: str) -> list[float]:
        vectors = await self.embed_batch([text])
        return vectors[0]

    async def embed_batch(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        try:
            response = await self._client.aio.models.embed_content(
                model=self._embedding_model,
                contents=list(texts),
                config=types.EmbedContentConfig(
                    output_dimensionality=self._embedding_dim,
                ),
            )
        except APIError as exc:
            raise GeminiError(f"Gemini embed_content failed: {exc}") from exc
        # `response.embeddings` preserves input order, one entry per content.
        return [list(item.values) for item in response.embeddings]

    async def stream_chat(
        self, messages: Sequence[ChatMessage], *, temperature: float = 0.2
    ) -> AsyncIterator[str]:
        system_instruction, contents = _split_messages(messages)
        config = types.GenerateContentConfig(
            temperature=temperature,
            system_instruction=system_instruction,
        )
        try:
            stream = await self._client.aio.models.generate_content_stream(
                model=self._chat_model,
                contents=contents,
                config=config,
            )
            async for chunk in stream:
                # `chunk.text` is None for non-text parts (safety, etc.).
                if chunk.text:
                    yield chunk.text
        except APIError as exc:
            raise GeminiError(f"Gemini stream failed: {exc}") from exc

    async def complete_json(
        self, messages: Sequence[ChatMessage], *, temperature: float = 0.2
    ) -> str:
        system_instruction, contents = _split_messages(messages)
        config = types.GenerateContentConfig(
            temperature=temperature,
            system_instruction=system_instruction,
            # Constrain output to valid JSON. The prompts also spell out the
            # exact shape, and the service layer re-validates defensively.
            response_mime_type="application/json",
        )
        try:
            response = await self._client.aio.models.generate_content(
                model=self._chat_model,
                contents=contents,
                config=config,
            )
        except APIError as exc:
            raise GeminiError(f"Gemini complete_json failed: {exc}") from exc
        return response.text or "{}"


# Composition-root singleton. Callers import `get_llm_adapter()` rather than
# instantiating a concrete adapter directly, keeping the provider choice in
# one place and easy to override in tests.
_adapter: LLMAdapter | None = None


def get_llm_adapter() -> LLMAdapter:
    global _adapter
    if _adapter is None:
        _adapter = GeminiAdapter()
    return _adapter