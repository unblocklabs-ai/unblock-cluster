from __future__ import annotations

import asyncio
import json
import random
from dataclasses import dataclass
from typing import Any, Protocol

from openai import AsyncOpenAI

from datagraph.core.openai_client import MAX_RETRY_ATTEMPTS, SleepFunc, _is_retryable, _retry_after

LABEL_RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "label": {"type": "string"},
        "summary": {"type": "string"},
        "keySignals": {"type": "array", "items": {"type": "string"}},
        "tags": {"type": "array", "items": {"type": "string"}},
        "coherent": {"type": "boolean"},
    },
    "required": ["label", "summary", "keySignals", "tags", "coherent"],
}


@dataclass(frozen=True)
class LabelResult:
    label: str
    summary: str
    key_signals: list[str]
    tags: list[str]
    coherent: bool


class LabelProvider(Protocol):
    async def label_cluster(self, prompt: str, representatives: list[str]) -> LabelResult:
        ...


class LabelValidationError(ValueError):
    pass


class OpenAIChatLabelProvider:
    def __init__(self, *, api_key: str | None, model: str) -> None:
        self.model = model
        self.client = AsyncOpenAI(api_key=api_key, max_retries=0)

    async def label_cluster(self, prompt: str, representatives: list[str]) -> LabelResult:
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": "\n\n".join(representatives)},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "cluster_label",
                    "strict": True,
                    "schema": LABEL_RESULT_SCHEMA,
                },
            },
        )
        content = response.choices[0].message.content
        if not content:
            raise LabelValidationError("label provider returned empty content")
        try:
            payload = json.loads(content)
        except json.JSONDecodeError as exc:
            raise LabelValidationError("label provider returned invalid JSON") from exc
        return validate_label_result(payload)


def make_label_provider(
    labeling_config: dict[str, Any],
    *,
    api_key: str | None = None,
) -> LabelProvider:
    return OpenAIChatLabelProvider(api_key=api_key, model=labeling_config["model"])


async def label_with_retry(
    provider: LabelProvider,
    prompt: str,
    representatives: list[str],
    *,
    sleep: SleepFunc = asyncio.sleep,
    max_attempts: int = MAX_RETRY_ATTEMPTS,
) -> tuple[LabelResult, int]:
    attempts = 0
    validation_failures = 0
    while True:
        attempts += 1
        try:
            return await provider.label_cluster(prompt, representatives), attempts
        except LabelValidationError as exc:
            validation_failures += 1
            if validation_failures >= 2:
                raise RuntimeError(
                    f"label provider returned invalid schema after {validation_failures} attempts"
                ) from exc
        except Exception as exc:  # noqa: BLE001 - provider boundary normalizes retryable failures.
            retry_after = _retry_after(exc)
            if attempts >= max_attempts or not _is_retryable(exc):
                raise RuntimeError(
                    f"label provider failed after {attempts} attempts: {exc}"
                ) from exc
            delay = (
                retry_after
                if retry_after is not None
                else min(2 ** (attempts - 1), 8) + random.uniform(0, 0.05)
            )
            await sleep(delay)


def validate_label_result(value: Any) -> LabelResult:
    if not isinstance(value, dict):
        raise LabelValidationError("label result must be an object")
    unknown = set(value) - {"label", "summary", "keySignals", "tags", "coherent"}
    if unknown:
        raise LabelValidationError("label result contains unknown keys")
    label = value.get("label")
    summary = value.get("summary")
    key_signals = value.get("keySignals")
    tags = value.get("tags")
    coherent = value.get("coherent")
    if not isinstance(label, str) or not label.strip():
        raise LabelValidationError("label must be a non-empty string")
    if not isinstance(summary, str) or not summary.strip():
        raise LabelValidationError("summary must be a non-empty string")
    if not _is_string_list(key_signals):
        raise LabelValidationError("keySignals must be a list of strings")
    if not _is_string_list(tags):
        raise LabelValidationError("tags must be a list of strings")
    if not isinstance(coherent, bool):
        raise LabelValidationError("coherent must be a boolean")
    return LabelResult(
        label=label.strip(),
        summary=summary.strip(),
        key_signals=key_signals,
        tags=tags,
        coherent=coherent,
    )


def _is_string_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)
