"""Tests for CSP_ACTIVE_ACCOUNTS filter in csp_allocator.

Verifies that the dormant account U22076184 (Yash Trad IRA) is excluded
from CSP entry ticket generation, and that the three active accounts
(Yash Ind, Yash Roth, Vikram Ind) are eligible.
"""

from __future__ import annotations

import pytest

from agt_equities.config import (
    ACCOUNT_ALIAS,
    CSP_ACTIVE_ACCOUNTS,
    is_csp_active_account,
)


pytestmark = pytest.mark.sprint_a


def test_account_alias_has_four_accounts() -> None:
    """The canonical alias map covers all four IBKR account IDs."""
    assert set(ACCOUNT_ALIAS.keys()) == {
        "U21971297",
        "U22076329",
        "U22076184",
        "U22388499",
    }


def test_vikram_label_is_vikram_ind() -> None:
    """Display label ruling 2026-04-22: 'Vikram Ind', not 'Vikram'."""
    assert ACCOUNT_ALIAS["U22388499"] == "Vikram Ind"


def test_yash_trad_ira_is_dormant() -> None:
    """Yash Trad IRA (U22076184) is explicitly excluded from CSP entry."""
    assert "U22076184" not in CSP_ACTIVE_ACCOUNTS


def test_active_accounts_are_the_three_approved() -> None:
    """Only Yash Ind, Yash Roth, Vikram Ind are eligible for CSP entry."""
    assert CSP_ACTIVE_ACCOUNTS == frozenset({
        "U21971297",  # Yash Ind
        "U22076329",  # Yash Roth
        "U22388499",  # Vikram Ind
    })


def test_active_accounts_is_frozenset() -> None:
    """Active-account set is immutable so callers cannot mutate the invariant."""
    assert isinstance(CSP_ACTIVE_ACCOUNTS, frozenset)


def test_active_set_is_subset_of_alias() -> None:
    """Every active account has a display label."""
    assert CSP_ACTIVE_ACCOUNTS <= ACCOUNT_ALIAS.keys()


def test_agt_deck_queries_uses_canonical_alias() -> None:
    """agt_deck.queries imports the canonical map; no duplicate definition."""
    import agt_deck.queries as q
    from agt_equities.config import ACCOUNT_ALIAS as canonical
    assert q.ACCOUNT_ALIAS is canonical


# ---------- is_csp_active_account ----------


@pytest.mark.parametrize("account_id", ["U21971297", "U22076329", "U22388499"])
def test_live_mode_active_accounts_eligible(account_id: str) -> None:
    """In live mode, the three approved accounts pass the filter."""
    assert is_csp_active_account(account_id, mode="live") is True


def test_live_mode_dormant_account_blocked() -> None:
    """In live mode, Yash Trad IRA (U22076184) is blocked."""
    assert is_csp_active_account("U22076184", mode="live") is False


def test_live_mode_unknown_account_blocked() -> None:
    """In live mode, unknown account IDs are blocked (fail-closed)."""
    assert is_csp_active_account("U99999999", mode="live") is False


@pytest.mark.parametrize(
    "account_id",
    [
        "DU1234567",
        "DU7654321",
        "U21971297",
        "U22076184",
        "U99999999",
    ],
)
def test_paper_mode_all_accounts_pass_through(account_id: str) -> None:
    """In paper mode, every account is eligible — dormant is a live-only concept."""
    assert is_csp_active_account(account_id, mode="paper") is True


def test_unknown_mode_defaults_to_live_semantics() -> None:
    """Any mode string other than 'paper' applies the live-mode allow-list.

    Fail-closed: unknown mode behaves like live (restrictive), never like
    paper (permissive). Prevents a typo in AGT_BROKER_MODE from silently
    opening the dormant gate in production.
    """
    assert is_csp_active_account("U22076184", mode="prod") is False
    assert is_csp_active_account("U22076184", mode="") is False
    assert is_csp_active_account("U21971297", mode="live") is True
    assert is_csp_active_account("U21971297", mode="prod") is True
