#!/usr/bin/env python3
import argparse
import secrets
from pathlib import Path


DEFAULT_ENV = Path(__file__).resolve().parents[1] / ".env"
EXAMPLE_ENV = Path(__file__).resolve().parents[1] / ".env.example"


def env_lines(path):
    if not path.exists():
        return []
    return path.read_text().splitlines()


def upsert_env_value(lines, key, value):
    prefix = f"{key}="
    updated = False
    output = []
    for line in lines:
        if line.startswith(prefix):
            output.append(f"{key}={value}")
            updated = True
        else:
            output.append(line)
    if not updated:
        output.append(f"{key}={value}")
    return output


def main():
    parser = argparse.ArgumentParser(description="Create or rotate DATA_GRAPH_API_TOKEN in .env.")
    parser.add_argument("--env", type=Path, default=DEFAULT_ENV)
    parser.add_argument("--print-token", action="store_true")
    args = parser.parse_args()

    if args.env.exists():
        lines = env_lines(args.env)
    elif EXAMPLE_ENV.exists():
        lines = env_lines(EXAMPLE_ENV)
    else:
        lines = []

    token = secrets.token_hex(32)
    args.env.parent.mkdir(parents=True, exist_ok=True)
    args.env.write_text("\n".join(upsert_env_value(lines, "DATA_GRAPH_API_TOKEN", token)) + "\n")
    try:
        args.env.chmod(0o600)
    except PermissionError:
        pass

    print(f"Updated {args.env}")
    if args.print_token:
        print(token)


if __name__ == "__main__":
    main()
