# Operations Notes

## Local Automation

When driving the API from local scripts, write multiprocessing workflows to
real `.py` files instead of piping them through stdin or heredocs. On macOS,
process-pool workers cannot re-import the `__main__` module from `<stdin>`,
which breaks cluster/layout runs. Tests that intentionally need in-process CPU
execution can use the `inline_cpu_runs=True` settings seam; agents and the
reviewer have both hit this stdin/process-pool footgun.

## Scale Benchmark

```sh
.venv/bin/python scripts/bench_scale.py \
  --size 100000 \
  --data-dir output/bench-scale \
  --json-out output/bench-scale/metrics.json
```

The benchmark is offline and deterministic by default. It deletes and
recreates the `--data-dir` on every run (do not point it at real data), uploads
records through the API in 1000-record batches, runs mock 1536-dimensional
embeddings, cluster, layout, scripted labels, and trends, then reports stage
timings, cluster/layout phase durations, artifact request times (cold =
composed, warm = served from cache), gzip wire bytes, evidence latency, peak
RSS, and final DB size.
Pass `--no-seed` only when intentionally exploring non-reproducible variation.
