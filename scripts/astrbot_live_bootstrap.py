"""Start the real AstrBot host with secrets kept outside model configuration.

The wrapper is copied to the AstrBot installation root by the deployment helper
and can also be run directly from that root. Existing environment variables win
over the JSON file so operators can override a key without editing files.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


ENV_BY_PROVIDER = {
    "opencode": "ASTRMAI_OPENCODE_API_KEY",
    "qwen": "ASTRMAI_QWEN_API_KEY",
    "openai_embedding": "ASTRMAI_OPENAI_EMBEDDING_API_KEY",
}


def _load_secrets(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    return payload if isinstance(payload, dict) else {}


def _configure_environment(host_root: Path) -> None:
    secrets_path = Path(
        os.getenv(
            "ASTRMAI_LIVE_SECRETS_FILE",
            str(host_root / "data" / "config" / "astrmai_live_secrets.json"),
        )
    )
    payload = _load_secrets(secrets_path)
    providers = payload.get("providers", {})
    if isinstance(providers, dict):
        for provider_id, env_name in ENV_BY_PROVIDER.items():
            entry = providers.get(provider_id)
            value = entry.get("api_key") if isinstance(entry, dict) else entry
            if isinstance(value, str) and value.strip() and not value.startswith("<"):
                os.environ.setdefault(env_name, value.strip())

    host_key = payload.get("host_api_key")
    if isinstance(host_key, str) and host_key.strip() and not host_key.startswith("<"):
        os.environ.setdefault("ASTRMAI_HOST_API_KEY", host_key.strip())

    # AstrBot's Loguru console sink otherwise inherits the Windows GBK codepage.
    os.environ.setdefault("PYTHONUTF8", "1")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")


def main() -> None:
    host_root = Path(__file__).resolve().parent.parent
    os.chdir(host_root)
    if str(host_root) not in sys.path:
        sys.path.insert(0, str(host_root))
    _configure_environment(host_root)
    # Execute main.py as the real entrypoint so Python sets sys.path[0] to the
    # Host root. This keeps root-level imports such as runtime_bootstrap stable
    # on mapped drives and when launched from a batch wrapper.
    main_path = host_root / "main.py"
    os.execv(
        sys.executable,
        [sys.executable, "-X", "utf8", str(main_path), *sys.argv[1:]],
    )


if __name__ == "__main__":
    main()
