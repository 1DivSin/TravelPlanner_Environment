"""Safely validate a Penguin gateway key using model-list metadata only."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen


DEFAULT_BASE_URL = "https://penguinapi.org"
MAIN_MODEL = "claude-opus-5"
HAIKU_MODEL = "claude-haiku-4-5"


def load_key(path: str | Path) -> str:
    """Read one key line without retaining surrounding file whitespace."""
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    if len(lines) != 1 or len(lines[0]) < 32 or any(character.isspace() for character in lines[0]):
        raise ValueError("key file must contain one whitespace-free line of at least 32 characters")
    return lines[0]


def select_models(model_ids: list[str]) -> tuple[str, str]:
    available = set(model_ids)
    if HAIKU_MODEL not in available:
        raise ValueError(f"required model is unavailable: {HAIKU_MODEL}")
    if MAIN_MODEL not in available:
        raise ValueError(f"required model is unavailable: {MAIN_MODEL}")
    return MAIN_MODEL, HAIKU_MODEL


def _model_ids(request: Request) -> list[str]:
    with urlopen(request, timeout=15) as response:
        payload = json.loads(response.read())
    return [model["id"] for model in payload["data"] if isinstance(model.get("id"), str)]


def probe_gateway(base_url: str, key: str) -> tuple[list[str], str]:
    """Fetch model metadata, falling back to Anthropic's x-api-key header on 401."""
    url = f"{base_url.rstrip('/')}/v1/models"
    common_headers = {
        "Accept": "application/json",
        "Anthropic-Version": "2023-06-01",
        "User-Agent": "travelplanner-gateway-preflight/1.0",
    }
    bearer_request = Request(
        url,
        headers={**common_headers, "Authorization": f"Bearer {key}"},
        method="GET",
    )
    try:
        return _model_ids(bearer_request), "bearer"
    except HTTPError as error:
        if error.code != 401:
            raise

    api_key_request = Request(
        url,
        headers={**common_headers, "X-Api-Key": key},
        method="GET",
    )
    return _model_ids(api_key_request), "x-api-key"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate Penguin gateway model metadata")
    parser.add_argument("--key-file", required=True)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)

    base_url = args.base_url.rstrip("/")
    model_ids, auth_mode = probe_gateway(base_url, load_key(args.key_file))
    main_model, haiku_model = select_models(model_ids)
    result = {
        "base_url": base_url,
        "auth_mode": auth_mode,
        "main_model": main_model,
        "haiku_model": haiku_model,
    }
    Path(args.output).write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(
        f"Gateway preflight passed: base_url={base_url}, auth_mode={auth_mode}, "
        f"main_model={main_model}, haiku_model={haiku_model}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
