"""Bates (Heston + Merton jumps) option pricer via Carr-Madan FFT.

References:
    Bates, D. (1996). "Jumps and stochastic volatility: Exchange rate
    processes implicit in Deutsche Mark options." Review of Financial
    Studies, 9(1), 69-107.

    Carr, P. & Madan, D. (1999). "Option valuation using the fast
    Fourier transform." Journal of Computational Finance, 2(4), 61-73.

No I/O, no state. All functions are pure.
"""

from __future__ import annotations

import numpy as np

from agt_equities.synth.schemas import BatesParams


def bates_characteristic_function(
    u: np.ndarray,
    T: float,
    r: float,
    q: float,
    params: BatesParams,
) -> np.ndarray:
    """Bates characteristic function phi(u; T) = E[exp(i*u*ln(S_T))].

    Accepts complex-valued u arrays (the FFT routine passes
    u_j = v_j - (alpha + 1)*i).
    """
    v0, kappa, theta, sigma_v, rho = (
        params.v0, params.kappa, params.theta, params.sigma_v, params.rho,
    )
    lambda_, mu_J, sigma_J = params.lambda_jump, params.mu_J, params.sigma_J

    # Heston component — Albrecher-Mayer stable form uses (xi - d) numerator
    xi = kappa - rho * sigma_v * 1j * u
    d = np.sqrt(xi**2 + sigma_v**2 * (1j * u + u**2))
    g = (xi - d) / (xi + d)

    exp_dT = np.exp(-d * T)
    D = (xi - d) / sigma_v**2 * ((1.0 - exp_dT) / (1.0 - g * exp_dT))
    C = (r - q) * 1j * u * T + (kappa * theta / sigma_v**2) * (
        (xi - d) * T - 2.0 * np.log((1.0 - g * exp_dT) / (1.0 - g))
    )

    heston_cf = np.exp(C + D * v0)

    # Merton jump adjustment: E[exp(i*u*sum_of_jumps_over_T)]
    k_bar = np.exp(mu_J + 0.5 * sigma_J**2) - 1.0
    jump_cf = np.exp(
        T * lambda_ * (np.exp(1j * u * mu_J - 0.5 * u**2 * sigma_J**2) - 1.0 - 1j * u * k_bar)
    )

    return heston_cf * jump_cf


def bates_fft_call_price(
    S0: float,
    strikes: np.ndarray,
    T: float,
    r: float,
    q: float,
    params: BatesParams,
    alpha: float = 1.5,
    N_fft: int = 4096,
    eta: float = 0.25,
) -> np.ndarray:
    """Call prices under Bates via Carr-Madan FFT.

    Args:
        S0: spot.
        strikes: sorted strike array (absolute, not K/S0).
        T: time to expiry in years.
        r: continuously-compounded risk-free rate.
        q: continuous dividend yield.
        params: BatesParams.
        alpha: damping coefficient (default 1.5 per Carr-Madan).
        N_fft: FFT grid size (default 4096, must be power of 2).
        eta: log-strike grid step (default 0.25).

    Returns:
        np.ndarray of call prices aligned to `strikes`.

    Accuracy target: <0.5% error vs MC reference for 0.1 \u2264 K/S0 \u2264 1.2,
    7 \u2264 DTE \u2264 60 days. Sentinel test enforces on canonical strikes.
    """
    if N_fft & (N_fft - 1):
        raise ValueError(f"N_fft must be a power of 2 (got {N_fft})")
    if T <= 0:
        raise ValueError(f"T must be positive (got {T})")

    strikes = np.asarray(strikes, dtype=float)

    lambda_grid = 2.0 * np.pi / (N_fft * eta)
    b = lambda_grid * N_fft / 2.0

    j = np.arange(N_fft)
    v = j * eta
    u = v - (alpha + 1.0) * 1j

    phi = bates_characteristic_function(u, T, r, q, params)
    denom = alpha**2 + alpha - v**2 + 1j * (2.0 * alpha + 1.0) * v
    psi = np.exp(-r * T) * phi / denom

    # Simpson 1/3 weights: w_0 = 1/3, w_j = (3 + (-1)^j)/3 for j>=1
    simpson = (3.0 + (-1.0) ** np.arange(1, N_fft + 1)) / 3.0
    simpson[0] = 1.0 / 3.0

    x = np.exp(1j * b * v) * psi * eta * simpson
    fft_vals = np.real(np.fft.fft(x))

    # Log-strike grid. Carr-Madan frames the pricer with unit spot;
    # shift by ln(S0) to recover absolute strikes.
    log_k_grid = -b + lambda_grid * j
    log_k_grid_shifted = log_k_grid + np.log(S0)
    k_grid_abs = np.exp(log_k_grid_shifted)
    call_grid = S0 * np.exp(-alpha * log_k_grid) * fft_vals / np.pi

    return np.interp(strikes, k_grid_abs, call_grid)


def bates_fft_put_price(
    S0: float,
    strikes: np.ndarray,
    T: float,
    r: float,
    q: float,
    params: BatesParams,
    **fft_kwargs,
) -> np.ndarray:
    """Put prices via put-call parity: P = C - S0*exp(-q*T) + K*exp(-r*T)."""
    strikes = np.asarray(strikes, dtype=float)
    call = bates_fft_call_price(S0, strikes, T, r, q, params, **fft_kwargs)
    return call - S0 * np.exp(-q * T) + strikes * np.exp(-r * T)


__all__ = [
    "BatesParams",
    "bates_characteristic_function",
    "bates_fft_call_price",
    "bates_fft_put_price",
]
