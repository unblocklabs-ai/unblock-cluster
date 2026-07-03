from __future__ import annotations

import time
from secrets import token_bytes

CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def new_id(prefix: str) -> str:
    timestamp_ms = int(time.time() * 1000)
    value = (timestamp_ms << 80) | int.from_bytes(token_bytes(10), "big")
    chars = []
    for _ in range(26):
        chars.append(CROCKFORD[value & 31])
        value >>= 5
    return f"{prefix}_{''.join(reversed(chars))}"
