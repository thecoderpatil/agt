"""
agt_deck/db.py — backward-compat shim.

The canonical connection module is now agt_equities/db.py. This shim
preserves the get_ro_conn / get_rw_conn names used by existing callers
in the Cure Console FastAPI process. New code should import directly
from agt_equities.db.

This shim will be removed once all agt_deck/* callers have migrated
to the new names.
"""

from agt_equities.db import (  # noqa: F401
    get_db_connection as get_rw_conn,
    get_ro_connection as get_ro_conn,
)
