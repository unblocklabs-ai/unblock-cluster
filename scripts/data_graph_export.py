#!/usr/bin/env python3
import argparse
import json
import os
import sys
from pathlib import Path

from data_graph_cli import get_json, load_env, validated_base_url


def main():
    parser = argparse.ArgumentParser(description="Export Data Graph config, latest artifact, or bundle JSON.")
    parser.add_argument("graph_id")
    parser.add_argument("--base-url", default=os.environ.get("DATA_GRAPH_BASE_URL", "http://127.0.0.1:8080"))
    parser.add_argument("--env", type=Path, default=Path(".env"))
    parser.add_argument("--token", default=None)
    parser.add_argument("--kind", choices=["config", "artifact", "bundle"], default="bundle")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    load_env(args.env)
    token = args.token or os.environ.get("DATA_GRAPH_API_TOKEN")
    if not token:
        raise SystemExit("Provide --token or set DATA_GRAPH_API_TOKEN in .env.")

    base_url = validated_base_url(args.base_url)
    graph = get_json(f"{base_url}/api/data-graph/{args.graph_id}", token)
    artifact = None
    if args.kind in {"artifact", "bundle"}:
        artifact = get_json(f"{base_url}/api/data-graph/{args.graph_id}/artifact/latest", token)

    if args.kind == "config":
        payload = graph["config"]
    elif args.kind == "artifact":
        payload = artifact
    else:
        payload = {"graph": graph, "artifact": artifact}

    text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
        print(f"Wrote {args.output}")
    else:
        sys.stdout.write(text)


if __name__ == "__main__":
    main()
