#!/usr/bin/env python3
import argparse
import csv
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path


def load_env(path):
    if not path or not path.exists():
        return
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def load_rows(path, data_format, schema=None):
    data_format = data_format or "auto"
    if data_format == "auto":
        suffix = path.suffix.lower()
        if suffix == ".jsonl":
            data_format = "jsonl"
        elif suffix == ".csv":
            data_format = "csv"
        else:
            data_format = "json"

    if data_format == "json":
        payload = json.loads(path.read_text())
        if isinstance(payload, dict) and isinstance(payload.get("data"), list):
            return payload["data"]
        if isinstance(payload, list):
            return payload
        raise SystemExit("JSON input must be an array or an object with a data array.")

    if data_format == "jsonl":
        rows = []
        for line in path.read_text().splitlines():
            if line.strip():
                rows.append(json.loads(line))
        return rows

    if data_format == "csv":
        with path.open(newline="") as handle:
            rows = list(csv.DictReader(handle))
        if schema:
            return [coerce_row(row, schema) for row in rows]
        return rows

    raise SystemExit(f"Unsupported format: {data_format}")


def coerce_row(row, schema):
    output = dict(row)
    for field, field_type in schema.items():
        if field in row and row[field] is not None:
            output[field] = coerce_value(row[field], field_type)
    return output


def coerce_value(value, field_type):
    if field_type == "String":
        return value
    if field_type == "Number":
        text = str(value).strip()
        if not text:
            raise SystemExit("CSV Number fields cannot be empty.")
        try:
            number = float(text)
        except ValueError as error:
            raise SystemExit(f"CSV value {value!r} is not a valid Number.") from error
        return int(number) if number.is_integer() else number
    if field_type == "Boolean":
        text = str(value).strip().lower()
        if text in {"true", "1", "yes", "y"}:
            return True
        if text in {"false", "0", "no", "n"}:
            return False
        raise SystemExit(f"CSV value {value!r} is not a valid Boolean.")
    if field_type in {"Object", "Array"}:
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as error:
            raise SystemExit(f"CSV value for {field_type} must be JSON.") from error
        if field_type == "Object" and not isinstance(parsed, dict):
            raise SystemExit("CSV Object values must decode to JSON objects.")
        if field_type == "Array" and not isinstance(parsed, list):
            raise SystemExit("CSV Array values must decode to JSON arrays.")
        return parsed
    return value


def get_json(url, token):
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")
        raise SystemExit(f"GET {url} failed: {error.code} {detail}") from error


def request_json(method, url, token, payload):
    body = json.dumps(payload).encode()
    request = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")
        raise SystemExit(f"{method} {url} failed: {error.code} {detail}") from error


def main():
    parser = argparse.ArgumentParser(description="Import JSON, JSONL, or CSV rows into Data Graph.")
    parser.add_argument("--base-url", default=os.environ.get("DATA_GRAPH_BASE_URL", "http://127.0.0.1:8080"))
    parser.add_argument("--env", type=Path, default=Path(".env"))
    parser.add_argument("--token", default=None)
    parser.add_argument("--graph-id", default=None)
    parser.add_argument("--config", type=Path, help="Config JSON file. Required when creating a new graph.")
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--format", choices=["auto", "json", "jsonl", "csv"], default="auto")
    args = parser.parse_args()

    load_env(args.env)
    token = args.token or os.environ.get("DATA_GRAPH_API_TOKEN")
    if not token:
        raise SystemExit("Provide --token or set DATA_GRAPH_API_TOKEN in .env.")

    base_url = args.base_url.rstrip("/")
    if args.graph_id:
        schema = None
        if args.format == "csv" or (args.format == "auto" and args.data.suffix.lower() == ".csv"):
            graph = get_json(f"{base_url}/api/data-graph/{args.graph_id}", token)
            schema = graph.get("config", {}).get("dataSchema")
        rows = load_rows(args.data, args.format, schema=schema)
        result = request_json("POST", f"{base_url}/api/data-graph/{args.graph_id}/data", token, {"data": rows})
    else:
        if not args.config:
            raise SystemExit("--config is required when --graph-id is not provided.")
        config = json.loads(args.config.read_text())
        if "config" in config:
            config = config["config"]
        rows = load_rows(args.data, args.format, schema=config.get("dataSchema"))
        result = request_json("POST", f"{base_url}/api/data-graph", token, {"config": config, "data": rows})

    json.dump(result, sys.stdout, indent=2)
    print()


if __name__ == "__main__":
    main()
