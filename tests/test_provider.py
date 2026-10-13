"""Verify Cloudflare routing, media submission, and failure classification."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from lwe.decisions import (
    DecisionError,
    DecisionRequest,
    DecisionService,
    DecisionSettings,
    Noul,
)
from lwe_plugin_decision_cloudflare import CloudflareDecisionProvider
from lwe_plugin_decision_cloudflare import MAX_IMAGE_BYTES


@pytest.mark.parametrize(
    "model", ["clef", "clef-flash", "clef-omni", "@cf/cloudflare/clef-omni"]
)
def test_cloudflare_wire_contract_and_zero_probability(
    monkeypatch: pytest.MonkeyPatch,
    model: str,
) -> None:
    """Keep embedded media and structured state intact and unwrap Workers AI."""
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "account123")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "secret")
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            headers={"cf-ray": "request123"},
            json={
                "success": True,
                "result": {
                    "model": "clef-omni",
                    "answers": {"urgent": {"type": "noul", "noul": 0}},
                    "usage": {"input_tokens": 50},
                },
            },
        )

    provider = CloudflareDecisionProvider(httpx.MockTransport(respond))
    settings = DecisionSettings(provider="cloudflare", model=model)
    request = DecisionRequest(
        state={"ticket": 42},
        questions={"urgent": Noul(instructions="Urgent?")},
        images=["data:image/png;base64,eA=="],
    )
    result = DecisionService(settings, provider).decide(request)
    assert result.model_dump()["answers"]["urgent"]["noul"] == 0
    assert result.request_id == "request123"
    assert result.usage == {"input_tokens": 50}
    sent = requests[0]
    selector = model.removeprefix("@cf/cloudflare/")
    assert str(sent.url).endswith(
        f"/accounts/account123/ai/run/@cf/cloudflare/{selector}"
    )
    assert sent.headers["Authorization"] == "Bearer secret"
    expected = request.model_dump(exclude_none=True, exclude={"audio", "videos"})
    assert json.loads(sent.content) == {**expected, "model": selector}


@pytest.mark.parametrize(
    "status,body,retryable",
    [
        (401, {"errors": ["secret"]}, False),
        (413, {}, False),
        (429, {}, True),
        (503, {}, True),
        (200, {"success": False, "errors": ["secret"]}, False),
        (200, {"success": True, "result": None}, False),
    ],
)
def test_cloudflare_failure_classification(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    body: dict[str, object],
    retryable: bool,
) -> None:
    """Application errors and authentication failures must not look like valid decisions."""
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "account123")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "secret")
    provider = CloudflareDecisionProvider(
        httpx.MockTransport(lambda request: httpx.Response(status, json=body))
    )
    with pytest.raises(DecisionError) as failed:
        provider.invoke(
            DecisionRequest(state="test", questions={"q": Noul(instructions="Q?")}),
            DecisionSettings(provider="cloudflare", model="clef-omni"),
        )
    assert failed.value.retryable is retryable
    assert "secret" not in str(failed.value)


def test_cloudflare_policy_checks_model_capabilities_and_file_size(
    tmp_path: Path,
) -> None:
    """Reject unsupported modalities and oversized inputs before downloading or invoking."""
    provider = CloudflareDecisionProvider()
    dense = DecisionSettings(provider="cloudflare", model="clef-flash")
    omni = DecisionSettings(provider="cloudflare", model="clef-omni")
    request = DecisionRequest(state="test", questions={"q": Noul(instructions="Q?")})
    audio = request.model_copy(update={"audio": ["https://media.example/call.wav"]})
    provider.validate(audio, omni)
    with pytest.raises(ValueError, match="does not support audio"):
        provider.validate(audio, dense)
    with pytest.raises(ValueError, match="Too many images"):
        provider.validate(
            request.model_copy(
                update={"images": ["https://media.example/image.png"] * 5}
            ),
            omni,
        )
    oversized = tmp_path / "oversized.png"
    with oversized.open("wb") as stream:
        stream.truncate(MAX_IMAGE_BYTES + 1)
    with pytest.raises(ValueError, match="decoded byte limit"):
        provider.prepare(
            request.model_copy(update={"images": [oversized.as_uri()]}), omni
        )
