"""Request/response validation for the run API."""
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from . import settings


class ReviewsInput(BaseModel):
    """Input for the 'reviews' actor -- mirrors main.py's CLI."""
    place: str = Field(min_length=3,
                       description="Place URL, goo.gl link, ChIJ.. id, or 0x..:0x.. feature id")
    sort: Literal["relevant", "newest", "highest", "lowest"] = "newest"
    max_reviews: int | None = Field(default=None, ge=0)
    hl: str = "en"
    delay: float = Field(default=0.3, ge=0, le=10)
    ratings: list[int] | None = None
    details: bool = True
    proxies: list[str] | None = None

    @field_validator("ratings")
    @classmethod
    def _stars(cls, v):
        if v is not None and any(r not in (1, 2, 3, 4, 5) for r in v):
            raise ValueError("ratings must be star values 1-5")
        return v


class PlacesInput(BaseModel):
    """Input for the 'places' actor -- mirrors find.py's CLI."""
    city: str = Field(min_length=2)
    categories: list[str] = Field(min_length=1)
    max_places: int | None = Field(default=None, ge=1)
    hl: str = "en"
    gl: str = "us"
    delay: float = Field(default=0.3, ge=0, le=10)
    details: bool = True
    workers: int = Field(default=4, ge=1, le=16)
    proxies: list[str] | None = None

    @field_validator("categories", mode="before")
    @classmethod
    def _split(cls, v):
        if isinstance(v, str):
            v = [c.strip() for c in v.split(",")]
        return [c for c in v if c]


class CreateRun(BaseModel):
    actor: Literal["reviews", "places"]
    input: dict
    memory_mb: int | None = Field(default=None, ge=128)
    timeout_secs: int | None = Field(default=None, ge=30)

    def validated_input(self):
        model = ReviewsInput if self.actor == "reviews" else PlacesInput
        return model(**self.input).model_dump()

    def clamped_memory(self):
        mb = self.memory_mb or settings.DEFAULT_MEMORY_MB
        return min(mb, settings.MAX_MEMORY_MB)

    def clamped_timeout(self):
        secs = self.timeout_secs or settings.DEFAULT_TIMEOUT_SECS
        return min(secs, settings.MAX_TIMEOUT_SECS)
