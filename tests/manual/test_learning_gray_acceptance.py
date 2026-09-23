"""Read-only local acceptance entry point for Stage 10.

The script consumes a release manifest and environment diagnostics only.  It
does not connect to a Provider, server, production database, or user traffic.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


FORBIDDEN = ("api_key", "cookie", "authorization", "password", "raw_message", "full_message")


def validate_manifest(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("release manifest must be an object")
    for key in FORBIDDEN:
        if key in json.dumps(payload, sort_keys=True).lower():
            raise ValueError(f"manifest contains forbidden sensitive field: {key}")
    required = {"release_id", "manifest_sha256", "flags", "cohort_id"}
    missing = sorted(required - set(payload))
    if missing:
        raise ValueError(f"manifest missing fields: {', '.join(missing)}")
    flags = payload["flags"]
    if not isinstance(flags, dict) or flags.get("learning_injection_enabled") is not False:
        raise ValueError("local acceptance requires injection disabled")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args()
    payload = validate_manifest(args.manifest)
    print(json.dumps({
        "stage": "10-gray-release",
        "mode": "local_shadow_or_preflight",
        "release_id": payload["release_id"],
        "automatic_injection_authorized": False,
        "production_provider_authorized": False,
        "production_database_write_authorized": False,
        "production_cursor_or_replay_authorized": False,
        "server_or_container_authorized": False,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
