"""Self-healing bootstrap assertion.

Boot-time guard: verify that agt_equities.db.DB_PATH resolves to the
canonical production DB path. If not, halt loudly -- Telegram page first,
stderr second, non-zero exit third.

This module MUST NOT open a SQLite connection or import the incidents
write surface. The whole point is to catch the case where the incidents
table is the wrong table.
"""
from __future__ import annotations

import sys
from pathlib import Path

from agt_equities.runtime import PROD_DB_PATH


class SelfHealingBootstrapError(RuntimeError):
    """Raised when the self-healing surface resolves a non-canonical DB path.

    Deliberately NOT caught by the standard exception guard in
    check_invariants_tick. This exception MUST propagate to the service
    supervisor (NSSM) so the service exits non-zero and the restart loop
    surfaces the failure. Silent downgrade is the exact failure mode this
    guard exists to prevent.
    """


def assert_canonical_db_path(
    *,
    resolved_path,
    allow_override: bool = False,
) -> None:
    """Validate the resolved self-healing DB path at service boot.

    Args:
        resolved_path: The path that ``agt_equities.db.DB_PATH`` resolved
            to, typically passed as ``agt_equities.db.DB_PATH`` itself.
        allow_override: Tests and ``shadow_scan.py`` must pass True.
            Production entry-points (telegram_bot, agt_scheduler) MUST
            pass False.

    Raises:
        SelfHealingBootstrapError: if ``resolved_path`` differs from
        ``PROD_DB_PATH`` and ``allow_override`` is False.
    """
    canonical = Path(PROD_DB_PATH).resolve()
    resolved = Path(resolved_path).resolve()
    if resolved == canonical:
        return
    if allow_override:
        return

    msg = (
        f"SELF_HEALING_DB_PATH_MISMATCH: resolved={resolved} "
        f"canonical={canonical}. Service refuses to start. Set "
        f"AGT_DB_PATH={canonical} in the NSSM env or fix the _BASE_DIR "
        f"resolution in agt_equities/db.py."
    )
    print(msg, file=sys.stderr, flush=True)

    try:
        from agt_equities.telegram_utils import send_telegram_message
        send_telegram_message(
            f"AGT BOOT HALTED\n{msg}",
            parse_mode=None,
        )
    except Exception:
        # Telegram unreachable is acceptable; stderr + non-zero exit is enough.
        pass

    raise SelfHealingBootstrapError(msg)
