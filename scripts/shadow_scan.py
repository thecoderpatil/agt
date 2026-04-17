"""Shadow Scan CLI - dry-run harness for every AGT decision engine.

See ``docs/adr/ADR-008_SHADOW_SCAN.md`` for the full architecture.

MR 1 scope: plumbing skeleton.
MR 2 scope: CSP allocator wired. ``--engine csp`` threads the shadow
``RunContext`` into ``run_csp_allocator`` so ``CollectorOrderSink``
captures staged tickets instead of writing ``pending_orders``.

Full candidate generation (RAY scan + yfinance extras + per-household
snapshot reconstruction) still lives inside ``telegram_bot.py`` /
``scan_csp_setups``. Extracting that pipeline behind a shared entry
point is follow-up scope (MR 2.x). Until then the CLI exercises the
ctx seam by invoking the allocator against an empty candidate list -
enough to prove the signature + sink wiring are reachable from outside
the bot stack without pulling ``ib_async`` or ``yfinance`` into the
CLI's import graph.

Runtime guards (never relaxable):
    1. ``ctx.mode is RunMode.SHADOW`` immediately after construction
       (matches invariant ``NO_LIVE_CTX_IN_SHADOW_SCRIPT``).
    2. ``db_path`` never equals ``PROD_DB_PATH`` (matches invariant
       ``NO_SHADOW_ON_PROD_DB``).

Both guards are plain ``if ... raise RuntimeError`` so they survive
``python -O``. Tripping either aborts with exit code 3.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

# Hard import guard - shadow_scan MUST refuse to run if agt_equities is
# not importable. Better to blow up loud than silently fall through.
try:
    from agt_equities.runtime import (
        PROD_DB_PATH,
        RunContext,
        RunMode,
        clone_sqlite_db_with_wal,
    )
    from agt_equities.sinks import (
        CollectorDecisionSink,
        CollectorOrderSink,
        ShadowDecision,
        ShadowOrder,
    )
except ImportError as exc:  # pragma: no cover - defensive
    sys.stderr.write(
        f"agt_equities import failed; cannot run shadow scan: {exc}\n"
    )
    sys.exit(2)


REPO_ROOT = Path(__file__).resolve().parent.parent
REPORTS_DIR = REPO_ROOT / "reports"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="shadow_scan",
        description=(
            "Dry-run every AGT decision engine against real account state. "
            "No DB writes, no IB placements - drained to JSON for Cowork "
            "review."
        ),
    )
    p.add_argument(
        "--engine",
        choices=("csp", "harvest", "roll", "cc", "all"),
        default="all",
        help=(
            "Which engine(s) to shadow. MR 2: 'csp' wired via ctx seam "
            "(empty-candidate invocation); others still stubbed."
        ),
    )
    p.add_argument(
        "--gateway",
        choices=("paper", "live"),
        default="paper",
        help="IB gateway to read from (read-only). Default: paper.",
    )
    p.add_argument(
        "--emit",
        choices=("json", "telegram"),
        default="json",
        help="Output format. MR 1: json only. MR 6 wires telegram.",
    )
    p.add_argument(
        "--db-clone",
        type=str,
        default=None,
        help=(
            "Path to an existing SQLite clone to run against. "
            "When omitted, a fresh clone of the production DB is created "
            "via clone_sqlite_db_with_wal()."
        ),
    )
    return p.parse_args(argv)


def build_shadow_ctx(db_clone_path: str) -> RunContext:
    """Construct a shadow ctx and enforce both hard guards.

    Both asserts are runtime (``if ... raise``) so they survive
    ``python -O``.

    Raises:
        RuntimeError: if ``db_clone_path`` equals ``PROD_DB_PATH`` or if
            the constructed ``ctx.mode`` is not ``RunMode.SHADOW`` (the
            second case is defensive; a ``RunMode`` enum constructor
            could not plausibly flip after this module is imported, but
            the assert is cheap insurance).
    """
    if db_clone_path == PROD_DB_PATH:
        raise RuntimeError(
            "NO_SHADOW_ON_PROD_DB: shadow_scan refused db_path == "
            f"PROD_DB_PATH ({PROD_DB_PATH!r}). Use a cloned DB via "
            "clone_sqlite_db_with_wal()."
        )

    order_sink = CollectorOrderSink()
    decision_sink = CollectorDecisionSink()
    ctx = RunContext(
        mode=RunMode.SHADOW,
        run_id=uuid.uuid4().hex,
        order_sink=order_sink,
        decision_sink=decision_sink,
        db_path=db_clone_path,
    )
    if ctx.mode is not RunMode.SHADOW:
        raise RuntimeError(
            "NO_LIVE_CTX_IN_SHADOW_SCRIPT: constructed ctx.mode is not SHADOW"
        )
    return ctx


def _run_csp_engine(ctx: RunContext) -> None:
    """Invoke ``run_csp_allocator`` under the shadow ctx.

    MR 2 scope: exercise the ctx seam mechanically. We pass an empty
    candidate list because the full candidate pipeline (RAY screen +
    yfinance extras + snapshot load) still lives inside the bot stack.
    That pipeline is extracted in follow-up scope (tracked as MR 2.x).

    Even with zero candidates, this invocation proves three things:
      1. ``run_csp_allocator`` imports cleanly against the MR 2 signature
         (``ctx: RunContext`` required keyword-only).
      2. The allocator short-circuits on an empty candidate list without
         touching the ctx's order_sink (recorded in the digest).
      3. Follow-up MR 2.x can drop real candidates in and the ctx seam
         will carry staged tickets to ``CollectorOrderSink`` unchanged.
    """
    try:
        from agt_equities.csp_allocator import run_csp_allocator
    except ImportError as exc:  # pragma: no cover - defensive
        sys.stderr.write(
            f"[shadow_scan] csp_allocator import failed: {exc}\n"
        )
        return

    def _empty_extras_provider(snapshot: dict, candidate) -> dict:
        return {}

    try:
        result = run_csp_allocator(
            ray_candidates=[],
            snapshots={},
            vix=0.0,
            extras_provider=_empty_extras_provider,
            ctx=ctx,
        )
    except Exception as exc:
        sys.stderr.write(
            f"[shadow_scan] run_csp_allocator raised: {exc}\n"
        )
        return

    n_allocations = len(getattr(result, "allocations", []) or [])
    n_errors = len(getattr(result, "errors", []) or [])
    sys.stdout.write(
        f"[shadow_scan] csp: allocator completed ctx.run_id={ctx.run_id} "
        f"allocations={n_allocations} errors={n_errors} "
        "(empty-candidate invocation; full RAY pipeline follow-up scope)\n"
    )



def _run_harvest_engine(ctx: RunContext) -> None:
    """Invoke ``scan_csp_harvest_candidates`` under the shadow ctx.

    MR 3 scope: exercise the ctx seam mechanically. Passes empty
    positions so the scan short-circuits without touching IB.
    Full IB-connect + positions fetch deferred to pipeline extraction MR.
    """
    import asyncio
    try:
        from agt_equities.csp_harvest import scan_csp_harvest_candidates
    except ImportError as exc:  # pragma: no cover - defensive
        sys.stderr.write(
            f"[shadow_scan] csp_harvest import failed: {exc}\n"
        )
        return

    class _EmptyIB:
        async def reqPositionsAsync(self): return []

    try:
        result = asyncio.run(
            scan_csp_harvest_candidates(_EmptyIB(), ctx=ctx)
        )
    except Exception as exc:
        sys.stderr.write(
            f"[shadow_scan] scan_csp_harvest_candidates raised: {exc}\n"
        )
        return

    n_staged = len(result.get("staged", []))
    sys.stdout.write(
        f"[shadow_scan] harvest: scan completed ctx.run_id={ctx.run_id} "
        f"staged={n_staged} "
        "(empty-positions invocation; full IB pipeline follow-up scope)\n"
    )

def run_engines_stub(ctx: RunContext, engine: str) -> None:
    """Dispatch to per-engine shadow branches.

    MR 1 landed as a single placeholder. MR 2 wires ``csp``. Harvest /
    roll / cc land in MRs 3-5. ``all`` runs every wired engine in order.
    """
    wired: dict[str, callable] = {
        "csp": _run_csp_engine,
        "harvest": _run_harvest_engine,
    }

    def _stub(_ctx: RunContext, engine_name: str) -> None:
        sys.stdout.write(
            f"[shadow_scan] {engine_name}: not wired yet "
            f"(ctx.run_id={_ctx.run_id}) - will land in a future MR.\n"
        )

    if engine == "all":
        for name in ("csp", "harvest", "roll", "cc"):
            fn = wired.get(name)
            if fn is None:
                _stub(ctx, name)
            else:
                fn(ctx)
        return

    fn = wired.get(engine)
    if fn is None:
        _stub(ctx, engine)
    else:
        fn(ctx)


def render_digest(
    orders: list[ShadowOrder],
    decisions: list[ShadowDecision],
    ctx: RunContext,
) -> str:
    """Render a plaintext digest of what the engines would have staged.

    Format is stable so Cowork-side diffs stay readable. MR 1 expects an
    empty digest because no engine runs; MR 2 starts populating it.
    """
    lines: list[str] = []
    lines.append(f"# Shadow Scan {ctx.run_id}")
    lines.append(f"#   mode={ctx.mode.value}")
    lines.append(f"#   db_path={ctx.db_path}")
    lines.append(f"#   generated_at={_utc_now_iso()}")
    lines.append(f"#   orders={len(orders)} decisions={len(decisions)}")
    if not orders and not decisions:
        lines.append("# (no engine output - MR 1 plumbing only)")
    else:
        for so in sorted(orders, key=lambda x: (x.engine, x.ticker)):
            limit_str = "MKT" if so.limit is None else f"{so.limit:.2f}"
            lines.append(
                f"  {so.engine:16s} {so.ticker:6s} {so.right} "
                f"{so.strike:>8.2f} x{so.qty:<4d} @{limit_str:>7s}"
            )
        for sd in decisions:
            lines.append(f"  [{sd.kind}] {sd.payload}")
    return "\n".join(lines) + "\n"


def write_json_artifact(
    ctx: RunContext,
    orders: list[ShadowOrder],
    decisions: list[ShadowDecision],
    reports_dir: Path | None = None,
) -> Path:
    """Persist a machine-readable artifact for downstream tooling.

    Returns the absolute path written. Creates ``reports/`` if missing.
    """
    if reports_dir is None:
        reports_dir = REPORTS_DIR
    reports_dir.mkdir(parents=True, exist_ok=True)
    out = reports_dir / f"shadow_scan_{ctx.run_id}.json"
    payload = {
        "run_id": ctx.run_id,
        "mode": ctx.mode.value,
        "db_path": ctx.db_path,
        "generated_at": _utc_now_iso(),
        "orders": [
            {
                "engine": so.engine,
                "ticker": so.ticker,
                "right": so.right,
                "strike": so.strike,
                "qty": so.qty,
                "limit": so.limit,
                "decided_at": so.decided_at,
                "meta": so.meta,
            }
            for so in orders
        ],
        "decisions": [
            {
                "kind": sd.kind,
                "run_id": sd.run_id,
                "payload": sd.payload,
                "decided_at": sd.decided_at,
            }
            for sd in decisions
        ],
    }
    out.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return out


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.db_clone is not None:
        db_clone_path = args.db_clone
        owns_clone = False
    else:
        try:
            db_clone_path = clone_sqlite_db_with_wal(PROD_DB_PATH)
            owns_clone = True
        except FileNotFoundError:
            # Degrade gracefully on dev boxes where PROD_DB_PATH is absent.
            sys.stderr.write(
                f"[shadow_scan] source DB missing: {PROD_DB_PATH}. "
                "Running against an empty ':memory:' placeholder.\n"
            )
            db_clone_path = ":memory:"
            owns_clone = False

    try:
        ctx = build_shadow_ctx(db_clone_path)
    except RuntimeError as exc:
        sys.stderr.write(f"[shadow_scan] invariant trip: {exc}\n")
        return 3

    try:
        run_engines_stub(ctx, args.engine)
    finally:
        orders = (
            ctx.order_sink.drain()
            if hasattr(ctx.order_sink, "drain")
            else []
        )
        decisions = (
            ctx.decision_sink.drain()
            if hasattr(ctx.decision_sink, "drain")
            else []
        )

    if args.emit == "json":
        artifact = write_json_artifact(ctx, orders, decisions)
        sys.stdout.write(f"[shadow_scan] wrote {artifact}\n")
    elif args.emit == "telegram":  # pragma: no cover - MR 6 scope
        sys.stdout.write(
            "[shadow_scan] telegram emit is MR 6 scope - not wired yet.\n"
        )

    sys.stdout.write(render_digest(orders, decisions, ctx))

    if owns_clone and db_clone_path not in (":memory:",):
        # Tear down the clone's parent tempdir. Safe because
        # clone_sqlite_db_with_wal() creates a fresh mkdtemp() when
        # dest_dir is None.
        try:
            shutil.rmtree(
                Path(db_clone_path).parent, ignore_errors=True
            )
        except OSError:
            pass

    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
