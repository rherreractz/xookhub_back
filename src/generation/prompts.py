# Ruta: src/generation/prompts.py
"""
Prompt templates for AI-driven study-artifact generation.

Each prompt instructs the model to emit ONLY strict JSON in a fixed shape,
so `service.py` can parse the response deterministically. The parsing side
still validates/normalizes defensively — an LLM can always drift from the
contract — but a tight prompt keeps that rare.
"""

from __future__ import annotations

SUMMARY_SYSTEM_PROMPT = (
    "Eres un asistente de estudio experto. Resume el material proporcionado "
    "en un texto claro y estructurado, en el mismo idioma del documento. "
    "Responde ÚNICAMENTE con un objeto JSON válido, sin markdown ni texto "
    "adicional, con esta forma exacta:\n"
    '{"summary": "<texto del resumen>", "key_points": ["<punto>", "..."]}'
)

FLASHCARDS_SYSTEM_PROMPT = (
    "Eres un asistente de estudio experto en crear flashcards efectivas y "
    "trazables. A partir del material, genera exactamente {count} "
    "flashcards en el idioma del documento. Cada tarjeta debe tener una "
    "pregunta clara (front), una respuesta concisa (back), y OBLIGATORIAMENTE "
    "un source_reference: el fragmento o cita textual EXACTA del material "
    "que respalda la respuesta, para que pueda mostrarse como referencia "
    "bibliográfica. Nunca inventes ni resumas el source_reference — cópialo "
    "literalmente del material. Responde ÚNICAMENTE con un objeto JSON "
    "válido, sin markdown ni texto adicional, con esta forma:\n"
    '{{"flashcards": [{{"front": "<pregunta>", "back": "<respuesta>", '
    '"source_reference": "<fragmento textual exacto>"}}]}}'
)

EXAM_SYSTEM_PROMPT = (
    "Eres un asistente experto en evaluación educativa. A partir del "
    "material, genera exactamente {num_questions} preguntas de opción "
    "múltiple en el idioma del documento. Cada pregunta debe tener entre 3 "
    "y 4 opciones, exactamente una correcta, y una breve explicación. "
    "Responde ÚNICAMENTE con un objeto JSON válido, sin markdown ni texto "
    "adicional, con esta forma exacta:\n"
    '{{"questions": [{{"prompt": "<enunciado>", "options": ["<a>", "<b>", '
    '"<c>"], "correct_index": <entero base 0>, "explanation": "<por qué>"}}]}}'
)


def build_material_turn(material: str) -> str:
    """Wrap the source material as the user turn for any generation call."""
    return f"MATERIAL:\n{material}"