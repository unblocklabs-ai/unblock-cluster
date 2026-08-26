from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from pathlib import Path

import numpy as np
import pytest
import sqlite_vec

from datagraph import memory_analysis


def _open(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True)
    conn.load_extension(sqlite_vec.loadable_path())
    conn.enable_load_extension(False)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _create_fixture(
    db_path: Path,
    *,
    vector_count: int = 40,
    aliases: int = 2,
    include_stale_at: bool = True,
) -> None:
    stale_at_column = ", stale_at TEXT" if include_stale_at else ""
    with _open(db_path) as conn:
        conn.executescript(
            f"""
            CREATE TABLE content (
              hash TEXT PRIMARY KEY,
              doc TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE TABLE documents (
              id INTEGER PRIMARY KEY,
              collection TEXT NOT NULL,
              path TEXT NOT NULL,
              title TEXT NOT NULL,
              hash TEXT NOT NULL,
              created_at TEXT NOT NULL,
              modified_at TEXT NOT NULL,
              active INTEGER NOT NULL DEFAULT 1,
              UNIQUE(collection, path),
              FOREIGN KEY(hash) REFERENCES content(hash)
            );
            CREATE TABLE content_vectors (
              hash TEXT NOT NULL,
              seq INTEGER NOT NULL,
              pos INTEGER NOT NULL,
              chunk_len INTEGER NOT NULL,
              model TEXT NOT NULL,
              embed_fingerprint TEXT NOT NULL,
              total_chunks INTEGER NOT NULL,
              embedded_at TEXT NOT NULL,
              PRIMARY KEY(hash, seq)
            );
            CREATE TABLE memory_analysis_runs (
              id TEXT PRIMARY KEY,
              created_at TEXT NOT NULL,
              completed_at TEXT NOT NULL,
              input_digest TEXT NOT NULL,
              model TEXT NOT NULL,
              embedding_fingerprint TEXT NOT NULL,
              dimensions INTEGER NOT NULL,
              params_json TEXT NOT NULL,
              future_metadata TEXT
              {stale_at_column}
            );
            CREATE TABLE memory_analysis_clusters (
              run_id TEXT NOT NULL,
              cluster_id INTEGER NOT NULL,
              size INTEGER NOT NULL,
              mean_probability REAL NOT NULL,
              PRIMARY KEY(run_id, cluster_id),
              FOREIGN KEY(run_id) REFERENCES memory_analysis_runs(id) ON DELETE CASCADE
            );
            CREATE TABLE memory_analysis_memberships (
              run_id TEXT NOT NULL,
              hash TEXT NOT NULL,
              seq INTEGER NOT NULL,
              cluster_id INTEGER NOT NULL,
              probability REAL NOT NULL,
              outlier_score REAL NOT NULL,
              x REAL NOT NULL,
              y REAL NOT NULL,
              representative_rank INTEGER,
              PRIMARY KEY(run_id, hash, seq),
              FOREIGN KEY(run_id) REFERENCES memory_analysis_runs(id) ON DELETE CASCADE
            );
            CREATE TABLE memory_analysis_duplicate_occurrences (
              run_id TEXT NOT NULL,
              content_fingerprint TEXT NOT NULL,
              canonical_hash TEXT NOT NULL,
              canonical_seq INTEGER NOT NULL,
              duplicate_hash TEXT NOT NULL,
              duplicate_seq INTEGER NOT NULL,
              PRIMARY KEY(run_id, duplicate_hash, duplicate_seq),
              FOREIGN KEY(run_id) REFERENCES memory_analysis_runs(id) ON DELETE CASCADE
            );
            CREATE VIRTUAL TABLE vectors_vec USING vec0(
              hash_seq TEXT PRIMARY KEY,
              embedding float[32] distance_metric=cosine
            );
            """
        )
        rng = np.random.default_rng(42)
        for index in range(vector_count + 1):
            hash_ = f"hash-{index:03d}"
            active = int(index < vector_count)
            content = f"memory {index}"
            vector = np.zeros(32, dtype=np.float32)
            vector[index % 2] = 1
            vector += rng.normal(0, 0.01, 32).astype(np.float32)
            conn.execute(
                "INSERT INTO content VALUES (?, ?, ?)",
                (hash_, content, "2026-08-24T00:00:00Z"),
            )
            for alias in range(aliases if active else 1):
                conn.execute(
                    """
                    INSERT INTO documents
                      (collection, path, title, hash, created_at, modified_at, active)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "memory",
                        f"memory-{index}-alias-{alias}.md",
                        f"Memory {index}",
                        hash_,
                        "2026-08-24T00:00:00Z",
                        "2026-08-24T00:00:00Z",
                        active,
                    ),
                )
            conn.execute(
                """
                INSERT INTO content_vectors
                  (hash, seq, pos, chunk_len, model, embed_fingerprint,
                   total_chunks, embedded_at)
                VALUES (?, 0, 0, ?, 'test-model', 'test-fingerprint', 1, ?)
                """,
                (hash_, len(content), "2026-08-24T00:00:00Z"),
            )
            conn.execute(
                "INSERT INTO vectors_vec(hash_seq, embedding) VALUES (?, ?)",
                (f"{hash_}_0", sqlite_vec.serialize_float32(vector.tolist())),
            )
        conn.commit()


def _vector_snapshot(conn: sqlite3.Connection) -> list[tuple[str, bytes]]:
    return [
        (row["hash_seq"], bytes(row["embedding"]))
        for row in conn.execute(
            "SELECT hash_seq, embedding FROM vectors_vec ORDER BY hash_seq"
        )
    ]


def _insert_stale_run(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        INSERT INTO memory_analysis_runs (
          id, created_at, completed_at, input_digest, model,
          embedding_fingerprint, dimensions, params_json, stale_at
        ) VALUES (
          'previous-run', '2026-08-24T00:00:00Z', '2026-08-24T00:01:00Z',
          'previous-digest', 'test-model', 'test-fingerprint', 32, '{}',
          '2026-08-24T00:02:00Z'
        )
        """
    )


def _add_exact_duplicate_chunks(conn: sqlite3.Connection) -> None:
    repeated = "repeat 🙂"
    conn.execute(
        "UPDATE content SET doc = ? WHERE hash = 'hash-000'",
        (f"{repeated}\n{repeated}",),
    )
    conn.execute("UPDATE content SET doc = ? WHERE hash = 'hash-001'", (repeated,))
    conn.execute(
        """
        UPDATE content_vectors
        SET chunk_len = 9, total_chunks = 2
        WHERE hash = 'hash-000'
        """
    )
    conn.execute(
        "UPDATE content_vectors SET chunk_len = 9 WHERE hash = 'hash-001'"
    )
    vector = bytes(
        conn.execute(
            "SELECT embedding FROM vectors_vec WHERE hash_seq = 'hash-000_0'"
        ).fetchone()[0]
    )
    conn.execute(
        """
        INSERT INTO content_vectors (
          hash, seq, pos, chunk_len, model, embed_fingerprint,
          total_chunks, embedded_at
        ) VALUES (
          'hash-000', 1, 10, 9, 'test-model', 'test-fingerprint', 2,
          '2026-08-24T00:00:00Z'
        )
        """
    )
    conn.execute(
        "INSERT INTO vectors_vec(hash_seq, embedding) VALUES ('hash-000_1', ?)",
        (vector,),
    )


def _assert_previous_stale_run(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "SELECT id, stale_at FROM memory_analysis_runs"
    ).fetchone()
    assert tuple(row) == ("previous-run", "2026-08-24T00:02:00Z")


def test_active_hash_without_content_vectors_is_rejected_before_analysis(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "qmd.sqlite"
    _create_fixture(db_path, vector_count=6)
    with _open(db_path) as conn:
        _insert_stale_run(conn)
        conn.execute("DELETE FROM content_vectors WHERE hash = 'hash-000'")
        conn.commit()

    result = _run_worker(db_path, tmp_path)

    assert result.returncode == 1
    assert "hash-000 has no embedded chunks" in result.stderr
    with _open(db_path) as conn:
        _assert_previous_stale_run(conn)


def test_incomplete_content_vector_sequence_is_rejected_before_analysis(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "qmd.sqlite"
    _create_fixture(db_path, vector_count=6)
    with _open(db_path) as conn:
        _insert_stale_run(conn)
        conn.execute(
            "UPDATE content_vectors SET total_chunks = 2 WHERE hash = 'hash-000'"
        )
        conn.commit()

    result = _run_worker(db_path, tmp_path)

    assert result.returncode == 1
    assert "hash-000 has incomplete chunk sequences" in result.stderr
    with _open(db_path) as conn:
        _assert_previous_stale_run(conn)


def test_missing_vectors_vec_embedding_is_rejected_before_analysis(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "qmd.sqlite"
    _create_fixture(db_path, vector_count=6)
    with _open(db_path) as conn:
        _insert_stale_run(conn)
        conn.execute("DELETE FROM vectors_vec WHERE hash_seq = 'hash-000_0'")
        conn.commit()

    result = _run_worker(db_path, tmp_path)

    assert result.returncode == 1
    assert "hash-000:0 has no vectors_vec embedding" in result.stderr
    with _open(db_path) as conn:
        _assert_previous_stale_run(conn)


def test_population_deduplicates_exact_chunks_and_tracks_each_occurrence(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "qmd.sqlite"
    _create_fixture(db_path, vector_count=6)
    with _open(db_path) as conn:
        _add_exact_duplicate_chunks(conn)
        conn.commit()

        population = memory_analysis._load_population(conn)

        assert population.keys == [
            ("hash-000", 0),
            ("hash-002", 0),
            ("hash-003", 0),
            ("hash-004", 0),
            ("hash-005", 0),
        ]
        assert [
            (duplicate.canonical_key, duplicate.duplicate_key)
            for duplicate in population.duplicate_occurrences
        ] == [
            (("hash-000", 0), ("hash-000", 1)),
            (("hash-000", 0), ("hash-001", 0)),
        ]
        assert len({
            duplicate.content_fingerprint
            for duplicate in population.duplicate_occurrences
        }) == 1

        first_digest = population.digest
        conn.execute("UPDATE documents SET active = 0 WHERE hash = 'hash-001'")
        conn.commit()
        changed = memory_analysis._load_population(conn)

    assert changed.digest != first_digest
    assert [
        duplicate.duplicate_key for duplicate in changed.duplicate_occurrences
    ] == [("hash-000", 1)]


def test_population_includes_only_allowed_collections_before_validation_and_deduplication(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "qmd.sqlite"
    _create_fixture(db_path, vector_count=8)
    with _open(db_path) as conn:
        conn.execute(
            "UPDATE documents SET collection = 'skills' WHERE hash IN ('hash-000', 'hash-001')"
        )
        conn.execute("DELETE FROM content_vectors WHERE hash = 'hash-000'")
        conn.execute("UPDATE content SET doc = 'memory 2' WHERE hash = 'hash-001'")
        conn.commit()

        population = memory_analysis._load_population(conn, ("memory",))

    assert population.keys == [
        ("hash-002", 0),
        ("hash-003", 0),
        ("hash-004", 0),
        ("hash-005", 0),
        ("hash-006", 0),
        ("hash-007", 0),
    ]
    assert population.duplicate_occurrences == []


def test_duplicate_occurrences_are_persisted_with_the_latest_run(tmp_path: Path) -> None:
    db_path = tmp_path / "qmd.sqlite"
    _create_fixture(db_path, vector_count=6)
    with _open(db_path) as conn:
        _add_exact_duplicate_chunks(conn)
        _insert_stale_run(conn)
        conn.commit()
        population = memory_analysis._load_population(conn)
        requested, resolved = memory_analysis._parse_config_json(
            '{"space":{"method":"none"}}'
        )
        config = memory_analysis._resolve_config(requested, resolved, len(population.keys))
        analysis = memory_analysis.Analysis(
            labels=np.zeros(len(population.keys), dtype=np.int64),
            probabilities=np.ones(len(population.keys), dtype=np.float32),
            outlier_scores=np.zeros(len(population.keys), dtype=np.float32),
            points=np.zeros((len(population.keys), 2), dtype=np.float32),
            representative_ranks={},
        )

        memory_analysis._persist(
            conn,
            population,
            analysis,
            config,
            "current-run",
            "2026-08-26T00:00:00+00:00",
        )

        occurrences = conn.execute(
            """
            SELECT run_id, canonical_hash, canonical_seq, duplicate_hash, duplicate_seq
            FROM memory_analysis_duplicate_occurrences
            ORDER BY duplicate_hash, duplicate_seq
            """
        ).fetchall()
        assert [tuple(row) for row in occurrences] == [
            ("current-run", "hash-000", 0, "hash-000", 1),
            ("current-run", "hash-000", 0, "hash-001", 0),
        ]
        assert conn.execute(
            "SELECT COUNT(*) FROM memory_analysis_runs WHERE id = 'previous-run'"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM memory_analysis_memberships"
        ).fetchone()[0] == 5


def test_effective_config_reaches_computation_and_representative_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    population_size = 60
    requested, resolved = memory_analysis._parse_config_json(
        json.dumps(
            {
                "space": {"nComponents": 100, "nNeighbors": 200},
                "hdbscan": {"minClusterSize": 20, "minSamples": 12},
                "seed": 99,
            }
        )
    )
    config = memory_analysis._resolve_config(requested, resolved, population_size)
    observed: dict[str, object] = {}

    def fake_clustering_space(matrix: np.ndarray, worker_config: dict[str, object]) -> np.ndarray:
        observed["cluster_config"] = worker_config
        return matrix

    def fake_hdbscan_labels(
        matrix: np.ndarray, worker_config: dict[str, object], population: int
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        observed["hdbscan_config"] = worker_config
        assert population == population_size
        return (
            np.zeros(population, dtype=np.int64),
            np.ones(population, dtype=np.float32),
            np.zeros(population, dtype=np.float32),
        )

    def fake_layout(matrix: np.ndarray, layout_config: dict[str, object]) -> np.ndarray:
        observed["layout_config"] = layout_config
        return np.zeros((len(matrix), 2), dtype=np.float32)

    def fake_representatives(
        member_ids: list[str],
        member_vectors: np.ndarray,
        probabilities: np.ndarray,
        *,
        top_k: int,
    ) -> list[str]:
        observed["representative_top_k"] = top_k
        return member_ids[:top_k]

    monkeypatch.setattr(memory_analysis, "_clustering_space", fake_clustering_space)
    monkeypatch.setattr(memory_analysis, "_hdbscan_labels", fake_hdbscan_labels)
    monkeypatch.setattr(memory_analysis, "_layout", fake_layout)
    monkeypatch.setattr(memory_analysis, "select_representatives", fake_representatives)

    population = memory_analysis.Population(
        keys=[(f"hash-{index}", 0) for index in range(population_size)],
        matrix=np.ones((population_size, 32), dtype=np.float32),
        duplicate_occurrences=[],
        model="model",
        fingerprint="fingerprint",
        dimensions=32,
        digest="digest",
    )
    analysis = memory_analysis._analyze(population, config)

    assert config.effective["space"] == {
        "method": "umap",
        "nComponents": 58,
        "nNeighbors": 59,
        "metric": "cosine",
        "minDist": 0.1,
    }
    assert observed["cluster_config"] is config.effective
    assert observed["hdbscan_config"] is config.effective
    assert observed["layout_config"] == {
        "method": "umap",
        "nNeighbors": 30,
        "minDist": 0.1,
        "seed": 99,
    }
    assert observed["representative_top_k"] == 50
    assert len(analysis.representative_ranks) == 50


def _run_worker(
    db_path: Path,
    tmp_path: Path,
    config: dict[str, object] | str | None = None,
    collections: list[str] | str | None = None,
) -> subprocess.CompletedProcess[str]:
    root = Path(__file__).parents[1]
    command = [str(root / "bin/unblock-memory-analysis"), "--db", str(db_path)]
    if config is not None:
        command.extend(
            ["--config-json", config if isinstance(config, str) else json.dumps(config)]
        )
    if collections is not None:
        command.extend(
            [
                "--collections-json",
                collections if isinstance(collections, str) else json.dumps(collections),
            ]
        )
    return subprocess.run(
        command,
        cwd=tmp_path,
        env={**os.environ, "NUMBA_CACHE_DIR": str(tmp_path / "numba")},
        text=True,
        capture_output=True,
        check=False,
        timeout=180,
    )


@pytest.mark.slow
def test_cli_analyzes_active_vectors_in_place_and_atomic_rerun(tmp_path: Path) -> None:
    db_path = tmp_path / "qmd.sqlite"
    _create_fixture(db_path)
    with _open(db_path) as conn:
        before = _vector_snapshot(conn)
        content_vector_count = conn.execute("SELECT COUNT(*) FROM content_vectors").fetchone()[0]

    first = _run_worker(db_path, tmp_path)
    assert first.returncode == 0, first.stderr
    assert first.stdout == ""
    with _open(db_path) as conn:
        first_run = conn.execute("SELECT * FROM memory_analysis_runs").fetchone()
        first_run_id = first_run["id"]
        params = json.loads(first_run["params_json"])
        assert params["requested"] == {}
        assert params["resolved"]["space"] == {
            "method": "umap",
            "nComponents": 25,
            "nNeighbors": 15,
            "minDist": 0.1,
        }
        assert params["effective"]["space"]["metric"] == "cosine"
        assert params["effective"]["hdbscan"] == {
            "minClusterSize": 15,
            "minSamples": 10,
            "clusterSelectionMethod": "eom",
            "clusterSelectionEpsilon": 0.0,
            "allowSingleCluster": False,
        }
        assert first_run["stale_at"] is None

    override = {
        "space": {"method": "none", "nComponents": 12, "nNeighbors": 9, "minDist": 0.3},
        "hdbscan": {
            "minClusterSize": 5,
            "minSamples": 3,
            "clusterSelectionMethod": "leaf",
            "clusterSelectionEpsilon": 0.05,
            "allowSingleCluster": True,
        },
        "seed": 7,
    }
    second = _run_worker(db_path, tmp_path, override, ["memory"])
    assert second.returncode == 0, second.stderr
    assert second.stdout == ""

    with _open(db_path) as conn:
        assert _vector_snapshot(conn) == before
        assert (
            conn.execute("SELECT COUNT(*) FROM content_vectors").fetchone()[0]
            == content_vector_count
        )
        assert conn.execute("SELECT COUNT(*) FROM memory_analysis_runs").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM memory_analysis_memberships").fetchone()[0] == 40
        stored = conn.execute("SELECT * FROM memory_analysis_runs").fetchone()
        stored_run = stored["id"]
        assert stored_run != first_run_id
        stored_params = json.loads(stored["params_json"])
        assert stored_params["collections"] == ["memory"]
        assert stored_params["requested"] == override
        assert stored_params["resolved"] == override
        assert stored_params["effective"] == {
            **override,
            "space": {**override["space"], "metric": "cosine"},
        }
        identities = conn.execute(
            "SELECT hash, seq FROM memory_analysis_memberships ORDER BY hash, seq"
        ).fetchall()
        assert len({(row["hash"], row["seq"]) for row in identities}) == 40
        assert all(row["hash"] != "hash-040" for row in identities)
        columns = {
            row["name"]
            for table in (
                "memory_analysis_runs",
                "memory_analysis_clusters",
                "memory_analysis_memberships",
            )
            for row in conn.execute(f"PRAGMA table_info({table})")
        }
        assert "embedding" not in columns
        assert "doc" not in columns

    failed = _run_worker(db_path, tmp_path, {"hdbscan": {"minClusterSize": 41}})
    assert failed.returncode == 1
    assert "must not exceed population 40" in failed.stderr
    with _open(db_path) as conn:
        assert conn.execute("SELECT id FROM memory_analysis_runs").fetchone()[0] == stored_run


@pytest.mark.parametrize(
    ("config", "message"),
    [
        ("{", "must be valid JSON"),
        ("[]", "must be an object"),
        ('{"unknown":1}', "unknown properties"),
        ('{"space":{"nComponents":true}}', "must be an integer"),
        ('{"space":{"minDist":NaN}}', "must be valid JSON"),
        ('{"hdbscan":{"clusterSelectionEpsilon":1e999}}', "finite number"),
        ('{"hdbscan":{"allowSingleCluster":1}}', "must be a boolean"),
        ('{"seed":-1}', "integer from 0"),
    ],
)
def test_invalid_config_fails_before_opening_database(
    tmp_path: Path, config: str, message: str
) -> None:
    result = _run_worker(tmp_path / "missing.sqlite", tmp_path, config)
    assert result.returncode == 1
    assert result.stdout == ""
    assert message in result.stderr


@pytest.mark.parametrize(
    ("collections", "message"),
    [
        ("{", "must be valid JSON"),
        ("{}", "must be a non-empty array"),
        ("[]", "must be a non-empty array"),
        ('["memory", 1]', "values must be non-empty strings"),
        ('["memory", " "]', "values must be non-empty strings"),
        ('["memory", "memory"]', "values must be unique"),
    ],
)
def test_invalid_collection_allowlist_fails_before_opening_database(
    tmp_path: Path, collections: str, message: str
) -> None:
    result = _run_worker(
        tmp_path / "missing.sqlite", tmp_path, collections=collections
    )
    assert result.returncode == 1
    assert result.stdout == ""
    assert message in result.stderr


def test_current_plugin_schema_is_required(tmp_path: Path) -> None:
    db_path = tmp_path / "qmd.sqlite"
    _create_fixture(db_path, include_stale_at=False)
    result = _run_worker(db_path, tmp_path)
    assert result.returncode == 1
    assert "missing columns: stale_at" in result.stderr


def test_missing_plugin_schema_fails_before_analysis(tmp_path: Path) -> None:
    db_path = tmp_path / "qmd.sqlite"
    sqlite3.connect(db_path).close()
    result = subprocess.run(
        [
            str(Path(__file__).parents[1] / "bin/unblock-memory-analysis"),
            "--db",
            str(db_path),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 1
    assert result.stdout == ""
    assert "has not initialized its analysis schema" in result.stderr
