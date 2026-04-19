"""ADR-014 Pydantic v2 schemas. Extra-forbid on every model; drift = fallback per ADR-013 Layer 1."""

from __future__ import annotations

from pydantic import BaseModel, Field


class BatesParams(BaseModel):
    """Bates model parameters per ADR-014 §3.1.

    Bounds are defensive — rejecting calibrations that wander into
    unstable regions of parameter space. Spot-check any ValidationError
    against recent market regime before relaxing.
    """

    v0: float = Field(gt=0, lt=4, description="initial variance (annualized, vol^2)")
    kappa: float = Field(gt=0, lt=10, description="mean reversion speed")
    theta: float = Field(gt=0, lt=4, description="long-run variance")
    sigma_v: float = Field(gt=0, lt=2, description="vol of vol")
    rho: float = Field(ge=-0.99, le=0.99, description="corr(dW1, dW2)")
    lambda_jump: float = Field(ge=0, le=5, description="jump intensity (per year)")
    mu_J: float = Field(ge=-0.5, le=0.5, description="log-jump mean")
    sigma_J: float = Field(gt=0.001, le=1, description="log-jump stddev")

    model_config = {"extra": "forbid", "frozen": True}


__all__ = ["BatesParams"]
