from __future__ import annotations

import asyncio
import math
import os
from pathlib import Path

import numpy as np
import pytest

from datagraph.core.summarization import (
    OpenAIChatSummaryProvider,
    effective_summarization_prompt,
    summarize_with_retry,
)
from datagraph.core.vectors import unpack_vector
from datagraph.db import connect
from scripts.gen_synthetic import generate_records
from tests.test_embedding import (
    _client as _embedding_client,
)
from tests.test_embedding import (
    _create_graph as _create_embedding_graph,
)
from tests.test_embedding import (
    _enqueue_embedding,
    _minimal_record,
)
from tests.test_embedding import (
    _poll_run as _poll_embedding_run,
)
from tests.test_embedding import (
    _post_records as _post_embedding_records,
)
from tests.test_labeling import (
    _cluster_fixture,
    _cluster_ids,
    _enqueue_label,
    _phase4_client,
)
from tests.test_labeling import (
    _poll_run as _poll_label_run,
)


@pytest.mark.skipif(not os.environ.get("OPENAI_API_KEY"), reason="OPENAI_API_KEY is not set")
def test_real_openai_embedding_integration_opt_in(tmp_path: Path) -> None:
    with _embedding_client(tmp_path) as client:
        graph = _create_embedding_graph(
            client,
            embedding={
                "provider": "openai",
                "model": "text-embedding-3-small",
                "dimensions": 1536,
            },
        )
        _post_embedding_records(
            client,
            graph["id"],
            [
                _minimal_record(
                    f"real-{index}",
                    customerText=f"Please help with issue number {index} about my order.",
                )
                for index in range(10)
            ],
        )
        run_id = _enqueue_embedding(client, graph["id"])
        run = _poll_embedding_run(client, graph["id"], run_id)
        assert run["status"] == "succeeded"
        with connect(client.app.state.settings.db_path) as conn:
            rows = conn.execute("SELECT vector FROM embedding_vectors").fetchall()
        assert len(rows) == 10
        for row in rows:
            vector = unpack_vector(row["vector"])
            assert vector.shape == (1536,)
            assert math.isclose(float(np.linalg.norm(vector)), 1.0, rel_tol=1e-5)


@pytest.mark.skipif(not os.environ.get("OPENAI_API_KEY"), reason="OPENAI_API_KEY is not set")
def test_real_openai_label_run_smoke(tmp_path: Path) -> None:
    with _phase4_client(tmp_path, label_provider=None) as client:
        graph_id, view_id, cluster_run_id = _cluster_fixture(client, record_count=160)
        cluster_ids = _cluster_ids(client, cluster_run_id)[:3]
        run_id = _enqueue_label(client, graph_id, view_id, body={"clusterIds": cluster_ids})
        run = _poll_label_run(client, graph_id, run_id, timeout=240)
        assert run["status"] == "succeeded"
        topics = client.get(
            f"/api/graphs/{graph_id}/views/{view_id}/topics",
            params={"clusterRunId": cluster_run_id},
        ).json()
        labeled = [
            topic["label"] for topic in topics["topics"] if topic["clusterId"] in set(cluster_ids)
        ]
        assert len(labeled) == len(cluster_ids)
        assert all(label["summary"] for label in labeled)
        assert all(1 <= len(label["label"].split()) <= 10 for label in labeled)


@pytest.mark.skipif(not os.environ.get("OPENAI_API_KEY"), reason="OPENAI_API_KEY not set")
def test_real_openai_summarizer_schema_and_verbatim_phrases() -> None:
    records = generate_records(5000, 42)[:10]
    provider = OpenAIChatSummaryProvider(
        api_key=os.environ.get("OPENAI_API_KEY"),
        model="gpt-5.4-nano",
    )
    prompt = effective_summarization_prompt(
        {"prompt": None, "context": "A consumer supplement and meal delivery brand."}
    )
    for record in records:
        text = record["customerText"]
        result, _attempts = asyncio.run(summarize_with_retry(provider, prompt, text))
        assert result.issue
        assert result.key_customer_phrases
        assert any(phrase in text for phrase in result.key_customer_phrases)
