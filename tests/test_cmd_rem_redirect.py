"""ADR-007 Step 5 -- /list_rem /approve_rem /reject_rem now read/write incidents.

Verifies the Telegram handler layer routes to ``agt_equities.incidents_repo``
(the new structured surface) rather than the legacy
``agt_equities.remediation`` state-machine helpers. The GitLab API helpers
(``gitlab_lower_approval_rule`` / ``gitlab_merge_mr`` / ``gitlab_close_mr``)
remain re-used and are patched in each test.

Covers:
    * /list_rem reads from incidents_repo.list_by_status, not the legacy
      remediation.list_awaiting.
    * /approve_rem accepts both numeric incidents.id (with optional '#')
      and legacy ALL_CAPS incident_key; only 'awaiting_approval' can be
      merged; GitLab errors leave the row untouched.
    * /reject_rem routes to incidents_repo.mark_rejected, carries the
      full reason forward, and still closes the MR via remediation.gitlab_close_mr.
    * The /list_rem, /approve_rem, /reject_rem commands are registered.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Module-level import: runs at collection time, before the tripwire
# fixture fires. init_db() lives behind `if __name__ == ...` is false,
# but it runs once regardless; by importing here we materialize the
# module with whatever DB_PATH resolved at collection time (the real
# path). Tests patch repo functions so no runtime DB I/O happens.
import telegram_bot  # noqa: E402
AUTHORIZED_USER_ID = telegram_bot.AUTHORIZED_USER_ID

pytestmark = pytest.mark.sprint_a

# ---------------------------------------------------------------------------
# Fixtures: forged Update + Context
# ---------------------------------------------------------------------------

def _make_update(args_text: str = "") -> SimpleNamespace:
    """Build a minimal telegram.Update stand-in accepted by the handlers."""
    msg = SimpleNamespace()
    msg.reply_text = AsyncMock()
    upd = SimpleNamespace()
    upd.message = msg
    upd.effective_user = SimpleNamespace(id=AUTHORIZED_USER_ID)
    upd.effective_chat = SimpleNamespace(id=AUTHORIZED_USER_ID)
    return upd


def _make_context(args: list[str]) -> SimpleNamespace:
    """Build a minimal ContextTypes.DEFAULT_TYPE stand-in."""
    return SimpleNamespace(args=args, bot=SimpleNamespace(send_message=AsyncMock()))


# ---------------------------------------------------------------------------
# /list_rem
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_rem_reads_from_incidents_repo() -> None:
    """/list_rem calls incidents_repo.list_by_status, not remediation.list_awaiting."""
    fake_rows = [
        {
            "id": 17, "incident_key": "NO_LIVE_IN_PAPER",
            "invariant_id": "NO_LIVE_IN_PAPER", "status": "awaiting_approval",
            "mr_iid": 84, "last_action_at": "2026-04-16T10:15:00+00:00",
        },
        {
            "id": 18, "incident_key": "NO_BELOW_BASIS_CC",
            "invariant_id": "NO_BELOW_BASIS_CC", "status": "awaiting_approval",
            "mr_iid": 85, "last_action_at": "2026-04-16T11:00:00+00:00",
        },
    ]
    upd = _make_update()
    ctx = _make_context([])
    with patch.object(
        telegram_bot, "is_authorized", return_value=True,
    ), patch(
        "agt_equities.incidents_repo.list_by_status",
        return_value=fake_rows,
    ) as mock_list, patch(
        "agt_equities.remediation.list_awaiting",
        side_effect=AssertionError("legacy helper should not be called"),
    ):
        await telegram_bot.cmd_list_rem(upd, ctx)

    mock_list.assert_called_once()
    upd.message.reply_text.assert_awaited_once()
    reply = upd.message.reply_text.await_args.args[0]
    assert "#17" in reply and "#18" in reply
    assert "NO_LIVE_IN_PAPER" in reply
    assert "MR !84" in reply
    # Hint line must mention both arg forms.
    assert "numeric" in reply and "legacy ALL_CAPS" in reply


@pytest.mark.asyncio
async def test_list_rem_empty_queue() -> None:
    """Empty queue renders a friendly 'no active' message."""
    upd = _make_update()
    ctx = _make_context([])
    with patch.object(
        telegram_bot, "is_authorized", return_value=True,
    ), patch(
        "agt_equities.incidents_repo.list_by_status", return_value=[],
    ):
        await telegram_bot.cmd_list_rem(upd, ctx)
    upd.message.reply_text.assert_awaited_once_with(
        "No incidents awaiting approval."
    )


# ---------------------------------------------------------------------------
# /approve_rem
# ---------------------------------------------------------------------------

def _awaiting_row(rid: int = 17, key: str = "NO_LIVE_IN_PAPER",
                  mr: int = 84) -> dict:
    return {
        "id": rid, "incident_key": key, "invariant_id": key,
        "status": "awaiting_approval", "mr_iid": mr,
        "last_action_at": "2026-04-16T10:15:00+00:00",
    }


@pytest.mark.asyncio
async def test_approve_rem_numeric_id() -> None:
    """/approve_rem 17 -> incidents_repo.get(17) -> merge -> mark_merged."""
    upd = _make_update()
    ctx = _make_context(["17"])
    with patch.object(telegram_bot, "is_authorized", return_value=True), \
         patch("agt_equities.incidents_repo.get",
               return_value=_awaiting_row()) as mock_get, \
         patch("agt_equities.incidents_repo.get_by_key",
               side_effect=AssertionError(
                   "numeric id must not route to get_by_key")) as mock_by_key, \
         patch("agt_equities.remediation.gitlab_lower_approval_rule") as mock_lower, \
         patch("agt_equities.remediation.gitlab_merge_mr",
               return_value={"state": "merged"}) as mock_merge, \
         patch("agt_equities.incidents_repo.mark_merged") as mock_mark:
        await telegram_bot.cmd_approve_rem(upd, ctx)

    mock_get.assert_called_once_with(17)
    mock_by_key.assert_not_called()
    mock_lower.assert_called_once_with(84)
    mock_merge.assert_called_once_with(84)
    mock_mark.assert_called_once_with(17)
    reply = upd.message.reply_text.await_args.args[0]
    assert "Merged" in reply and "#17" in reply and "MR !84" in reply


@pytest.mark.asyncio
async def test_approve_rem_hash_prefix_stripped() -> None:
    """/approve_rem '#17' treats '17' as numeric id."""
    upd = _make_update()
    ctx = _make_context(["#17"])
    with patch.object(telegram_bot, "is_authorized", return_value=True), \
         patch("agt_equities.incidents_repo.get",
               return_value=_awaiting_row()) as mock_get, \
         patch("agt_equities.remediation.gitlab_lower_approval_rule"), \
         patch("agt_equities.remediation.gitlab_merge_mr",
               return_value={"state": "merged"}), \
         patch("agt_equities.incidents_repo.mark_merged"):
        await telegram_bot.cmd_approve_rem(upd, ctx)
    mock_get.assert_called_once_with(17)


@pytest.mark.asyncio
async def test_approve_rem_legacy_key() -> None:
    """Legacy ALL_CAPS key routes to incidents_repo.get_by_key."""
    upd = _make_update()
    ctx = _make_context(["NO_LIVE_IN_PAPER"])
    with patch.object(telegram_bot, "is_authorized", return_value=True), \
         patch("agt_equities.incidents_repo.get",
               side_effect=AssertionError(
                   "non-numeric arg must not route to get(int)")), \
         patch("agt_equities.incidents_repo.get_by_key",
               return_value=_awaiting_row()) as mock_by_key, \
         patch("agt_equities.remediation.gitlab_lower_approval_rule"), \
         patch("agt_equities.remediation.gitlab_merge_mr",
               return_value={"state": "merged"}), \
         patch("agt_equities.incidents_repo.mark_merged"):
        await telegram_bot.cmd_approve_rem(upd, ctx)
    mock_by_key.assert_called_once_with("NO_LIVE_IN_PAPER", active_only=True)


@pytest.mark.asyncio
async def test_approve_rem_rejects_non_awaiting_status() -> None:
    """Only 'awaiting_approval' can be merged; other states short-circuit."""
    upd = _make_update()
    ctx = _make_context(["17"])
    row = _awaiting_row()
    row["status"] = "open"
    with patch.object(telegram_bot, "is_authorized", return_value=True), \
         patch("agt_equities.incidents_repo.get", return_value=row), \
         patch("agt_equities.remediation.gitlab_merge_mr") as mock_merge, \
         patch("agt_equities.incidents_repo.mark_merged") as mock_mark:
        await telegram_bot.cmd_approve_rem(upd, ctx)
    mock_merge.assert_not_called()
    mock_mark.assert_not_called()
    reply = upd.message.reply_text.await_args.args[0]
    assert "awaiting_approval" in reply and "open" in reply


@pytest.mark.asyncio
async def test_approve_rem_unknown_id() -> None:
    """Unknown id -> reply 'Unknown incident' without GitLab calls."""
    upd = _make_update()
    ctx = _make_context(["999"])
    with patch.object(telegram_bot, "is_authorized", return_value=True), \
         patch("agt_equities.incidents_repo.get", return_value=None), \
         patch("agt_equities.remediation.gitlab_merge_mr") as mock_merge:
        await telegram_bot.cmd_approve_rem(upd, ctx)
    mock_merge.assert_not_called()
    reply = upd.message.reply_text.await_args.args[0]
    assert "Unknown incident" in reply


@pytest.mark.asyncio
async def test_approve_rem_merge_api_failure_keeps_row() -> None:
    """If GitLab merge returns non-merged state, do not call mark_merged."""
    upd = _make_update()
    ctx = _make_context(["17"])
    with patch.object(telegram_bot, "is_authorized", return_value=True), \
         patch("agt_equities.incidents_repo.get",
               return_value=_awaiting_row()), \
         patch("agt_equities.remediation.gitlab_lower_approval_rule"), \
         patch("agt_equities.remediation.gitlab_merge_mr",
               return_value={"state": "cannot_be_merged"}), \
         patch("agt_equities.incidents_repo.mark_merged") as mock_mark:
        await telegram_bot.cmd_approve_rem(upd, ctx)
    mock_mark.assert_not_called()
    reply = upd.message.reply_text.await_args.args[0]
    assert "did not confirm merge" in reply or "cannot_be_merged" in reply


# ---------------------------------------------------------------------------
# /reject_rem
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reject_rem_routes_to_incidents_repo() -> None:
    """/reject_rem 17 reason... -> incidents_repo.mark_rejected(17, reason)."""
    upd = _make_update()
    ctx = _make_context(["17", "basis", "check", "wrong"])
    with patch.object(telegram_bot, "is_authorized", return_value=True), \
         patch("agt_equities.incidents_repo.get",
               return_value=_awaiting_row()), \
         patch("agt_equities.remediation.gitlab_close_mr") as mock_close, \
         patch("agt_equities.incidents_repo.mark_rejected",
               return_value={"id": 17, "status": "rejected_once"}) as mock_reject, \
         patch("agt_equities.remediation.mark_rejected",
               side_effect=AssertionError(
                   "legacy state helper must not be called")):
        await telegram_bot.cmd_reject_rem(upd, ctx)

    mock_close.assert_called_once_with(84)
    mock_reject.assert_called_once_with(17, "basis check wrong")
    reply = upd.message.reply_text.await_args.args[0]
    assert "Rejected" in reply and "rejected_once" in reply


@pytest.mark.asyncio
async def test_reject_rem_requires_reason() -> None:
    """Missing reason is rejected with a usage message."""
    upd = _make_update()
    ctx = _make_context(["17"])  # no reason
    with patch.object(telegram_bot, "is_authorized", return_value=True), \
         patch("agt_equities.incidents_repo.mark_rejected") as mock_reject:
        await telegram_bot.cmd_reject_rem(upd, ctx)
    mock_reject.assert_not_called()
    reply = upd.message.reply_text.await_args.args[0]
    assert "Usage" in reply or "reason" in reply.lower()


@pytest.mark.asyncio
async def test_reject_rem_gitlab_close_failure_non_fatal() -> None:
    """gitlab_close_mr failure still advances the incidents row."""
    upd = _make_update()
    ctx = _make_context(["17", "bad", "patch"])
    with patch.object(telegram_bot, "is_authorized", return_value=True), \
         patch("agt_equities.incidents_repo.get",
               return_value=_awaiting_row()), \
         patch("agt_equities.remediation.gitlab_close_mr",
               side_effect=RuntimeError("GitLab 500")), \
         patch("agt_equities.incidents_repo.mark_rejected",
               return_value={"id": 17, "status": "rejected_once"}) as mock_reject:
        await telegram_bot.cmd_reject_rem(upd, ctx)
    mock_reject.assert_called_once_with(17, "bad patch")


# ---------------------------------------------------------------------------
# Command registration
# ---------------------------------------------------------------------------

def test_commands_registered() -> None:
    """The three /*_rem handlers are bound by bootstrap_application.

    We inspect the source of the dispatcher-binding function rather than
    spinning up a real Application (which requires a Telegram token + bot
    session). The goal is to detect accidental deletion.
    """
    import inspect
    import telegram_bot as tb
    # The binder lives in a helper; grep its source.
    # Grab the module source once.
    src = inspect.getsource(tb)
    assert 'CommandHandler("list_rem"' in src
    assert 'CommandHandler("approve_rem"' in src
    assert 'CommandHandler("reject_rem"' in src


def test_resolve_helper_existence() -> None:
    """The helper is importable and routes digits vs non-digits correctly."""
    import telegram_bot as tb
    from unittest.mock import patch
    with patch("agt_equities.incidents_repo.get", return_value={"id": 17}) as g, \
         patch("agt_equities.incidents_repo.get_by_key",
               return_value={"id": 99}) as gb:
        assert tb._resolve_incident_arg("17") == {"id": 17}
        assert tb._resolve_incident_arg("#17") == {"id": 17}
        assert tb._resolve_incident_arg("SOMETHING") == {"id": 99}
        g.assert_called_with(17)
        gb.assert_called_with("SOMETHING", active_only=True)
    # Empty / whitespace -> None.
    assert tb._resolve_incident_arg("") is None
    assert tb._resolve_incident_arg("   ") is None
