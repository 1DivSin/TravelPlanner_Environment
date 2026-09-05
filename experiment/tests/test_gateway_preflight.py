import json
from urllib.error import HTTPError

import pytest

import gateway_preflight


def test_load_key_accepts_one_nonempty_line_and_rejects_invalid_input(tmp_path):
    key_file = tmp_path / "gateway.key"
    key = "k" * 32
    key_file.write_text(f"{key}\n", encoding="utf-8")
    assert gateway_preflight.load_key(key_file) == key

    for invalid in ("", "k" * 31, f"{'k' * 32} {'x'}", f"{'k' * 32}\n{'x' * 32}\n"):
        key_file.write_text(invalid, encoding="utf-8")
        with pytest.raises(ValueError):
            gateway_preflight.load_key(key_file)


def test_select_models_requires_opus_5_and_haiku():
    haiku = "claude-haiku-4-5"
    opus = "claude-opus-5"

    assert gateway_preflight.select_models(
        [opus, "claude-opus-4-8", "claude-opus-4-8[1m]", haiku]
    ) == (opus, haiku)

    with pytest.raises(ValueError, match="claude-opus-5"):
        gateway_preflight.select_models(["claude-opus-4-8[1m]", "claude-opus-4-8", haiku])

    with pytest.raises(ValueError, match="claude-haiku-4-5"):
        gateway_preflight.select_models([opus])


def test_probe_gateway_falls_back_to_x_api_key_after_bearer_401(monkeypatch):
    calls = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(
                {"data": [{"id": "claude-opus-4-8[1m]"}, {"id": "claude-haiku-4-5"}]}
            ).encode()

    def fake_urlopen(request, timeout):
        calls.append((request.full_url, {name.lower(): value for name, value in request.header_items()}, timeout))
        if len(calls) == 1:
            raise HTTPError(request.full_url, 401, "unauthorized", {}, None)
        return Response()

    monkeypatch.setattr(gateway_preflight, "urlopen", fake_urlopen)
    key = "s" * 32
    models, auth_mode = gateway_preflight.probe_gateway("https://penguinapi.org/", key)

    assert models == ["claude-opus-4-8[1m]", "claude-haiku-4-5"]
    assert auth_mode == "x-api-key"
    assert [call[0] for call in calls] == ["https://penguinapi.org/v1/models"] * 2
    assert calls[0][1]["authorization"] == f"Bearer {key}"
    assert "x-api-key" not in calls[0][1]
    assert calls[1][1]["x-api-key"] == key
    assert "authorization" not in calls[1][1]
    for _, headers, _ in calls:
        assert headers["accept"] == "application/json"
        assert headers["anthropic-version"] == "2023-06-01"
        assert headers["user-agent"]


def test_cli_writes_only_safe_metadata_and_prints_no_secret(monkeypatch, tmp_path, capsys):
    key = "p" * 32
    key_file = tmp_path / "gateway.key"
    output = tmp_path / "preflight.json"
    key_file.write_text(key, encoding="utf-8")

    def fake_probe(base_url, loaded_key):
        assert base_url == "https://penguinapi.org"
        assert loaded_key == key
        return ["claude-opus-5", "claude-haiku-4-5"], "bearer"

    monkeypatch.setattr(gateway_preflight, "probe_gateway", fake_probe)
    assert gateway_preflight.main(["--key-file", str(key_file), "--output", str(output)]) == 0

    output_text = output.read_text(encoding="utf-8")
    assert json.loads(output_text) == {
        "base_url": "https://penguinapi.org",
        "auth_mode": "bearer",
        "main_model": "claude-opus-5",
        "haiku_model": "claude-haiku-4-5",
    }
    assert key not in output_text
    assert key not in capsys.readouterr().out
