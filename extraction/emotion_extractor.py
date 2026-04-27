"""
extraction/emotion_extractor.py
--------------------------------
Converts any input (text, game reviews, descriptions) into a 9-dimensional
emotional vector using an LLM backend (Ollama local or OpenAI).
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field, asdict
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

DIMENSIONS = ["pace", "tension", "agency", "warmth", "scale", "beauty", "dread", "wonder", "rivalry"]

DIMENSION_GUIDE = """
- pace     (0=slow/meditative, 10=frantic/twitch): How fast the game demands you respond and move
- tension  (0=relaxed/carefree, 10=high-stakes/stressful): How much every decision feels like it matters right now
- agency   (0=passive/on-rails, 10=full player control): How much the outcome depends on your choices
- warmth   (0=cold/clinical/hostile, 10=emotionally safe/nurturing/human): Whether the game feels welcoming
- scale    (0=intimate/small, 10=vast/epic/cosmic): The felt size of the world and your place in it
- beauty   (0=purely functional, 10=artistic/aesthetic): How much visual or sonic artistry is part of the experience
- dread    (0=none, 10=pervasive fear/unease): Fear, unease, the feeling that something is wrong
- wonder   (0=predictable, 10=revelatory/mind-expanding): Surprise, discovery, the feeling of a world larger than you expected
- rivalry  (0=solo/cooperative, 10=purely competitive against humans): How much the thrill comes from competing
"""

EXTRACTION_SYSTEM_PROMPT = f"""You are an expert game psychologist and emotion analyst.
Your task is to extract emotional dimension scores from text about a video game.
Score each dimension 0-10 (decimals allowed) based on the emotional experience, NOT game mechanics.

Dimensions:
{DIMENSION_GUIDE}

ALWAYS respond with valid JSON only. No preamble, no explanation outside the JSON.
Format:
{{
  "pace": <float 0-10>,
  "tension": <float 0-10>,
  "agency": <float 0-10>,
  "warmth": <float 0-10>,
  "scale": <float 0-10>,
  "beauty": <float 0-10>,
  "dread": <float 0-10>,
  "wonder": <float 0-10>,
  "rivalry": <float 0-10>,
  "confidence": <float 0-1>,
  "justifications": {{
    "pace": "<one sentence>",
    "tension": "<one sentence>",
    "agency": "<one sentence>",
    "warmth": "<one sentence>",
    "scale": "<one sentence>",
    "beauty": "<one sentence>",
    "dread": "<one sentence>",
    "wonder": "<one sentence>",
    "rivalry": "<one sentence>"
  }}
}}"""

USER_INPUT_SYSTEM_PROMPT = f"""You are an emotion interpreter for a game discovery app.
A user has described how they want to FEEL while playing a game.
Your job is to translate their description into a 9-dimensional emotional target vector.

Dimensions:
{DIMENSION_GUIDE}

Score dimensions based on what the user WANTS to feel. If they don't mention a dimension, infer a neutral default.
ALWAYS respond with valid JSON only. Same format as above."""


@dataclass
class EmotionVector:
    pace: float = 5.0
    tension: float = 5.0
    agency: float = 5.0
    warmth: float = 5.0
    scale: float = 5.0
    beauty: float = 5.0
    dread: float = 0.0
    wonder: float = 5.0
    rivalry: float = 5.0
    confidence: float = 0.5
    justifications: dict = field(default_factory=dict)

    def to_list(self) -> list[float]:
        return [getattr(self, d) for d in DIMENSIONS]

    def to_dict(self) -> dict:
        return {d: getattr(self, d) for d in DIMENSIONS}

    @classmethod
    def from_dict(cls, d: dict) -> "EmotionVector":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

    @classmethod
    def midpoint(cls) -> "EmotionVector":
        """Neutral/unknown default."""
        return cls()

    def contrast(self, other: "EmotionVector") -> "EmotionVector":
        """Compute a target vector from a loved game minus a hated game."""
        result = {}
        for d in DIMENSIONS:
            loved = getattr(self, d)
            hated = getattr(other, d)
            # Target: push toward loved, away from hated
            delta = loved - hated
            target = max(0.0, min(10.0, 5.0 + delta * 0.8))
            result[d] = target
        result["confidence"] = min(self.confidence, other.confidence)
        return EmotionVector(**result)


class EmotionExtractor:
    """
    Extracts 9-dimensional emotion vectors using an LLM.
    Falls back gracefully: OpenAI → Ollama → heuristic defaults.
    """

    def __init__(
        self,
        ollama_host: str = None,
        openai_api_key: str = None,
        model_local: str = "llama3",
        model_openai: str = "gpt-4o",
        timeout: int = 30,
    ):
        self.ollama_host = ollama_host or os.getenv("OLLAMA_HOST", "http://localhost:11434")
        self.openai_api_key = openai_api_key or os.getenv("OPENAI_API_KEY", "")
        self.model_local = model_local
        self.model_openai = model_openai
        self.timeout = timeout

    # ── Public API ────────────────────────────────────────────────────────────

    def from_reviews(self, reviews_text: str, game_name: str = "") -> EmotionVector:
        """Extract emotion vector from aggregated Steam reviews."""
        prompt = f"Game: {game_name}\n\nPlayer reviews (aggregated):\n{reviews_text[:4000]}"
        return self._extract(prompt, system=EXTRACTION_SYSTEM_PROMPT)

    def from_description(self, description: str, game_name: str = "") -> EmotionVector:
        """Extract from developer description and metadata."""
        prompt = f"Game: {game_name}\n\nGame description:\n{description[:3000]}"
        return self._extract(prompt, system=EXTRACTION_SYSTEM_PROMPT)

    def from_user_text(self, user_input: str) -> EmotionVector:
        """Convert free-text user feeling description to a target vector."""
        return self._extract(user_input, system=USER_INPUT_SYSTEM_PROMPT)

    def from_anchor_games(
        self, loved_vector: EmotionVector, hated_vector: EmotionVector
    ) -> EmotionVector:
        """Derive target from loved/hated game contrast."""
        return loved_vector.contrast(hated_vector)

    def merge_weighted(
        self,
        vectors: list[EmotionVector],
        weights: list[float],
    ) -> EmotionVector:
        """Weighted average of multiple vectors (e.g. reviews + description + metadata)."""
        assert len(vectors) == len(weights), "vectors and weights must match"
        total_w = sum(weights)
        result = {d: 0.0 for d in DIMENSIONS}
        for vec, w in zip(vectors, weights):
            for d in DIMENSIONS:
                result[d] += getattr(vec, d) * (w / total_w)
        result["confidence"] = sum(v.confidence * w for v, w in zip(vectors, weights)) / total_w
        return EmotionVector(**result)

    # ── Internal ──────────────────────────────────────────────────────────────

    def _extract(self, user_message: str, system: str) -> EmotionVector:
        """Try OpenAI first, fall back to Ollama."""
        if self.openai_api_key:
            try:
                return self._call_openai(user_message, system)
            except Exception as e:
                logger.warning(f"OpenAI failed ({e}), falling back to Ollama")

        try:
            return self._call_ollama(user_message, system)
        except Exception as e:
            logger.error(f"Ollama failed ({e}), returning default vector")
            return EmotionVector(confidence=0.0)

    def _call_openai(self, user_message: str, system: str) -> EmotionVector:
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {self.openai_api_key}"},
                json={
                    "model": self.model_openai,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user_message},
                    ],
                    "temperature": 0.1,
                    "response_format": {"type": "json_object"},
                },
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
            return self._parse_response(content)

    def _call_ollama(self, user_message: str, system: str) -> EmotionVector:
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(
                f"{self.ollama_host}/api/chat",
                json={
                    "model": self.model_local,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user_message},
                    ],
                    "stream": False,
                    "options": {"temperature": 0.1},
                },
            )
            resp.raise_for_status()
            content = resp.json()["message"]["content"]
            return self._parse_response(content)

    def _parse_response(self, content: str) -> EmotionVector:
        """Parse LLM JSON response into EmotionVector."""
        # Strip markdown code blocks if present
        content = re.sub(r"```(?:json)?", "", content).strip()
        try:
            data = json.loads(content)
            return EmotionVector(
                pace=float(data.get("pace", 5)),
                tension=float(data.get("tension", 5)),
                agency=float(data.get("agency", 5)),
                warmth=float(data.get("warmth", 5)),
                scale=float(data.get("scale", 5)),
                beauty=float(data.get("beauty", 5)),
                dread=float(data.get("dread", 0)),
                wonder=float(data.get("wonder", 5)),
                rivalry=float(data.get("rivalry", 5)),
                confidence=float(data.get("confidence", 0.7)),
                justifications=data.get("justifications", {}),
            )
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.error(f"Failed to parse LLM response: {e}\nContent: {content[:500]}")
            return EmotionVector(confidence=0.1)
