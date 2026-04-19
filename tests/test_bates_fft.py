"""Correctness tests for agt_equities.synth.bates_fft.

Strategy:
- Smoke: Bates reduces to BS when lambda_jump=0, sigma_v=0, v0=theta (no SV, no jumps).
- Put-call parity: C - P = S*exp(-qT) - K*exp(-rT), to <0.01% abs error.
- Monotonicity: call price decreasing in strike; put price increasing in strike.
- Monte Carlo cross-check: 100k-path MC at a canonical (S0, K, T, params) yields
  price within 2% of FFT (MC stderr dominates error budget at this N).
- OTM-put tail dominance: Bates OTM put price > BS OTM put price at same IV
  (verifies jump contribution to left tail; structural check for wheel-strategy
  validity).

No CBOE data. No disk. Pure numpy.
"""

from __future__ import annotations

import numpy as np
import pytest
from scipy.stats import norm

from agt_equities.synth.bates_fft import (
    bates_fft_call_price,
    bates_fft_put_price,
)
from agt_equities.synth.schemas import BatesParams

pytestmark = pytest.mark.sprint_a


# --- Helpers -----------------------------------------------------------------

def bs_call(S0, K, T, r, q, sigma):
    d1 = (np.log(S0 / K) + (r - q + 0.5 * sigma**2) * T) / (sigma * np.sqrt(T))
    d2 = d1 - sigma * np.sqrt(T)
    return S0 * np.exp(-q * T) * norm.cdf(d1) - K * np.exp(-r * T) * norm.cdf(d2)


def bs_put(S0, K, T, r, q, sigma):
    return bs_call(S0, K, T, r, q, sigma) - S0 * np.exp(-q * T) + K * np.exp(-r * T)


# Canonical Bates params (well-calibrated SPX-like vibes)
CANON_PARAMS = BatesParams(
    v0=0.04, kappa=2.0, theta=0.04, sigma_v=0.3, rho=-0.7,
    lambda_jump=0.1, mu_J=-0.1, sigma_J=0.15,
)


# --- Tests -------------------------------------------------------------------

def test_bates_reduces_to_bs_when_no_jumps_no_sv():
    """\u03bb=0, \u03c3_v=0 \u2192 Bates with constant vol = BS."""
    p = BatesParams(
        v0=0.04, kappa=1e-6, theta=0.04, sigma_v=1e-6, rho=0.0,
        lambda_jump=0.0, mu_J=0.0, sigma_J=0.01,
    )
    S0, T, r, q = 100.0, 0.25, 0.04, 0.0
    K = np.array([90.0, 100.0, 110.0])
    call = bates_fft_call_price(S0, K, T, r, q, p)
    ref = np.array([bs_call(S0, k, T, r, q, sigma=0.2) for k in K])  # sigma = sqrt(0.04)
    rel_err = np.abs(call - ref) / ref
    assert np.all(rel_err < 0.005), f"BS-limit err exceeds 0.5%: {rel_err}"


def test_put_call_parity():
    S0, T, r, q = 100.0, 0.25, 0.04, 0.01
    K = np.array([80.0, 95.0, 100.0, 105.0, 120.0])
    call = bates_fft_call_price(S0, K, T, r, q, CANON_PARAMS)
    put = bates_fft_put_price(S0, K, T, r, q, CANON_PARAMS)
    parity_rhs = S0 * np.exp(-q * T) - K * np.exp(-r * T)
    parity_err = np.abs((call - put) - parity_rhs)
    assert np.all(parity_err < 0.01), f"PCP err exceeds $0.01: {parity_err}"


def test_call_monotonic_in_strike():
    S0, T, r, q = 100.0, 0.25, 0.04, 0.0
    K = np.linspace(70.0, 130.0, 25)
    call = bates_fft_call_price(S0, K, T, r, q, CANON_PARAMS)
    assert np.all(np.diff(call) < 0), "call price must strictly decrease in strike"


def test_put_monotonic_in_strike():
    S0, T, r, q = 100.0, 0.25, 0.04, 0.0
    K = np.linspace(70.0, 130.0, 25)
    put = bates_fft_put_price(S0, K, T, r, q, CANON_PARAMS)
    assert np.all(np.diff(put) > 0), "put price must strictly increase in strike"


def test_mc_cross_check_atm_call():
    """MC reference for a single ATM call. 100k paths at T=30d."""
    S0, T, r, q = 100.0, 30.0 / 365.0, 0.04, 0.0
    K = np.array([100.0])
    fft_price = bates_fft_call_price(S0, K, T, r, q, CANON_PARAMS)[0]

    # Euler discretization of Heston + Merton jumps, 100k paths, 30 steps
    rng = np.random.default_rng(42)
    n_paths = 100_000
    n_steps = 30
    dt = T / n_steps

    S = np.full(n_paths, S0)
    v = np.full(n_paths, CANON_PARAMS.v0)
    k_bar = np.exp(CANON_PARAMS.mu_J + 0.5 * CANON_PARAMS.sigma_J**2) - 1.0
    for _ in range(n_steps):
        z1 = rng.standard_normal(n_paths)
        z2 = CANON_PARAMS.rho * z1 + np.sqrt(1 - CANON_PARAMS.rho**2) * rng.standard_normal(n_paths)
        v_pos = np.maximum(v, 0.0)
        # Poisson jumps
        n_jumps = rng.poisson(CANON_PARAMS.lambda_jump * dt, size=n_paths)
        jump_log = np.where(
            n_jumps > 0,
            n_jumps * CANON_PARAMS.mu_J + np.sqrt(n_jumps) * CANON_PARAMS.sigma_J * rng.standard_normal(n_paths),
            0.0,
        )
        dS_log = (r - q - 0.5 * v_pos - CANON_PARAMS.lambda_jump * k_bar) * dt + np.sqrt(v_pos * dt) * z1 + jump_log
        S = S * np.exp(dS_log)
        v = v + CANON_PARAMS.kappa * (CANON_PARAMS.theta - v_pos) * dt + CANON_PARAMS.sigma_v * np.sqrt(v_pos * dt) * z2

    mc_price = np.exp(-r * T) * np.mean(np.maximum(S - K[0], 0.0))
    rel_err = abs(fft_price - mc_price) / mc_price
    # 100k paths \u2192 MC stderr ~0.4%; allow 2% for composite error budget
    assert rel_err < 0.02, f"FFT vs MC rel_err = {rel_err:.4f} (fft={fft_price:.4f}, mc={mc_price:.4f})"


def test_otm_put_exceeds_bs_tail():
    """Bates OTM put price exceeds BS-at-long-run-vol price (jump tail contribution)."""
    S0, T, r, q = 100.0, 30.0 / 365.0, 0.04, 0.0
    K = np.array([80.0])  # 20% OTM put
    bates_put = bates_fft_put_price(S0, K, T, r, q, CANON_PARAMS)[0]
    bs_ref = bs_put(S0, K[0], T, r, q, sigma=np.sqrt(CANON_PARAMS.theta))
    assert bates_put > bs_ref, (
        f"Bates OTM put ({bates_put:.4f}) must exceed BS ({bs_ref:.4f}) — "
        f"jump contribution to left tail missing?"
    )


def test_strikes_array_shape_preserved():
    S0, T, r, q = 100.0, 0.25, 0.04, 0.0
    K = np.array([90.0, 95.0, 100.0, 105.0, 110.0])
    call = bates_fft_call_price(S0, K, T, r, q, CANON_PARAMS)
    assert call.shape == K.shape


def test_invalid_n_fft_raises():
    S0, T, r, q = 100.0, 0.25, 0.04, 0.0
    K = np.array([100.0])
    with pytest.raises(ValueError, match="power of 2"):
        bates_fft_call_price(S0, K, T, r, q, CANON_PARAMS, N_fft=3000)


def test_non_positive_t_raises():
    S0, r, q = 100.0, 0.04, 0.0
    K = np.array([100.0])
    with pytest.raises(ValueError, match="T must be positive"):
        bates_fft_call_price(S0, K, 0.0, r, q, CANON_PARAMS)


def test_bates_params_extra_forbid():
    """Drift guard — Pydantic rejects unknown keys."""
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        BatesParams(
            v0=0.04, kappa=2.0, theta=0.04, sigma_v=0.3, rho=-0.7,
            lambda_jump=0.1, mu_J=-0.1, sigma_J=0.15,
            mystery_field=1.0,  # type: ignore[call-arg]
        )
