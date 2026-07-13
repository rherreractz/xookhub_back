# Ruta: src/rag/prompts.py
"""
Prompt templates for the RAG pipeline.

Centralized here (not inlined in `service.py`) so prompt-engineering
iteration doesn't touch retrieval/orchestration logic, and so the exact
wording is reviewable in one place.
"""

from __future__ import annotations

from dataclasses import dataclass

RAG_SYSTEM_PROMPT = (
    "Eres XookHub, un asistente de estudio. Responde únicamente con base en "
    "el CONTEXTO proporcionado, que proviene de los documentos de la sala del "
    "usuario. Reglas estrictas:\n"
    "1. Si el contexto no contiene la respuesta, dilo claramente; no inventes "
    "información.\n"
    "2. Cita las fuentes que uses refiriéndote a ellas como [Fuente N], "
    "usando el número que aparece en cada fragmento del contexto.\n"
    "3. Responde en el mismo idioma en que el usuario formuló la pregunta.\n"
    "4. Sé conciso y didáctico."
)


@dataclass(frozen=True, slots=True)
class RetrievedChunk:
    """A single retrieved chunk plus the metadata needed to cite it.

    `source_index` is the 1-based number shown to the model as [Fuente N]
    and echoed back in `Message.citations` so the frontend can map a
    citation to its originating chunk/document.
    """

    source_index: int
    chunk_id: str
    document_id: str
    content: str
    page_number: int | None
    distance: float


def build_context_block(chunks: list[RetrievedChunk]) -> str:
    """Render retrieved chunks into the CONTEXTO section of the user turn."""
    if not chunks:
        return "CONTEXTO:\n(No se encontraron fragmentos relevantes.)"

    parts = ["CONTEXTO:"]
    for chunk in chunks:
        page = f" (pág. {chunk.page_number})" if chunk.page_number is not None else ""
        parts.append(f"[Fuente {chunk.source_index}]{page}\n{chunk.content}")
    return "\n\n".join(parts)


def build_user_turn(question: str, chunks: list[RetrievedChunk]) -> str:
    """Combine the retrieved context and the user's question into one turn."""
    return f"{build_context_block(chunks)}\n\nPREGUNTA:\n{question}"


# --------------------------------------------------------------------------- #
# Stateless "quick answer" prompt (POST /rooms/{id}/chat)
# --------------------------------------------------------------------------- #
# Deliberately simpler/plainer than RAG_SYSTEM_PROMPT above: no citation
# markers, no elaborate persona — but NOT a blind grounding instruction
# either. A pure "answer only from context" rule made the bot refuse to
# even respond to "Hola" ("El contexto provisto no contiene información
# para responder a tu saludo.") — technically following the instruction,
# but exactly the kind of literal-but-unhelpful behavior worth avoiding.
# The fix isn't to drop grounding — that stays for anything that's
# actually a claim about the material — it's to have the model recognize
# social/small-talk turns don't need it in the first place.
QUICK_ANSWER_INSTRUCTION = (
    "Eres el asistente de estudio de esta sala. Si el mensaje del usuario "
    "es un saludo o una interacción social (por ejemplo 'hola', '¿cómo "
    "estás?', 'gracias'), respóndele de forma breve, cálida y natural — no "
    "necesitas el contexto para esto, y nunca digas que 'el contexto no "
    "contiene información' para responder a un saludo. Pero si el mensaje "
    "es una pregunta académica o técnica sobre el material de estudio, "
    "responde única y exclusivamente basándote en el contexto provisto; si "
    "el contexto no contiene la respuesta a ESE tipo de pregunta, indícalo "
    "amablemente en vez de inventar información."
)


def build_quick_answer_context(chunks: list[RetrievedChunk]) -> str:
    """Plain concatenation of retrieved chunk texts — no [Fuente N] markers,
    since this endpoint returns a single flat answer with no citation UI."""
    if not chunks:
        return "(No se encontraron fragmentos relevantes en los documentos de esta sala.)"
    return "\n\n".join(chunk.content for chunk in chunks)


def build_quick_answer_user_turn(query: str, chunks: list[RetrievedChunk]) -> str:
    """Assemble the Contexto/Pregunta user turn for the quick-answer endpoint.

    `QUICK_ANSWER_INSTRUCTION` is sent separately as the system instruction
    (see `RAGService.quick_answer`) — Gemini has no inline system turn, so
    splitting it out there is both correct and idiomatic for this API.
    """
    context = build_quick_answer_context(chunks)
    return f"Contexto:\n{context}\n\nPregunta del usuario:\n{query}"