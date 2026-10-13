"""Call Cloudflare's Clef model family through LWE's decision provider interface."""

from __future__ import annotations

import os
from typing import cast

import httpx
from pydantic import Field

from lwe.decisions import (
    CombinedMediaLimit,
    DecisionError,
    DecisionRequest,
    DecisionSettings,
    MediaLimits,
    MediaLoader,
    MediaPolicy,
)
from lwe.decisions.models import DecisionModel

API_URL_TEMPLATE = "https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/@cf/cloudflare/{model}"
MODEL_NAMESPACE = "@cf/cloudflare/"
MIB = 1024 * 1024
MAX_IMAGES = 4
MAX_IMAGE_BYTES = 4 * MIB
MAX_TOTAL_IMAGE_BYTES = 8 * MIB
MAX_AUDIO_CLIPS = 4
MAX_AUDIO_BYTES = 8 * MIB
MAX_VIDEOS = 2
MAX_VIDEO_BYTES = 16 * MIB
MAX_TOTAL_AUDIO_VIDEO_BYTES = 16 * MIB
IMAGE_MIME_TYPES = frozenset({"image/png", "image/jpeg", "image/webp"})
AUDIO_MIME_TYPES = frozenset({"audio/wav", "audio/mpeg"})
VIDEO_MIME_TYPES = frozenset({"video/mp4", "video/webm"})
IMAGE_LIMITS = MediaLimits(
    IMAGE_MIME_TYPES, MAX_IMAGES, MAX_IMAGE_BYTES, MAX_TOTAL_IMAGE_BYTES
)
DENSE_MEDIA_POLICY = MediaPolicy({"images": IMAGE_LIMITS})
OMNI_MEDIA_POLICY = MediaPolicy(
    {
        "images": IMAGE_LIMITS,
        "audio": MediaLimits(AUDIO_MIME_TYPES, MAX_AUDIO_CLIPS, MAX_AUDIO_BYTES),
        "videos": MediaLimits(VIDEO_MIME_TYPES, MAX_VIDEOS, MAX_VIDEO_BYTES),
    },
    (CombinedMediaLimit(("audio", "videos"), MAX_TOTAL_AUDIO_VIDEO_BYTES),),
)
MODEL_MEDIA_POLICIES = {
    "clef": DENSE_MEDIA_POLICY,
    "clef-flash": DENSE_MEDIA_POLICY,
    "clef-omni": OMNI_MEDIA_POLICY,
}


class CloudflareOptions(DecisionModel):
    """Provider-specific account routing configuration."""

    account_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]+$")


class CloudflareDecisionProvider:
    """Submit embedded media and typed questions to Workers AI.

    :param transport: Optional HTTP transport for embedding or offline tests.
    """

    def __init__(
        self,
        transport: httpx.BaseTransport | None = None,
        media_loader: MediaLoader | None = None,
    ) -> None:
        self.transport = transport
        self.media_loader = media_loader

    @staticmethod
    def _model_name(settings: DecisionSettings) -> str:
        """Accept short selectors or fully qualified Cloudflare model IDs."""
        model = settings.model.strip().removeprefix(MODEL_NAMESPACE)
        if model not in MODEL_MEDIA_POLICIES:
            raise ValueError(
                f"Unsupported Clef model; choose one of: {', '.join(MODEL_MEDIA_POLICIES)}"
            )
        return model

    def validate(self, request: DecisionRequest, settings: DecisionSettings) -> None:
        """Validate model capabilities and embedded media without external I/O."""
        CloudflareOptions.model_validate(settings.options)
        model = self._model_name(settings)
        request.validate_media(MODEL_MEDIA_POLICIES[model])

    def prepare(
        self, request: DecisionRequest, settings: DecisionSettings
    ) -> DecisionRequest:
        """Resolve media once with the selected model's byte and modality limits."""
        model = self._model_name(settings)
        return request.validate_media(
            MODEL_MEDIA_POLICIES[model],
            loader=self.media_loader or MediaLoader(timeout=settings.timeout),
            resolve=True,
        )

    @staticmethod
    def _payload(request: DecisionRequest, model: str) -> dict[str, object]:
        """Omit empty media fields unsupported by a model's input schema."""
        payload = request.model_dump(mode="json", exclude_none=True)
        payload["model"] = model
        for key in ("images", "audio", "videos"):
            if not payload[key]:
                del payload[key]
        return payload

    def invoke(
        self, request: DecisionRequest, settings: DecisionSettings
    ) -> dict[str, object]:
        """Validate the HTTP envelope and return the unwrapped decision result."""
        options = CloudflareOptions.model_validate(settings.options)
        account_id = options.account_id or os.getenv("CLOUDFLARE_ACCOUNT_ID")
        if not account_id:
            raise DecisionError("Set CLOUDFLARE_ACCOUNT_ID or options.account_id")
        try:
            CloudflareOptions(account_id=account_id)
            token = settings.credentials("CLOUDFLARE_API_TOKEN")
        except ValueError as error:
            raise DecisionError(
                "Invalid Cloudflare account or missing API credentials"
            ) from error
        model = self._model_name(settings)
        url = API_URL_TEMPLATE.format(account_id=account_id, model=model)
        payload = self._payload(request, model)
        try:
            with httpx.Client(
                timeout=settings.timeout, transport=self.transport
            ) as client:
                response = client.post(
                    url, headers={"Authorization": f"Bearer {token}"}, json=payload
                )
        except httpx.TransportError as error:
            raise DecisionError(
                "Cloudflare decision transport failed", retryable=True
            ) from error
        if not response.is_success:
            raise DecisionError(
                f"Cloudflare decision request failed (HTTP {response.status_code})",
                retryable=response.status_code in {408, 429}
                or response.status_code >= 500,
            )
        try:
            body: object = response.json()
        except ValueError as error:
            raise DecisionError("Cloudflare returned invalid JSON") from error
        if not isinstance(body, dict):
            raise DecisionError("Cloudflare returned an invalid response envelope")
        envelope = cast(dict[str, object], body)
        result = envelope.get("result")
        if envelope.get("success") is not True or not isinstance(result, dict):
            raise DecisionError("Cloudflare reported an unsuccessful decision")
        output = cast(dict[str, object], result).copy()
        output["request_id"] = response.headers.get("cf-ray")
        return output
