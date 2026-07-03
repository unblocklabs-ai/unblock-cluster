from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

import tiktoken

ENCODING = tiktoken.get_encoding("cl100k_base")
PLACEHOLDER_RE = re.compile(r"\{([^{}]+)\}")


@dataclass(frozen=True)
class RenderedEmbeddingText:
    text: str
    text_hash: str
    token_count: int


def render_embedding_text(
    record: dict[str, Any],
    embedding_config: dict[str, Any],
) -> RenderedEmbeddingText:
    text_template = embedding_config.get("textTemplate")
    if text_template:
        text = PLACEHOLDER_RE.sub(
            lambda match: _stringify(record.get(match.group(1))),
            text_template,
        )
    else:
        lines = []
        for field in embedding_config["textFields"]:
            value = record.get(field)
            if value is None:
                continue
            rendered = _stringify(value)
            if rendered == "":
                continue
            lines.append(f"{field}: {rendered}")
        text = "\n".join(lines)

    max_tokens = int(embedding_config.get("maxInputTokens") or 8000)
    tokens = ENCODING.encode(text)
    if len(tokens) > max_tokens:
        tokens = tokens[:max_tokens]
        text = ENCODING.decode(tokens)
    text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return RenderedEmbeddingText(text=text, text_hash=text_hash, token_count=len(tokens))


def token_count(text: str) -> int:
    return len(ENCODING.encode(text))


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    return str(value)
