"""Tests for CSP_ACTIVE_ACCOUNTS filter in csp_allocator.

Verifies that the dormant account U22076184 (Yash Trad IRA) is excluded
from CSP entry ticket generation, and that the three active accounts
(Yash Ind, Yash Roth, Vikram Ind) are eligible.
"""

from __future__ import annotations

import pytest

from agt_equities.config import ACCOUNT_ALIAS, CSP_ACTIVE_ACCOUNTS


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
