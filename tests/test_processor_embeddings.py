import os
import unittest

import numpy as np

import processor


class MemoryEmbeddingCache:
    def __init__(self):
        self.values = {}
        self.get_calls = 0
        self.set_calls = 0

    @staticmethod
    def key(provider, model, dimensions, text_hash):
        return (provider, model, dimensions or 0, text_hash)

    def get_many(self, provider, model, dimensions, text_hashes):
        self.get_calls += 1
        return {
            text_hash: self.values[self.key(provider, model, dimensions, text_hash)]
            for text_hash in text_hashes
            if self.key(provider, model, dimensions, text_hash) in self.values
        }

    def set_many(self, provider, model, dimensions, embeddings_by_hash):
        self.set_calls += 1
        for text_hash, embedding in embeddings_by_hash.items():
            self.values[self.key(provider, model, dimensions, text_hash)] = embedding


class ProcessorEmbeddingTests(unittest.TestCase):
    def setUp(self):
        self.saved_env = {
            key: os.environ.get(key)
            for key in (
                "OPENAI_API_KEY",
                "DATA_GRAPH_TEXT_FEATURE_METHOD",
                "DATA_GRAPH_EMBEDDING_MODEL",
                "DATA_GRAPH_EMBEDDING_DIMENSIONS",
                "DATA_GRAPH_EMBEDDING_BATCH_SIZE",
                "DATA_GRAPH_EMBEDDING_TIMEOUT_SECONDS",
            )
        }
        for key in self.saved_env:
            os.environ.pop(key, None)

    def tearDown(self):
        for key, value in self.saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_tfidf_is_default_text_feature_method(self):
        resolved = processor.resolve_processor_options({"cluster": {}})

        self.assertEqual(resolved["textFeatureMethod"], "tfidf")
        self.assertEqual(resolved["embeddingModel"], "text-embedding-3-small")

    def test_graph_config_overrides_env_defaults(self):
        os.environ["DATA_GRAPH_TEXT_FEATURE_METHOD"] = "tfidf"
        os.environ["DATA_GRAPH_EMBEDDING_MODEL"] = "env-model"
        os.environ["DATA_GRAPH_EMBEDDING_DIMENSIONS"] = "256"

        resolved = processor.resolve_processor_options(
            {
                "cluster": {
                    "textFeatureMethod": "embedding",
                    "embeddingModel": "graph-model",
                    "embeddingDimensions": 128,
                }
            }
        )

        self.assertEqual(resolved["textFeatureMethod"], "embedding")
        self.assertEqual(resolved["embeddingModel"], "graph-model")
        self.assertEqual(resolved["embeddingDimensions"], 128)

    def test_embedding_requires_api_key(self):
        sink = {
            "config": {
                "dataSchema": {"name": "String", "kind": "String"},
                "groupingFields": ["kind"],
                "cluster": {"textFeatureMethod": "embedding"},
            }
        }

        with self.assertRaises(processor.EmbeddingProcessingError) as context:
            processor.process_records(sink, [{"name": "one", "kind": "a"}])

        self.assertIn("OPENAI_API_KEY", str(context.exception))

    def test_embedding_features_use_fake_provider_and_cache(self):
        os.environ["OPENAI_API_KEY"] = "test-secret"
        cache = MemoryEmbeddingCache()
        calls = []

        def fake_embed(inputs, options):
            calls.append((list(inputs), options["embeddingModel"], options["embeddingDimensions"]))
            return [[float(len(text)), 1.0] for text in inputs]

        resolved = processor.resolve_processor_options(
            {
                "cluster": {
                    "textFeatureMethod": "embedding",
                    "embeddingModel": "text-embedding-3-small",
                    "embeddingDimensions": 2,
                }
            }
        )

        first = processor.embedding_text_feature_matrix(
            [{"name": "alpha"}, {"name": "beta"}, {"name": "alpha"}],
            ["name"],
            resolved,
            embedding_cache=cache,
            embed_texts=fake_embed,
        )
        second = processor.embedding_text_feature_matrix(
            [{"name": "alpha"}, {"name": "beta"}, {"name": "alpha"}],
            ["name"],
            resolved,
            embedding_cache=cache,
            embed_texts=fake_embed,
        )

        self.assertEqual(first.shape, (3, 2))
        self.assertEqual(second.shape, (3, 2))
        self.assertEqual(len(calls), 1)
        self.assertEqual(cache.set_calls, 1)
        self.assertEqual(len(cache.values), 2)

    def test_processing_uses_embedding_backend_with_fake_provider(self):
        os.environ["OPENAI_API_KEY"] = "test-secret"
        original_reduce = processor.reduce_to_points
        original_cluster = processor.cluster_points
        try:
            processor.reduce_to_points = lambda features: np.asarray(
                [[0.0, 0.0], [1.0, 1.0], [2.0, 2.0]]
            )
            processor.cluster_points = lambda points, row_count, cluster_config: np.asarray(
                [0, 0, 1]
            )
            fake_embed = lambda inputs, options: [
                [float(index), float(index + 1)] for index, _ in enumerate(inputs)
            ]
            metadata = {}
            rows = processor.process_records(
                {
                    "config": {
                        "dataSchema": {"name": "String", "kind": "String"},
                        "groupingFields": ["kind"],
                        "cluster": {
                            "textFeatureMethod": "embedding",
                            "featureFields": ["name"],
                        },
                    }
                },
                [
                    {"name": "alpha", "kind": "a"},
                    {"name": "beta", "kind": "a"},
                    {"name": "gamma", "kind": "b"},
                ],
                embedding_cache=MemoryEmbeddingCache(),
                embed_texts=fake_embed,
                metadata=metadata,
            )
        finally:
            processor.reduce_to_points = original_reduce
            processor.cluster_points = original_cluster

        self.assertEqual([row["clusterId"] for row in rows], [0, 0, 1])
        self.assertEqual(metadata["textFeatureMethod"], "embedding")

    def test_empty_artifact_metadata_includes_processor_backend(self):
        metadata = {}
        rows = processor.process_records(
            {
                "config": {
                    "dataSchema": {"name": "String", "kind": "String"},
                    "groupingFields": ["kind"],
                    "cluster": {},
                }
            },
            [],
            metadata=metadata,
        )

        self.assertEqual(rows, [])
        self.assertEqual(metadata["textFeatureMethod"], "tfidf")
        self.assertEqual(metadata["recordCount"], 0)


if __name__ == "__main__":
    unittest.main()
