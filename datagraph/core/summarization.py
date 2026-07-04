from __future__ import annotations

import asyncio
import hashlib
import json
import random
from dataclasses import dataclass, field
from typing import Any, Protocol

from openai import AsyncOpenAI

from datagraph.core.openai_client import (
    MAX_RETRY_ATTEMPTS,
    SleepFunc,
    TokenUsage,
    _is_retryable,
    _retry_after,
    token_usage_from_response_usage,
    zero_token_usage,
)

JUNK_TYPES = {
    "none",
    "ooo",
    "vendor_pitch",
    "newsletter",
    "platform_notification",
    "other_non_support",
}

SUMMARY_RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "issue": {"type": "string"},
        "product": {"type": ["string", "null"]},
        "desiredResolution": {"type": ["string", "null"]},
        "sentiment": {"type": ["string", "null"]},
        "keyCustomerPhrases": {"type": "array", "items": {"type": "string"}},
        "junkType": {"type": "string", "enum": sorted(JUNK_TYPES)},
    },
    "required": [
        "issue",
        "product",
        "desiredResolution",
        "sentiment",
        "keyCustomerPhrases",
        "junkType",
    ],
}

DEFAULT_SUMMARIZATION_PROMPT = """You extract one structured summary from one customer
feedback record.

Focus on the PRIMARY customer-authored support issue in the record. Preserve
the customer's vocabulary: keyCustomerPhrases must quote short phrases
verbatim from the customer text, not paraphrases.

Ignore out-of-office notices, auto-replies, signatures, tracking footers,
quoted boilerplate, and system-generated messages when they appear inside an
otherwise legitimate multi-message customer thread. Classify the whole record's
junkType only when the record is primarily non-support junk for this business.

Return concise fields:
- issue: the primary customer problem or request, as a short plain sentence.
- product: the product, plan, SKU family, or service area when present.
- desiredResolution: what the customer wants to happen, when clear.
- sentiment: a short sentiment label when clear.
- keyCustomerPhrases: 1-5 short verbatim customer phrases that justify the issue.
- junkType: one of none, ooo, vendor_pitch, newsletter, platform_notification,
  other_non_support."""


@dataclass(frozen=True)
class SummaryResult:
    issue: str
    product: str | None
    desired_resolution: str | None
    sentiment: str | None
    key_customer_phrases: list[str]
    junk_type: str
    token_usage: TokenUsage = field(default_factory=zero_token_usage)


class SummaryProvider(Protocol):
    async def summarize_record(self, prompt: str, record_text: str) -> SummaryResult:
        ...


class SummaryValidationError(ValueError):
    pass


class SummaryRetryExhausted(RuntimeError):
    def __init__(self, message: str, *, attempts: int) -> None:
        self.attempts = attempts
        super().__init__(message)


class OpenAIChatSummaryProvider:
    def __init__(self, *, api_key: str | None, model: str) -> None:
        self.model = model
        self.client = AsyncOpenAI(api_key=api_key, max_retries=0)

    async def summarize_record(self, prompt: str, record_text: str) -> SummaryResult:
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": record_text},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "record_summary",
                    "strict": True,
                    "schema": SUMMARY_RESULT_SCHEMA,
                },
            },
        )
        content = response.choices[0].message.content
        if not content:
            raise SummaryValidationError("summary provider returned empty content")
        try:
            payload = json.loads(content)
        except json.JSONDecodeError as exc:
            raise SummaryValidationError("summary provider returned invalid JSON") from exc
        result = validate_summary_result(payload)
        return SummaryResult(
            issue=result.issue,
            product=result.product,
            desired_resolution=result.desired_resolution,
            sentiment=result.sentiment,
            key_customer_phrases=result.key_customer_phrases,
            junk_type=result.junk_type,
            token_usage=token_usage_from_response_usage(response.usage),
        )


def make_summary_provider(
    summarization_config: dict[str, Any],
    *,
    api_key: str | None = None,
) -> SummaryProvider:
    return OpenAIChatSummaryProvider(api_key=api_key, model=summarization_config["model"])


async def summarize_with_retry(
    provider: SummaryProvider,
    prompt: str,
    record_text: str,
    *,
    sleep: SleepFunc = asyncio.sleep,
    max_attempts: int = MAX_RETRY_ATTEMPTS,
) -> tuple[SummaryResult, int]:
    attempts = 0
    validation_failures = 0
    while True:
        attempts += 1
        try:
            return await provider.summarize_record(prompt, record_text), attempts
        except SummaryValidationError as exc:
            validation_failures += 1
            if validation_failures >= 2:
                raise SummaryRetryExhausted(
                    "summary provider returned invalid schema after "
                    f"{validation_failures} attempts",
                    attempts=attempts,
                ) from exc
        except Exception as exc:  # noqa: BLE001 - provider boundary normalizes retryable failures.
            retry_after = _retry_after(exc)
            if attempts >= max_attempts or not _is_retryable(exc):
                raise SummaryRetryExhausted(
                    f"summary provider failed after {attempts} attempts: {exc}",
                    attempts=attempts,
                ) from exc
            delay = (
                retry_after
                if retry_after is not None
                else min(2 ** (attempts - 1), 8) + random.uniform(0, 0.05)
            )
            await sleep(delay)


def effective_summarization_prompt(summarization_config: dict[str, Any]) -> str:
    prompt = summarization_config.get("prompt")
    base = (
        prompt.strip()
        if isinstance(prompt, str) and prompt.strip()
        else DEFAULT_SUMMARIZATION_PROMPT
    )
    context = summarization_config.get("context")
    if isinstance(context, str) and context.strip():
        return f"{base}\n\nBrand/service context:\n{context.strip()}"
    return base


def prompt_sha256(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def render_summary_text(result: SummaryResult) -> str:
    lines = [
        f"issue: {result.issue}",
        f"product: {_nullable(result.product)}",
        f"desiredResolution: {_nullable(result.desired_resolution)}",
        f"sentiment: {_nullable(result.sentiment)}",
        f"junkType: {result.junk_type}",
        "keyCustomerPhrases:",
    ]
    lines.extend(f"- {phrase}" for phrase in result.key_customer_phrases)
    return "\n".join(lines)


def summary_result_to_json(result: SummaryResult) -> dict[str, Any]:
    return {
        "issue": result.issue,
        "product": result.product,
        "desiredResolution": result.desired_resolution,
        "sentiment": result.sentiment,
        "keyCustomerPhrases": result.key_customer_phrases,
        "junkType": result.junk_type,
    }


def validate_summary_result(value: Any) -> SummaryResult:
    if not isinstance(value, dict):
        raise SummaryValidationError("summary result must be an object")
    unknown = set(value) - {
        "issue",
        "product",
        "desiredResolution",
        "sentiment",
        "keyCustomerPhrases",
        "junkType",
    }
    if unknown:
        raise SummaryValidationError("summary result contains unknown keys")
    issue = value.get("issue")
    product = value.get("product")
    desired_resolution = value.get("desiredResolution")
    sentiment = value.get("sentiment")
    phrases = value.get("keyCustomerPhrases")
    junk_type = value.get("junkType")
    if not isinstance(issue, str) or not issue.strip():
        raise SummaryValidationError("issue must be a non-empty string")
    for field_name, field_value in (
        ("product", product),
        ("desiredResolution", desired_resolution),
        ("sentiment", sentiment),
    ):
        if field_value is not None and not isinstance(field_value, str):
            raise SummaryValidationError(f"{field_name} must be a string or null")
    if not isinstance(phrases, list) or not all(isinstance(item, str) for item in phrases):
        raise SummaryValidationError("keyCustomerPhrases must be a list of strings")
    cleaned_phrases = [phrase.strip() for phrase in phrases if phrase.strip()]
    if not cleaned_phrases:
        raise SummaryValidationError("keyCustomerPhrases must include at least one phrase")
    if junk_type not in JUNK_TYPES:
        raise SummaryValidationError("junkType is invalid")
    return SummaryResult(
        issue=issue.strip(),
        product=_clean_optional(product),
        desired_resolution=_clean_optional(desired_resolution),
        sentiment=_clean_optional(sentiment),
        key_customer_phrases=cleaned_phrases,
        junk_type=junk_type,
    )


def _clean_optional(value: Any) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _nullable(value: str | None) -> str:
    return value if value is not None else "(none)"
