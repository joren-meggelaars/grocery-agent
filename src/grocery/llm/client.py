"""Receipt reading through the Claude API (or a fake in tests).

The model call goes through messages.create with a JSON schema; the reply is validated with
Pydantic here rather than inside the SDK, so the raw model output is kept even when it does
not validate.
"""

import base64
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import anthropic
from pydantic import ValidationError

from grocery.config import Settings
from grocery.llm.schemas import ReceiptExtraction
from grocery.refdata import CATEGORIES

log = logging.getLogger(__name__)

PROMPT_VERSION = "receipt_v1"
_PROMPT_FILE = Path(__file__).parent / "prompts" / f"{PROMPT_VERSION}.md"
MAX_OUTPUT_TOKENS = 16000


class ReaderUnavailable(RuntimeError):
    """No API key configured."""


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


@dataclass
class ReadResult:
    parsed: ReceiptExtraction | None
    raw: dict | None
    model: str
    usage: Usage = field(default_factory=Usage)
    request_id: str | None = None
    latency_ms: int = 0
    error: str | None = None
    retryable: bool = False  # transient failure (network, overload): worth another attempt


class ReceiptReader(Protocol):
    model: str
    prompt_version: str

    def read(self, images: list[bytes], text: str | None) -> ReadResult: ...


def system_prompt() -> str:
    names = ", ".join(f'"{name}"' for name, _ in CATEGORIES)
    return _PROMPT_FILE.read_text(encoding="utf-8").replace("{categories}", names)


class AnthropicReceiptReader:
    prompt_version = PROMPT_VERSION

    def __init__(self, settings: Settings) -> None:
        if settings.anthropic_api_key is None:
            raise ReaderUnavailable("ANTHROPIC_API_KEY is not set")
        self.model = settings.llm_model
        self.effort = settings.llm_effort
        self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key.get_secret_value())
        self._system = system_prompt()
        self._schema = anthropic.transform_schema(ReceiptExtraction.model_json_schema())

    def read(self, images: list[bytes], text: str | None) -> ReadResult:
        content: list[dict] = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": base64.standard_b64encode(img).decode(),
                },
            }
            for img in images
        ]
        instruction = "Extract this receipt."
        if text:
            instruction += f"\n\nText layer of the receipt:\n{text}"
        content.append({"type": "text", "text": instruction})

        started = time.monotonic()
        try:
            response = self._client.messages.create(
                model=self.model,
                max_tokens=MAX_OUTPUT_TOKENS,
                system=self._system,
                messages=[{"role": "user", "content": content}],
                output_config={
                    "effort": self.effort,
                    "format": {"type": "json_schema", "schema": self._schema},
                },
            )
        except (anthropic.APIConnectionError, anthropic.RateLimitError) as exc:
            return self._failure(f"{type(exc).__name__}: {exc}", started, retryable=True)
        except anthropic.APIStatusError as exc:
            return self._failure(
                f"API error {exc.status_code}: {exc.message}",
                started,
                retryable=exc.status_code >= 500,
                request_id=getattr(exc, "request_id", None),
            )

        usage = Usage(
            input_tokens=response.usage.input_tokens or 0,
            output_tokens=response.usage.output_tokens or 0,
            cache_read_tokens=getattr(response.usage, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(response.usage, "cache_creation_input_tokens", 0) or 0,
        )
        base = dict(
            model=self.model,
            usage=usage,
            request_id=getattr(response, "_request_id", None),
            latency_ms=int((time.monotonic() - started) * 1000),
        )

        text_out = next((b.text for b in response.content if b.type == "text"), "")
        if response.stop_reason == "refusal":
            return ReadResult(None, None, error="The model declined to read this image.", **base)
        if response.stop_reason == "max_tokens":
            return ReadResult(None, None, error="The model's answer was cut off (receipt too long).", **base)
        try:
            raw = json.loads(text_out)
        except ValueError:
            return ReadResult(None, None, error="The model did not return valid JSON.", **base)
        try:
            parsed = ReceiptExtraction.model_validate(raw)
        except ValidationError as exc:
            log.warning("receipt extraction failed validation: %s", exc.errors()[:3])
            return ReadResult(None, raw, error="The model's answer did not match the expected format.", **base)
        return ReadResult(parsed, raw, **base)

    def _failure(self, error: str, started: float, retryable: bool, request_id: str | None = None) -> ReadResult:
        return ReadResult(
            None,
            None,
            model=self.model,
            request_id=request_id,
            latency_ms=int((time.monotonic() - started) * 1000),
            error=error,
            retryable=retryable,
        )


def make_reader(settings: Settings) -> ReceiptReader:
    return AnthropicReceiptReader(settings)
