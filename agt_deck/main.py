"""AGT Equities Command Deck — FastAPI application."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path

import jinja2
from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

# Add project root to path for agt_equities imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agt_deck.db import get_ro_conn, get_rw_conn
from agt_deck import queries, formatters
from agt_equities import risk

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("agt_deck")

DECK_TOKEN = os.environ.get("AGT_DECK_TOKEN", "")
from agt_equities.config import PAPER_MODE  # Sprint C pre-step: single source for AGT_PAPER_MODE
BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="AGT Command Deck", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
# Manual Jinja2 env — avoids Starlette's Jinja2Templates cache-key hashing bug
_jinja_env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(str(BASE_DIR / "templates")),
    autoescape=True,
)

_FMT_CONTEXT = {
    "money": formatters.money,
    "pct": formatters.pct,
    "pnl_color": formatters.pnl_color,
    "color_class": formatters.color_class,
    "concentration_color": formatters.concentration_color,
    "time_ago": formatters.time_ago,
    "format_age": formatters.format_age,
    "el_pct_color": formatters.el_pct_color,
    "lifecycle_state_classes": formatters.lifecycle_state_classes,
    "paper_mode": PAPER_MODE,
}


def _render(template_name: str, context: dict):
    """Render template manually, bypassing Starlette's cache-key issue."""
    context.update(_FMT_CONTEXT)
    template = _jinja_env.get_template(template_name)
    html = template.render(**context)
    return HTMLResponse(html)


# ── Auth middleware ───────────────────────────────────────────────

class TokenAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # Skip auth for static files
        if request.url.path.startswith("/static"):
            return await call_next(request)
        token = request.query_params.get("t", "")
        if not DECK_TOKEN or token != DECK_TOKEN:
            return Response("Unauthorized", status_code=401)
        return await call_next(request)

app.add_middleware(TokenAuthMiddleware)


# ── VIX cache ────────────────────────────────────────────────────

_vix_cache: dict = {"value": None, "fetched_at": 0}

def get_vix() -> float | None:
    """Fetch VIX from yfinance, cached 5 minutes."""
    now = time.time()
    if _vix_cache["value"] is not None and now - _vix_cache["fetched_at"] < 300:
        return _vix_cache["value"]
    try:
        import yfinance as yf
        ticker = yf.Ticker("^VIX")
        val = ticker.fast_info.get("lastPrice") or ticker.info.get("regularMarketPrice")
        if val:
            _vix_cache["value"] = float(val)
            _vix_cache["fetched_at"] = now
            return float(val)
    except Exception as exc:
        logger.warning("VIX fetch failed: %s", exc)
    return _vix_cache["value"]


# ── Spot price cache ─────────────────────────────────────────────

_spot_cache: dict = {}  # {ticker: (price, fetched_at)}

def get_spots(tickers: list[str]) -> dict[str, float]:
    """Batch fetch spot prices via yfinance, cached 60s."""
    now = time.time()
    result = {}
    need_fetch = []
    for t in tickers:
        cached = _spot_cache.get(t)
        if cached and now - cached[1] < 60:
            result[t] = cached[0]
        else:
            need_fetch.append(t)

    if need_fetch:
        try:
            import yfinance as yf
            data = yf.download(need_fetch, period="1d", progress=False)
            if not data.empty:
                close = data["Close"]
                if hasattr(close, "iloc"):
                    last_row = close.iloc[-1]
                    for t in need_fetch:
                        try:
                            val = float(last_row[t]) if t in last_row.index else None
                            if val and val > 0:
                                _spot_cache[t] = (val, now)
                                result[t] = val
                        except Exception:
                            pass
        except Exception as exc:
            logger.warning("Spot fetch failed: %s", exc)

    return result


# ── Active cycles loader ─────────────────────────────────────────

def load_active_cycles() -> list:
    """Load Walker active cycles from trade_repo."""
    try:
        from agt_equities import trade_repo
        trade_repo.DB_PATH = str(Path(__file__).resolve().parent.parent / "agt_desk.db")
        return trade_repo.get_active_cycles()
    except Exception as exc:
        logger.warning("load_active_cycles failed: %s", exc)
        return []


# ── Build top strip data ─────────────────────────────────────────

def build_top_strip(conn) -> dict:
    # ── Sprint C2: SSOT reads from DeskSnapshot (NAV, cycles, betas) ──
    from agt_equities.state_builder import build_state
    from agt_equities import trade_repo

    snapshot = build_state(db_path=str(trade_repo.DB_PATH))

    # Filter "live_positions not provided" — expected noise at this layer
    # because build_top_strip has no IB context.
    _snap_warnings = [w for w in snapshot.warnings if "live_positions not provided" not in w]
    if _snap_warnings:
        for w in _snap_warnings:
            logger.info("build_top_strip: DeskSnapshot warning: %s", w)

    nav_by_acct = snapshot.nav_by_account
    total_nav = snapshot.nav_total
    hh_nlv = dict(snapshot.household_nav)  # mutable copy for downstream
    cycles = snapshot.active_cycles

    # ── Local reads stay on passed-in conn ──
    vix = get_vix()
    change_nav = queries.get_change_in_nav(conn)
    last_sync = queries.get_last_sync(conn)
    industries = queries.get_ticker_industries(conn)

    # Inception P&L = current NAV − net inflows (deposits + asset transfers)
    net_deposits = 0
    net_asset_transfers = 0
    for acct, data in change_nav.items():
        net_deposits += float(data.get("deposits_withdrawals") or 0)
        net_asset_transfers += float(data.get("asset_transfers") or 0)
    net_inflows = net_deposits + net_asset_transfers
    inception_pnl = total_nav - net_inflows
    inception_pnl_pct = (inception_pnl / net_inflows * 100) if net_inflows > 0 else None

    # Sprint 1F Fix 3B: read EL from el_snapshots (written by bot's el_snapshot_writer)
    el_retain_pct = risk.vix_required_el_pct(vix) if vix else None
    _el_data = queries.get_health_strip_data(conn)
    _yash_el = sum(
        a["excess_liquidity"] or 0 for a in _el_data.get("accounts", [])
        if a.get("household") == "Yash_Household" and a.get("excess_liquidity") is not None
    )
    _vikram_el = sum(
        a["excess_liquidity"] or 0 for a in _el_data.get("accounts", [])
        if a.get("household") == "Vikram_Household" and a.get("excess_liquidity") is not None
    )
    el_current = _yash_el if _yash_el > 0 else None
    el_required = None  # EL required from VIX table, needs total margin NLV — deferred
    vikram_el_pct = None
    _vikram_nlv = sum(
        a["nlv"] or 0 for a in _el_data.get("accounts", [])
        if a.get("household") == "Vikram_Household" and a.get("nlv") is not None
    )
    if _vikram_nlv > 0 and _vikram_el > 0:
        vikram_el_pct = round(_vikram_el / _vikram_nlv * 100, 1)

    # Concentration — use spot prices, per-household
    wheel_tickers = list({c.ticker for c in cycles if c.status == 'ACTIVE' and c.cycle_type == 'WHEEL'})
    conc_spots = get_spots(wheel_tickers) if wheel_tickers else {}
    conc_ticker, conc_pct, conc_hh = risk.concentration_check(cycles, hh_nlv, conc_spots)

    # Sector violations
    sector_v = risk.sector_violations(cycles, industries)

    # Rule 11: Beta-weighted leverage (Sprint C2: betas from DeskSnapshot, 1.0 fallback)
    _betas = {t: snapshot.beta_by_symbol.get(t, 1.0) for t in wheel_tickers}
    leverage = risk.gross_beta_leverage(cycles, conc_spots, _betas, hh_nlv)

    # W3.6: Walker warnings from latest sync
    walker_warning_count = None
    walker_worst_severity = None
    try:
        row = conn.execute(
            "SELECT COUNT(*) as cnt, "
            "MAX(CASE severity WHEN 'ERROR' THEN 3 WHEN 'WARN' THEN 2 WHEN 'INFO' THEN 1 ELSE 0 END) as worst "
            "FROM walker_warnings_log "
            "WHERE sync_id = (SELECT MAX(sync_id) FROM walker_warnings_log)"
        ).fetchone()
        if row and row['cnt'] is not None:
            walker_warning_count = row['cnt']
            worst_num = row['worst'] or 0
            walker_worst_severity = {3: "ERROR", 2: "WARN", 1: "INFO"}.get(worst_num)
    except Exception:
        pass  # Table may not exist yet

    return {
        "vix": vix,
        "el_retain_pct": el_retain_pct,
        "total_nav": total_nav,
        "inception_pnl": inception_pnl,
        "inception_pnl_pct": inception_pnl_pct,
        "net_inflows": net_inflows,
        "el_current": el_current,
        "el_required": el_required,
        "vikram_el_pct": vikram_el_pct,
        "conc_ticker": conc_ticker,
        "conc_pct": conc_pct,
        "conc_hh": conc_hh,
        "sector_violations": sector_v,
        "leverage": leverage,
        "last_sync": last_sync,
        "nav_by_acct": nav_by_acct,
        "change_nav": change_nav,
        "walker_warning_count": walker_warning_count,
        "walker_worst_severity": walker_worst_severity,
        "desk_mode": _get_desk_mode(conn),
    }


def _get_desk_mode(conn) -> str:
    """Read current desk mode, defaulting to PEACETIME."""
    try:
        from agt_equities.mode_engine import get_current_mode
        return get_current_mode(conn)
    except Exception:
        return "PEACETIME"


# ── Build cycles table data ──────────────────────────────────────

def build_cycles_table() -> list[dict]:
    cycles = load_active_cycles()
    tickers = list({c.ticker for c in cycles if c.status == 'ACTIVE' and c.cycle_type == 'WHEEL'})
    spots = get_spots(tickers) if tickers else {}

    rows = []
    for c in cycles:
        if c.status != 'ACTIVE' or c.cycle_type != 'WHEEL':
            continue
        spot = spots.get(c.ticker)
        unreal_pct = None
        unreal_dollar = None
        if spot and c.paper_basis and c.shares_held > 0:
            unreal_dollar = (spot - c.paper_basis) * c.shares_held
            unreal_pct = (spot - c.paper_basis) / c.paper_basis * 100

        # Nearest DTE from open short options
        nearest_dte = None
        from datetime import date, datetime
        today = date.today()
        for ev in c.events:
            if ev.expiry and ev.asset_category == 'OPT':
                try:
                    exp_date = datetime.strptime(ev.expiry, "%Y%m%d").date()
                    dte = (exp_date - today).days
                    if dte >= 0 and (nearest_dte is None or dte < nearest_dte):
                        nearest_dte = dte
                except Exception:
                    pass

        # Current leg description
        leg = ""
        if c.open_short_puts > 0:
            leg += f"{c.open_short_puts}P"
        if c.open_short_calls > 0:
            if leg:
                leg += "+"
            leg += f"{c.open_short_calls}C"
        if not leg:
            leg = "flat"

        rows.append({
            "ticker": c.ticker,
            "household": c.household_id.replace("_Household", ""),
            "shares": int(c.shares_held),
            "paper_basis": c.paper_basis,
            "spot": spot,
            "unreal_dollar": unreal_dollar,
            "unreal_pct": unreal_pct,
            "nearest_dte": nearest_dte,
            "leg": leg,
            "cycle_seq": c.cycle_seq,
            "premium_total": c.premium_total,
            "realized_pnl": c.realized_pnl,
            "adjusted_basis": c.adjusted_basis,
            "open_short_puts": c.open_short_puts,
            "open_short_calls": c.open_short_calls,
            "event_count": len(c.events),
        })

    # Sort: nearest DTE asc, then unreal % asc
    rows.sort(key=lambda r: (
        r["nearest_dte"] if r["nearest_dte"] is not None else 999,
        r["unreal_pct"] if r["unreal_pct"] is not None else 0,
    ))
    return rows


# ── Underwater Positions helper (shared by command_deck + cure_console) ──

ATTENTION_MIN_LOSS_DOLLAR = 1500


def _build_underwater_rows(cycles: list) -> list[dict]:
    """Filter + sort attention rows for Underwater Positions panel.

    Filter: DTE <= 5 OR (loss > 15% AND |$loss| >= $1500).
    Enriches each row with has_cc (cycle.open_short_calls > 0).
    Sort: unreal_pct ascending (biggest loss first), household alpha tiebreak.
    """
    attention = [
        r for r in cycles
        if (r["nearest_dte"] is not None and r["nearest_dte"] <= 5)
        or (r["unreal_pct"] is not None and r["unreal_pct"] < -15
            and r["unreal_dollar"] is not None
            and abs(r["unreal_dollar"]) >= ATTENTION_MIN_LOSS_DOLLAR)
    ]
    for a in attention:
        a["has_cc"] = a.get("open_short_calls", 0) > 0
    attention.sort(key=lambda r: (
        r["unreal_pct"] if r["unreal_pct"] is not None else 0,
        r.get("household", ""),
    ))
    return attention


def _group_underwater_by_household(rows: list) -> list[tuple]:
    """Group underwater rows by household, alpha order. Empty households omitted."""
    from collections import OrderedDict
    groups: OrderedDict = OrderedDict()
    for r in rows:
        hh = r.get("household", "Unknown")
        groups.setdefault(hh, []).append(r)
    return sorted(groups.items())


# ── Routes ────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def command_deck(request: Request):
    conn = get_ro_conn()
    try:
        top = build_top_strip(conn)
        cycles = build_cycles_table()
        fills = queries.get_recent_fills(conn)
        recon = queries.get_recon_summary(conn)
        recent_orders = queries.get_recent_orders(conn)

        attention = _build_underwater_rows(cycles)
        attention_grouped = _group_underwater_by_household(attention)

        # Account pills
        nav_by_acct = top["nav_by_acct"]
        pills = []
        for acct in queries.ACCOUNT_ALIAS:  # Sprint 1E: derive from shared map
            pills.append({
                "account_id": acct,
                "alias": queries.ACCOUNT_ALIAS.get(acct, acct),
                "nav": nav_by_acct.get(acct),
                "el_pct": None,  # deferred to tonight
            })

        return _render("command_deck.html", {
            "request": request,
            "top": top,
            "cycles": cycles,
            "fills": fills,
            "recon": recon,
            "attention": attention,
            "attention_grouped": attention_grouped,
            "pills": pills,
            "token": request.query_params.get("t", ""),
            "ACCOUNT_ALIAS": queries.ACCOUNT_ALIAS,
            "recent_orders": recent_orders,
        })
    finally:
        conn.close()


@app.get("/sse")
async def sse(request: Request):
    """Server-sent events for live updates."""
    async def event_stream():
        while True:
            await asyncio.sleep(30)
            try:
                conn = get_ro_conn()
                try:
                    top = build_top_strip(conn)
                    cycles = build_cycles_table()
                    fills = queries.get_recent_fills(conn, limit=5)
                finally:
                    conn.close()

                data = {
                    "total_nav": formatters.money(top["total_nav"]),
                    "inception_pnl": formatters.money(top["inception_pnl"], plus=True),
                    "inception_pnl_pct": formatters.pct(top["inception_pnl_pct"], plus=True),
                    "vix": f"{top['vix']:.1f}" if top["vix"] else "—",
                    "cycle_count": len(cycles),
                    "last_sync": formatters.time_ago(
                        top["last_sync"]["finished_at"] if top["last_sync"] else None
                    ),
                }
                yield f"event: topstrip\ndata: {json.dumps(data)}\n\n"
            except Exception as exc:
                logger.warning("SSE update failed: %s", exc)
                yield f"event: error\ndata: {json.dumps({'error': str(exc)})}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Cure Console ─────────────────────────────────────────────────

def _build_household_el(top: dict, hh_nlv: dict) -> dict:
    """Sprint 1F Fix 3C: build per-household EL dict from top strip EL data."""
    result = {}
    for hh in hh_nlv:
        if "Yash" in hh:
            result[hh] = top.get("el_current")
        elif "Vikram" in hh:
            # Compute Vikram EL from vikram_el_pct + NLV
            pct = top.get("vikram_el_pct")
            nlv = hh_nlv.get(hh, 0)
            result[hh] = round(pct / 100 * nlv, 2) if pct and nlv else None
        else:
            result[hh] = None
    return result


def _build_cure_data(conn) -> dict:
    """Assemble Cure Console data from rule engine + glide paths."""
    from agt_equities.rule_engine import PortfolioState, evaluate_all, compute_leverage_pure
    from agt_equities.mode_engine import (
        get_current_mode, load_glide_paths, evaluate_glide_path,
        get_recent_transitions,
    )
    from datetime import date as _date

    top = build_top_strip(conn)
    cycles_raw = load_active_cycles()

    # Build PortfolioState
    hh_nlv = {}
    for acct, hh in queries.HOUSEHOLD_MAP.items():
        hh_nlv.setdefault(hh, 0)
        hh_nlv[hh] += top["nav_by_acct"].get(acct, 0)

    wheel_tickers = list({c.ticker for c in cycles_raw if c.status == 'ACTIVE'})
    spots = get_spots(wheel_tickers) if wheel_tickers else {}

    # Sector overrides
    sector_overrides = {}
    try:
        for r in conn.execute("SELECT ticker, sector FROM sector_overrides").fetchall():
            sector_overrides[r["ticker"]] = r["sector"]
    except Exception:
        pass

    industries = queries.get_ticker_industries(conn)

    ps = PortfolioState(
        household_nlv=hh_nlv,
        household_el=_build_household_el(top, hh_nlv),  # Sprint 1F Fix 3C
        active_cycles=cycles_raw,
        spots=spots,
        betas=get_betas(list(spots.keys())) if spots else {},  # Sprint 1F: cached yfinance betas
        industries=industries,
        sector_overrides=sector_overrides,
        vix=top.get("vix"),
        report_date=_date.today().strftime("%Y%m%d"),
    )

    # Sprint 1F Fix 4: load glide paths early for eval softening
    glide_paths_raw = load_glide_paths(conn)
    _paused_rules: dict[tuple[str, str, str | None], str] = {}
    for gp in glide_paths_raw:
        if gp.pause_conditions:
            try:
                pc = json.loads(gp.pause_conditions)
                if pc.get("paused"):
                    _paused_rules[(gp.household_id, gp.rule_id, gp.ticker)] = pc.get("reason", "paused")
            except Exception:
                pass

    # Evaluate all rules per household
    households = sorted(hh_nlv.keys())
    all_evals = []
    hh_sections = {}
    for hh in households:
        evals = evaluate_all(ps, hh, conn=conn)  # Sprint B: conn enables real R9 compositor

        # Sprint 1F Fix 4: soften paused evals → GREEN before rendering + R9
        for ev in evals:
            pause_key = (hh, ev.rule_id, ev.ticker)
            pause_reason = _paused_rules.get(pause_key)
            if pause_reason and ev.status in ("RED", "AMBER"):
                ev.status = "GREEN"
                ev.message = f"{ev.message} [paused: {pause_reason}]"

        # Sprint B: R9 compositor now wired directly in evaluate_all(conn=conn).
        # Sprint 1F Fix 4 duplicate wiring removed.

        all_evals.extend(evals)

        lev = compute_leverage_pure(cycles_raw, spots, ps.betas, hh_nlv, hh)

        # Per-ticker concentrations
        concs = []
        for c in cycles_raw:
            if c.status == 'ACTIVE' and c.shares_held > 0 and c.household_id == hh:
                price = spots.get(c.ticker) or c.paper_basis or 0
                pct = (c.shares_held * price / hh_nlv.get(hh, 1)) * 100
                concs.append({"ticker": c.ticker, "pct": round(pct, 1),
                              "shares": c.shares_held, "spot": price})
        concs.sort(key=lambda x: -x["pct"])

        hh_sections[hh] = {
            "nlv": hh_nlv.get(hh, 0),
            "leverage": round(lev, 4),
            "evals": evals,
            "concentrations": concs,
            "active_cycles": sum(1 for c in cycles_raw
                                 if c.status == 'ACTIVE' and c.household_id == hh),
        }

    # Glide paths with progress (uses glide_paths_raw loaded above for Fix 4 softening)
    today_str = _date.today().isoformat()
    glide_rows = []
    for gp in glide_paths_raw:
        # Find matching actual value from evaluations
        actual = gp.baseline_value  # fallback
        for ev in all_evals:
            if (ev.rule_id == gp.rule_id and ev.household == gp.household_id
                    and ev.ticker == gp.ticker and ev.raw_value is not None):
                actual = ev.raw_value
                break
        status, expected, delta = evaluate_glide_path(gp, actual, today_str)
        total_days = max(1, (_date.fromisoformat(gp.target_date) - _date.fromisoformat(gp.start_date)).days)
        elapsed = max(0, (_date.today() - _date.fromisoformat(gp.start_date)).days)
        progress_pct = min(100, elapsed / total_days * 100)
        days_remaining = max(0, total_days - elapsed)

        # Check if paused
        is_paused = False
        pause_reason = None
        if gp.pause_conditions:
            try:
                pc = json.loads(gp.pause_conditions)
                if pc.get("paused"):
                    is_paused = True
                    pause_reason = pc.get("reason", "paused")
            except Exception:
                pass

        glide_rows.append({
            "household": gp.household_id,
            "rule_id": gp.rule_id,
            "ticker": gp.ticker,
            "baseline": gp.baseline_value,
            "target": gp.target_value,
            "actual": round(actual, 2),
            "expected": round(expected, 4),
            "delta": round(delta, 4),
            "status": status,
            "progress_pct": round(progress_pct, 1),
            "days_elapsed": elapsed,
            "days_remaining": days_remaining,
            "is_paused": is_paused,
            "pause_reason": pause_reason,
        })

    mode = get_current_mode(conn)
    transitions = get_recent_transitions(conn)

    try:
        staged_exits = queries.get_staged_dynamic_exits(conn)
    except Exception as e:
        logger.warning("_build_cure_data: get_staged_dynamic_exits failed: %s", e)
        staged_exits = []

    # G2: Underwater Positions for Cure Console (uses build_cycles_table for dict rows)
    cycles_for_underwater = build_cycles_table()
    underwater = _build_underwater_rows(cycles_for_underwater)
    underwater_grouped = _group_underwater_by_household(underwater)

    return {
        "mode": mode,
        "households": hh_sections,
        "glide_paths": glide_rows,
        "all_evals": all_evals,
        "transitions": transitions,
        "top": top,
        "staged_exits": staged_exits,
        "underwater": underwater,
        "underwater_grouped": underwater_grouped,
    }


@app.get("/cure", response_class=HTMLResponse)
async def cure_console(request: Request):
    """Cure Console — per-household rule evaluations + glide path progress."""
    conn = get_ro_conn()
    try:
        cure = _build_cure_data(conn)
        return _render("cure_console.html", {
            "request": request,
            "token": request.query_params.get("t", ""),
            **cure,
        })
    finally:
        conn.close()


@app.get("/api/cure", response_class=HTMLResponse)
async def cure_console_partial(request: Request):
    """HTMX partial update for Cure Console body."""
    conn = get_ro_conn()
    try:
        cure = _build_cure_data(conn)
        return _render("cure_partial.html", {
            "request": request,
            "token": request.query_params.get("t", ""),
            **cure,
        })
    finally:
        conn.close()


@app.get("/api/cure/empty", response_class=HTMLResponse)
async def cure_empty(request: Request):
    """Return empty HTML — used by modal close button to clear #modal-root."""
    return HTMLResponse("")


@app.get("/api/cure/dynamic_exit/{audit_id}/attest", response_class=HTMLResponse)
async def smart_friction_modal(request: Request, audit_id: str):
    """Render the Smart Friction attestation modal for a STAGED dynamic exit row."""
    token = request.query_params.get("t", "")
    conn = get_ro_conn()
    try:
        row = queries.get_staged_exit_by_audit_id(conn, audit_id)
    except Exception as e:
        logger.warning("smart_friction_modal: query failed for %s: %s", audit_id, e)
        row = None
    finally:
        conn.close()

    if not row:
        return HTMLResponse(
            '<div class="bg-rose-900/40 border border-rose-700 text-rose-200 p-4 rounded-lg">'
            "Staging row not found or no longer STAGED. Refresh Cure Console."
            "</div>",
            status_code=404,
        )

    loss_whole = round(row.get("gate1_realized_loss") or 0)
    return _render("cure_smart_friction.html", {
        "row": row,
        "loss_whole": loss_whole,
        "token": token,
    })


def _htmx_error(html: str, status_code: int) -> HTMLResponse:
    """Error response that HTMX will swap into target despite 4xx/5xx status."""
    return HTMLResponse(
        content=html,
        status_code=status_code,
        headers={"HX-Reswap": "innerHTML"},
    )


@app.post("/api/cure/dynamic_exit/{audit_id}/attest", response_class=HTMLResponse)
async def smart_friction_submit(request: Request, audit_id: str):
    """POST handler for Smart Friction attestation. Transitions STAGED → ATTESTED."""
    # ── Parse form BEFORE acquiring DB lock (no await while holding conn) ──
    try:
        form = await request.form()
    except Exception as exc:
        logger.warning("smart_friction_submit: form parse failed for %s: %s", audit_id, exc)
        return _htmx_error(
            '<div class="bg-rose-900/40 border border-rose-700 text-rose-200 p-4 rounded-lg">'
            "MALFORMED_FORM: could not parse request body. Retry."
            "</div>",
            422,
        )

    conn = get_rw_conn()
    try:
        # ── Read staged row ──────────────────────────────────────
        row = queries.get_staged_exit_by_audit_id(conn, audit_id)
        if not row:
            conn.rollback()
            logger.info("ROW_NOT_STAGED: audit_id=%s", audit_id)
            return _htmx_error(
                '<div class="bg-rose-900/40 border border-rose-700 text-rose-200 p-4 rounded-lg">'
                "Row not found or no longer STAGED. Refresh Cure Console."
                "</div>",
                409,
            )

        # ── JIT check 1: mode drift ─────────────────────────────
        live_mode = _get_desk_mode(conn)
        if live_mode != row["desk_mode"]:
            conn.rollback()
            logger.warning("MODE_CHANGED: audit_id=%s staged_mode=%s live_mode=%s",
                           audit_id, row["desk_mode"], live_mode)
            return _htmx_error(
                '<div class="bg-rose-900/40 border border-rose-700 text-rose-200 p-4 rounded-lg">'
                f'Mode changed since staging: was {row["desk_mode"]}, now {live_mode}. '
                "Re-stage from Cure Console."
                "</div>",
                409,
            )

        # ── Validate form fields ─────────────────────────────────
        desk_mode = row["desk_mode"]
        loss_whole = round(row.get("gate1_realized_loss") or 0)

        if desk_mode == "WARTIME":
            # ── WARTIME: Integer Lock validation ─────────────────
            realized_loss = row.get("gate1_realized_loss") or 0
            if realized_loss < 0:
                conn.rollback()
                logger.warning(
                    "ANOMALY: WARTIME attestation on profitable exit audit_id=%s loss=%s",
                    audit_id, realized_loss,
                )
                return _htmx_error(
                    '<div class="bg-rose-900/40 border border-rose-700 text-rose-200 p-4 rounded-lg">'
                    "INVALID_WARTIME_PROFIT: Dynamic Exit on profitable trade should not "
                    "reach WARTIME attestation. Contact Architect."
                    "</div>",
                    422,
                )

            # Determine expected value: integer or ticker fallback
            if loss_whole <= 1:
                expected_value = row["ticker"]  # case-sensitive exact match
            else:
                expected_value = str(loss_whole)

            typed_value = form.get("attestation_value_typed", "")
            if typed_value != expected_value:
                conn.rollback()
                logger.warning("INTEGER_LOCK_FAIL: audit_id=%s ticker=%s", audit_id, row["ticker"])
                return _htmx_error(
                    '<div class="bg-amber-900/40 border border-amber-700 text-amber-200 p-4 rounded-lg">'
                    "INTEGER_LOCK_FAIL: typed value does not match expected. "
                    "Close and retry."
                    "</div>",
                    422,
                )

            operator_thesis = None
            checkbox_json = None
            attestation_typed = typed_value

        else:
            # ── PEACETIME: checkbox + thesis validation ──────────
            ack_loss = form.get("ack_loss")
            ack_cure = form.get("ack_cure")
            thesis_raw = form.get("operator_thesis", "")
            operator_thesis = thesis_raw.strip() if thesis_raw else ""

            if ack_loss != "on" or ack_cure != "on":
                conn.rollback()
                missing = [k for k, v in [("ack_loss", ack_loss), ("ack_cure", ack_cure)] if v != "on"]
                logger.info("ACK_MISSING: audit_id=%s missing=%s", audit_id, ",".join(missing))
                return _htmx_error(
                    '<div class="bg-amber-900/40 border border-amber-700 text-amber-200 p-4 rounded-lg">'
                    "Both acknowledgment checkboxes are required."
                    "</div>",
                    422,
                )

            if len(operator_thesis) < 30:
                conn.rollback()
                logger.info("THESIS_TOO_SHORT: audit_id=%s length=%d", audit_id, len(operator_thesis))
                exc_type = row.get("exception_type") or ""
                if exc_type == "thesis_deterioration":
                    thesis_label = "Bearish rationale must be at least 30 characters."
                elif exc_type == "emergency_risk_event":
                    thesis_label = "Risk catalyst must be at least 30 characters."
                else:
                    thesis_label = "Strategic rationale must be at least 30 characters."
                return _htmx_error(
                    '<div class="bg-amber-900/40 border border-amber-700 text-amber-200 p-4 rounded-lg">'
                    f"{thesis_label}"
                    "</div>",
                    422,
                )

            checkbox_json = json.dumps({
                "ack_loss": True,
                "ack_cure": True,
                "ack_ts": time.time(),
            })
            attestation_typed = None

        # ── Write: STAGED → ATTESTED ─────────────────────────────
        attested_limit_price = row.get("limit_price")
        rowcount = queries.attest_staged_exit(
            conn,
            audit_id=audit_id,
            operator_thesis=operator_thesis,
            attestation_value_typed=attestation_typed,
            checkbox_state_json=checkbox_json,
            attested_limit_price=attested_limit_price,
            expected_desk_mode=row["desk_mode"],
        )
        if rowcount == 0:
            conn.rollback()
            logger.warning("STALE_ROW: audit_id=%s", audit_id)
            return _htmx_error(
                '<div class="bg-rose-900/40 border border-rose-700 text-rose-200 p-4 rounded-lg">'
                "STALE_ROW: row changed between read and write. Refresh Cure Console."
                "</div>",
                409,
            )

        conn.commit()
        logger.info("Attested audit_id=%s ticker=%s household=%s mode=%s",
                     audit_id, row["ticker"], row["household"], desk_mode)

        return _render("cure_attest_success.html", {
            "row": row,
            "audit_id": audit_id,
            "attested_limit_price": attested_limit_price,
            "token": request.query_params.get("t", ""),
        })

    except Exception as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.exception("smart_friction_submit failed for audit_id=%s: %s", audit_id, exc)
        return _htmx_error(
            '<div class="bg-rose-900/40 border border-rose-700 text-rose-200 p-4 rounded-lg">'
            "Server error during attestation. Check logs and retry."
            "</div>",
            500,
        )
    finally:
        conn.close()


# ── Beta Impl 5: R5 Sell Gate staging route ──────────────────────

_VALID_EXCEPTION_TYPES = {
    "thesis_deterioration",
    "rule_6_forced_liquidation",
    "emergency_risk_event",
}


@app.post("/api/cure/r5_sell/stage", response_class=HTMLResponse)
async def r5_sell_stage(request: Request):
    """Stage an R5 stock sale via Smart Friction.

    Accepts {ticker, household, shares, limit_price, adjusted_cost_basis,
    exception_type, desk_mode, household_nlv, spot}. Delegates to
    rule_engine.stage_stock_sale_via_smart_friction().
    """
    try:
        form = await request.form()
    except Exception:
        return _htmx_error(
            '<div class="bg-rose-900/40 border border-rose-700 text-rose-200 p-4 rounded-lg">'
            "Invalid form data.</div>",
            400,
        )

    exception_type = form.get("exception_type", "")
    if exception_type not in _VALID_EXCEPTION_TYPES:
        return _htmx_error(
            '<div class="bg-rose-900/40 border border-rose-700 text-rose-200 p-4 rounded-lg">'
            f"Invalid exception_type: {exception_type}. "
            f"Valid: {', '.join(sorted(_VALID_EXCEPTION_TYPES))}</div>",
            400,
        )

    ticker = form.get("ticker", "").strip().upper()
    household = form.get("household", "").strip()
    if not ticker or not household:
        return _htmx_error(
            '<div class="bg-rose-900/40 border border-rose-700 text-rose-200 p-4 rounded-lg">'
            "Missing ticker or household.</div>",
            400,
        )

    try:
        shares = int(form.get("shares", 0))
        limit_price = float(form.get("limit_price", 0))
        adjusted_cost_basis = float(form.get("adjusted_cost_basis", 0))
        household_nlv = float(form.get("household_nlv", 0))
        spot = float(form.get("spot", 0))
    except (ValueError, TypeError) as e:
        return _htmx_error(
            '<div class="bg-rose-900/40 border border-rose-700 text-rose-200 p-4 rounded-lg">'
            f"Invalid numeric field: {e}</div>",
            400,
        )

    desk_mode = form.get("desk_mode", "PEACETIME")
    logged_rationale = form.get("logged_rationale", "").strip() or None

    # R6 forced liquidation requires WARTIME
    if exception_type == "rule_6_forced_liquidation" and desk_mode != "WARTIME":
        return _htmx_error(
            '<div class="bg-rose-900/40 border border-rose-700 text-rose-200 p-4 rounded-lg">'
            "Forced Liquidation requires WARTIME mode.</div>",
            400,
        )

    from agt_equities.rule_engine import (
        stage_stock_sale_via_smart_friction, SellException,
    )

    exception_flag = SellException(exception_type)

    conn = get_rw_conn()
    try:
        result = stage_stock_sale_via_smart_friction(
            ticker=ticker,
            household=household,
            limit_price=limit_price,
            shares=shares,
            adjusted_cost_basis=adjusted_cost_basis,
            exception_flag=exception_flag,
            household_nlv=household_nlv,
            spot=spot,
            desk_mode=desk_mode,
            conn=conn,
            cio_token=True,  # Attestation IS the CIO token (ADR-004)
            logged_rationale=logged_rationale,
            vikram_el_below_10=(exception_type == "rule_6_forced_liquidation"),
        )

        if not result.staged:
            return _htmx_error(
                '<div class="bg-rose-900/40 border border-rose-700 text-rose-200 p-4 rounded-lg">'
                f"R5 gate blocked: {result.reason}</div>",
                409,
            )

        logger.info(
            "R5_STAGED: audit_id=%s ticker=%s household=%s exception=%s shares=%d",
            result.audit_id, ticker, household, exception_type, shares,
        )

        return HTMLResponse(
            f'<div class="bg-emerald-900/40 border border-emerald-700 text-emerald-200 p-4 rounded-lg">'
            f'<div class="font-semibold mb-1">R5 Sell Staged</div>'
            f'<div class="text-xs text-emerald-300">{ticker} · {shares} shares · {exception_type.replace("_", " ").title()}</div>'
            f'<div class="text-xs text-slate-400 mt-1">audit_id: {result.audit_id[:8]}...</div>'
            f'<div class="text-xs text-slate-500 mt-2">Telegram TRANSMIT/CANCEL keyboard will appear within 10 seconds.</div>'
            f"</div>"
        )

    except Exception as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.exception("r5_sell_stage failed: %s", exc)
        return _htmx_error(
            '<div class="bg-rose-900/40 border border-rose-700 text-rose-200 p-4 rounded-lg">'
            "Server error during R5 staging. Check logs.</div>",
            500,
        )
    finally:
        conn.close()


# ── Sprint 1B: Lifecycle Queue + Health Strip fragments ──────────

@app.get("/cure/lifecycle", response_class=HTMLResponse)
async def cure_lifecycle(request: Request):
    """HTMX fragment: Action Queue with STAGED/ATTESTED/TRANSMITTING/TRANSMITTED rows."""
    conn = get_ro_conn()
    try:
        rows = queries.get_lifecycle_rows(conn)
        orphans = [r for r in rows if r.get("is_orphan")]
        return _render("cure_lifecycle.html", {
            "request": request,
            "token": request.query_params.get("t", ""),
            "rows": rows,
            "orphans": orphans,
        })
    finally:
        conn.close()


@app.get("/cure/health_strip", response_class=HTMLResponse)
async def cure_health_strip(request: Request):
    """HTMX fragment: Health Strip with per-account EL and mode badge."""
    conn = get_ro_conn()
    try:
        data = queries.get_health_strip_data(conn)
        return _render("cure_health_strip.html", {
            "request": request,
            "token": request.query_params.get("t", ""),
            **data,
        })
    finally:
        conn.close()


# ── Entry point ───────────────────────────────────────────────────

def main():
    global DECK_TOKEN
    import uvicorn
    if not DECK_TOKEN:
        import secrets
        token = secrets.token_urlsafe(32)
        os.environ["AGT_DECK_TOKEN"] = token
        DECK_TOKEN = token
        print(f"\n  Generated auth token: {token}")
        print(f"  Access: http://127.0.0.1:8787/?t={token}\n")
    else:
        print(f"\n  Access: http://127.0.0.1:8787/?t={DECK_TOKEN}\n")

    # Write active token to .deck_token for launcher discovery (atomic)
    import tempfile
    _token_path = Path(__file__).resolve().parent.parent / ".deck_token"
    try:
        fd, tmp = tempfile.mkstemp(dir=str(_token_path.parent), prefix=".deck_token_")
        with os.fdopen(fd, "w") as f:
            f.write(DECK_TOKEN)
        os.replace(tmp, str(_token_path))
        logger.info(".deck_token written for launcher discovery")
    except Exception as exc:
        logger.warning("Failed to write .deck_token: %s", exc)

    # Bind 0.0.0.0 for Tailscale mobile access (token auth protects all routes)
    uvicorn.run(app, host="0.0.0.0", port=8787, log_level="info")


if __name__ == "__main__":
    main()
