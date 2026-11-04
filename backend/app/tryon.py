from typing import Protocol, Optional
from pathlib import Path
import base64
import os
import logging
import httpx
from .evaluation import measured_post, record_event
from . import config  # Load .env before selecting the hosted provider.

logger = logging.getLogger("uvicorn.error")

TRY_ON_PROMPT = (
    "Create one photorealistic virtual clothing try-on image. "
    "Reference image 1 is the customer: preserve this exact person’s facial identity, "
    "skin tone, body shape, pose, camera framing and background. "
    "Reference image 2 is a garment reference only. Dress the customer from image 1 "
    "in the featured garment from image 2, matching its colour, fabric, pattern and cut. "
    "Do not copy the face, body or pose of any merchant model in image 2. "
    "Replace only the relevant clothing; keep other clothing intact. Preserve the customer’s existing body anatomy and proportions; never add or reshape anatomical features to fit the garment. "
    "Render natural fit, draping and lighting. Do not add text, collages or extra people."
)


class TryOnError(Exception):
    """An actionable provider error safe to display without credentials."""


class MetaImageTryOnProvider:
    async def generate(
        self, photo: Path, product_image: str, image_attributes: dict
    ) -> str:
        if not product_image.startswith("https://"):
            raise TryOnError("This product has no supported garment image.")
        customer_image = "data:image/jpeg;base64," + base64.b64encode(
            photo.read_bytes()
        ).decode("ascii")
        colours = image_attributes.get("colours", [])
        colour_text = ", ".join(colours)
        prompt = TRY_ON_PROMPT

        if colour_text:
            prompt += f"\nMerchant colour labels: {colour_text}."

        try:
            async with httpx.AsyncClient(timeout=110) as client:
                response = await measured_post(
                    client,
                    operation="image_generation",
                    provider="meta",
                    model="muse-image-1.0",
                    url="https://api.meta.ai/v1/images/edits",
                    headers={"Authorization": "Bearer " + os.environ["MODEL_API_KEY"]},
                    json={
                        "model": "muse-image-1.0",
                        "prompt": prompt,
                        "images": [
                            {"image_url": customer_image},
                            {"image_url": product_image},
                        ],
                        "n": 1,
                        "response_format": "url",
                    },
                )
            logger.info("Muse Image response status=%d", response.status_code)
            if response.status_code in (401, 403):
                raise TryOnError(
                    "Image generation access was denied. Check the Meta API key and Muse Image model access."
                )
            if response.status_code == 429:
                raise TryOnError(
                    "Image generation rate limit or quota reached. Please try again later."
                )
            if response.status_code == 404:
                raise TryOnError(
                    "Muse Image is not available for this account or endpoint."
                )
            response.raise_for_status()
            url = response.json()["data"][0]["url"]
            if not isinstance(url, str) or not url.startswith("https://"):
                raise ValueError("No HTTPS image URL")
            return url
        except (httpx.HTTPError, KeyError, IndexError, ValueError, TypeError) as exc:
            raise TryOnError(
                "The hosted image service could not generate this preview. Please retry."
            ) from exc


def configured_provider():
    selected = os.getenv("TRY_ON_PROVIDER", "muse-image").strip().lower()
    if selected == "muse-image" and os.getenv("MODEL_API_KEY", "").strip():
        return MetaImageTryOnProvider()
    return None


class TryOnProvider(Protocol):
    """Implement with a licensed image-generation service, then inject at startup.
    Preserve identity, skin tone, body shape and pose; no user masks required.
    Return an HTTPS image URL. Never substitute the original product image.
    """

    async def generate(
        self, photo: Path, product_image: str, image_attributes: dict
    ) -> str: ...


provider: Optional[TryOnProvider] = configured_provider()
