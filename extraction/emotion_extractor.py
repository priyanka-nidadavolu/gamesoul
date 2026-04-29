"""
extraction/emotion_extractor.py
--------------------------------
Converts any input into a 9-dimensional emotional vector.
Cloud version: OpenAI primary, Ollama optional local fallback.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

DIMENSIONS = ["pace", "tension", "agency", "warmth", "scale", "beauty", "dread", "wonder", "rivalry"]

DIMENSION_GUIDE = """
- pace     (0=slow/meditative, 10=frantic/twitch)
- tension  (0=relaxed/carefree, 10=high-stakes/stressful)
- agency   (0=passive/on-rails, 10=full player control)
- warmth   (0=cold/clinical/hostile, 10=emotionally safe/nurturing)
- scale    (0=intimate/small, 10=vast/epic/cosmic)
- beauty   (0=purely functional, 10=artistic/aesthetic)
- dread    (0=none, 10=pervasive fear/unease)
- wonder   (0=predictable, 10=revelatory/mind-expanding)
- rivalry  (0=solo/cooperative, 10=purely competitive vs humans)
"""

EXTRACTION_SYSTEM = f"""You are an expert game psychologist.
Extract emotional dimension scores from text about a video game.
Score each 0-10 based on the emotional EXPERIENCE, not mechanics.

Dimensions:{DIMENSION_GUIDE}

Respond ONLY with valid JSON, no preamble:
{{
  "pace": float, "tension": float, "agency": float, "warmth": float,
  "scale": float, "beauty": float, "dread": float, "wonder": float,
  "rivalry": float, "confidence": float (0-1),
  "justifications": {{"pace": "one sentence", ...}}
}}"""

USER_INPUT_SYSTEM = f"""You are an emotion interpreter for a game discovery app.
Translate a user's desired feeling into a 9-dimensional target vector.

Dimensions:{DIMENSION_GUIDE}

Respond ONLY with valid JSON, same format as above."""


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
        return cls()

    def contrast(self, other: "EmotionVector") -> "EmotionVector":
        result = {}
        for d in DIMENSIONS:
            delta = getattr(self, d) - getattr(other, d)
            result[d] = max(0.0, min(10.0, 5.0 + delta * 0.8))
        result["confidence"] = min(self.confidence, other.confidence)
        return EmotionVector(**result)


class EmotionExtractor:
    """
    OpenAI-primary extractor with optional Ollama fallback.
    Set OPENAI_API_KEY env var to enable.
    """

    def __init__(
        self,
        openai_api_key: str = None,
        ollama_host: str = None,
        model_openai: str = "gpt-4o-mini",   # cheap + fast
        model_local: str = "tinyllama",
        timeout: int = 30,
    ):
        self.openai_api_key = openai_api_key or os.getenv("OPENAI_API_KEY", "")
        self.ollama_host = ollama_host or os.getenv("OLLAMA_HOST", "http://localhost:11434")
        self.model_openai = model_openai
        self.model_local = model_local
        self.timeout = timeout

    # ── Public API ────────────────────────────────────────────────────────────

    def from_reviews(self, reviews_text: str, game_name: str = "") -> EmotionVector:
        prompt = f"Game: {game_name}\n\nPlayer reviews:\n{reviews_text[:4000]}"
        return self._extract(prompt, system=EXTRACTION_SYSTEM)

    def from_description(self, description: str, game_name: str = "") -> EmotionVector:
        prompt = f"Game: {game_name}\n\nDescription:\n{description[:3000]}"
        return self._extract(prompt, system=EXTRACTION_SYSTEM)

    def from_user_text(self, user_input: str) -> EmotionVector:
        return self._extract(user_input, system=USER_INPUT_SYSTEM)

    def from_anchor_games(self, loved: EmotionVector, hated: EmotionVector) -> EmotionVector:
        return loved.contrast(hated)

    def merge_weighted(self, vectors: list[EmotionVector], weights: list[float]) -> EmotionVector:
        total_w = sum(weights)
        result = {d: 0.0 for d in DIMENSIONS}
        for vec, w in zip(vectors, weights):
            for d in DIMENSIONS:
                result[d] += getattr(vec, d) * (w / total_w)
        result["confidence"] = sum(v.confidence * w for v, w in zip(vectors, weights)) / total_w
        return EmotionVector(**result)

    # ── Internal ──────────────────────────────────────────────────────────────

    def _extract(self, user_message: str, system: str) -> EmotionVector:
        if self.openai_api_key:
            try:
                return self._call_openai(user_message, system)
            except Exception as e:
                logger.warning(f"OpenAI failed ({e}), trying Ollama")
        try:
            return self._call_ollama(user_message, system)
        except Exception as e:
            logger.error(f"All LLMs failed ({e}), returning default vector")
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
            return self._parse(content)

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
            return self._parse(content)

    # Backward-compatible alias used by existing tests/callers.
    def _parse_response(self, content: str) -> EmotionVector:
        return self._parse(content)

    def _parse(self, content: str) -> EmotionVector:
        content = re.sub(r"```(?:json)?", "", content).strip()
        try:
            data = json.loads(content)
            return EmotionVector(
                **{d: float(data.get(d, 5)) for d in DIMENSIONS},
                confidence=float(data.get("confidence", 0.7)),
                justifications=data.get("justifications", {}),
            )
        except Exception as e:
            logger.error(f"Parse failed: {e} | content: {content[:200]}")
            return EmotionVector(confidence=0.1)
