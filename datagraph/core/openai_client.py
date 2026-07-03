from __future__ import annotations

import asyncio
import hashlib
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

import numpy as np
from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI, RateLimitError

from datagraph.core.embedding_text import token_count
from datagraph.core.vectors import normalize_l2

MAX_BATCH_INPUTS = 512
MAX_BATCH_TOKENS = 200_000
MAX_RETRY_ATTEMPTS = 4

SleepFunc = Callable[[float], Awaitable[None]]
ClockFunc = Callable[[], float]


class EmbeddingProvider(Protocol):
    async def embed_batch(self, texts: list[str]) -> list[np.ndarray]:
        ...


class ProviderRetryError(RuntimeError):
    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        self.retry_after = retry_after
        super().__init__(message)


class MockEmbeddingProvider:
    def __init__(self, dimensions: int = 1536, model: str = "mock") -> None:
        self.dimensions = dimensions
        self.model = model
        self.request_count = 0

    async def embed_batch(self, texts: list[str]) -> list[np.ndarray]:
        self.request_count += 1
        return [self.embed_text(text) for text in texts]

    async def embed_texts(self, texts: list[str]) -> list[np.ndarray]:
        return await self.embed_batch(texts)

    def embed_text(self, text: str) -> np.ndarray:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        seed = int.from_bytes(digest[:8], "big", signed=False)
        rng = np.random.default_rng(seed)
        return normalize_l2(rng.standard_normal(self.dimensions, dtype=np.float32))


class OpenAIEmbeddingProvider:
    def __init__(self, *, api_key: str | None, model: str, dimensions: int | None = None) -> None:
        self.model = model
        self.dimensions = dimensions
        self.client = AsyncOpenAI(api_key=api_key, max_retries=0)

    async def embed_batch(self, texts: list[str]) -> list[np.ndarray]:
        kwargs: dict[str, Any] = {"model": self.model, "input": texts}
        if self.dimensions is not None:
            kwargs["dimensions"] = self.dimensions
        response = await self.client.embeddings.create(**kwargs)
        ordered = sorted(response.data, key=lambda item: item.index)
        return [normalize_l2(item.embedding) for item in ordered]


@dataclass(frozen=True)
class Batch:
    texts: list[str]
    token_count: int


class TokenBucketRateLimiter:
    def __init__(
        self,
        *,
        requests_per_minute: int,
        clock: ClockFunc,
        sleep: SleepFunc,
    ) -> None:
        self.capacity = max(int(requests_per_minute), 1)
        self.refill_per_second = self.capacity / 60.0
        self.clock = clock
        self.sleep = sleep
        self.tokens = float(self.capacity)
        self.updated_at = clock()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        while True:
            async with self._lock:
                now = self.clock()
                elapsed = max(now - self.updated_at, 0.0)
                self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_per_second)
                self.updated_at = now
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return
                wait_seconds = (1.0 - self.tokens) / self.refill_per_second
            await self.sleep(wait_seconds)


def pack_embedding_batches(
    texts: list[str],
    *,
    max_inputs: int = MAX_BATCH_INPUTS,
    max_tokens: int = MAX_BATCH_TOKENS,
) -> list[Batch]:
    batches: list[Batch] = []
    current: list[str] = []
    current_tokens = 0
    for text in texts:
        tokens = token_count(text)
        if current and (len(current) >= max_inputs or current_tokens + tokens > max_tokens):
            batches.append(Batch(texts=current, token_count=current_tokens))
            current = []
            current_tokens = 0
        current.append(text)
        current_tokens += tokens
    if current:
        batches.append(Batch(texts=current, token_count=current_tokens))
    return batches


async def embed_with_retry(
    provider: EmbeddingProvider,
    texts: list[str],
    *,
    sleep: SleepFunc = asyncio.sleep,
    max_attempts: int = MAX_RETRY_ATTEMPTS,
) -> list[np.ndarray]:
    attempt = 0
    while True:
        attempt += 1
        try:
            return await provider.embed_batch(texts)
        except Exception as exc:  # noqa: BLE001 - provider boundary normalizes retryable failures.
            retry_after = _retry_after(exc)
            if attempt >= max_attempts or not _is_retryable(exc):
                raise RuntimeError(
                    f"embedding provider failed after {attempt} attempts: {exc}"
                ) from exc
            delay = (
                retry_after
                if retry_after is not None
                else min(2 ** (attempt - 1), 8) + random.uniform(0, 0.05)
            )
            await sleep(delay)


def make_embedding_provider(
    embedding_config: dict[str, Any],
    *,
    api_key: str | None = None,
) -> EmbeddingProvider:
    dimensions = int(embedding_config.get("dimensions") or 1536)
    provider = embedding_config["provider"]
    if provider == "mock":
        return MockEmbeddingProvider(dimensions=dimensions, model=embedding_config["model"])
    return OpenAIEmbeddingProvider(
        api_key=api_key,
        model=embedding_config["model"],
        dimensions=embedding_config.get("dimensions"),
    )


def _is_retryable(exc: Exception) -> bool:
    if isinstance(exc, ProviderRetryError | RateLimitError | APITimeoutError | APIConnectionError):
        return True
    if isinstance(exc, APIStatusError):
        return exc.status_code == 429 or exc.status_code >= 500
    return False


def _retry_after(exc: Exception) -> float | None:
    if isinstance(exc, ProviderRetryError):
        return exc.retry_after
    response = getattr(exc, "response", None)
    if response is None:
        return None
    headers = getattr(response, "headers", {}) or {}
    value = headers.get("retry-after") or headers.get("Retry-After")
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None
