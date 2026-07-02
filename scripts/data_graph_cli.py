import json
import os
import urllib.error
import urllib.parse
import urllib.request


def load_env(path):
    if not path or not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def validated_base_url(value):
    base_url = value.rstrip("/")
    parsed = urllib.parse.urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise SystemExit("--base-url must be an http:// or https:// URL.")
    return base_url


def get_json(url, token):
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        # URL comes from --base-url after validated_base_url restricts it to HTTP(S).
        with urllib.request.urlopen(  # nosec B310
            request,
            timeout=60,
        ) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise SystemExit(f"GET {url} failed: {error.code} {detail}") from error
    except urllib.error.URLError as error:
        raise SystemExit(f"GET {url} failed: {error.reason}") from error
    except json.JSONDecodeError as error:
        raise SystemExit(f"GET {url} returned invalid JSON.") from error
