"""Persona overlay — the text side of an agent's voice persona.

Voice identity (voice_id / vibe) lives in AGENTS.md frontmatter and is applied
at the TTS layer. This overlay shapes the *prompt* side: it frames the user's
spoken utterance for the agent turn so the reply stays in the agent's lane and
register. Deliberately minimal in phase 1 — a labeled wrapper, not a second
prompt engine.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SimplePersona:
    """Wraps the utterance with the agent's name + a spoken-context marker.

    `name` labels who is answering; `style` is a one-line register hint
    (e.g. "warm, concise"). Empty style => just the spoken-context marker.
    """

    name: str = "assistant"
    style: str = ""

    def apply(self, utterance: str) -> str:
        style = f" ({self.style})" if self.style else ""
        return (
            f"[voice turn for {self.name}{style}] "
            f"The user said aloud: {utterance.strip()}"
        )
