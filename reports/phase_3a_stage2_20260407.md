# Phase 3A Stage 2 Implementation Report — Cure Console UI

**Date:** 2026-04-07
**Author:** Coder (Claude Code)
**Status:** COMPLETE — awaiting Yash review before Stage 3
**Tests:** 156/156 (unchanged — UI changes, no new test targets)
**Runtime:** 15.24s

---

## Files Created/Changed

| File | Change |
|------|--------|
| `agt_deck/main.py` | Added `_build_cure_data()`, `/cure` route, `/api/cure` HTMX partial route, `_get_desk_mode()` helper, `desk_mode` in `build_top_strip()`, bind changed to `0.0.0.0` |
| `agt_deck/templates/cure_console.html` | **NEW** — full Cure Console page with top strip, HTMX 60s refresh |
| `agt_deck/templates/cure_partial.html` | **NEW** — HTMX-refreshable body (glide paths, per-household sections, mode transitions) |
| `agt_deck/templates/command_deck.html` | Added Mode Badge (PEACE/AMBER/WAR) between Warn and Sync. Linkified Lev cells to `/cure` |

---

## UI Components Delivered

### 1. Mode Badge (Top Strip)

Between Warn and Sync on both Command Deck and Cure Console:

```
[Warn: 0] [Mode: PEACE] [Sync: 3h ago]
```

Color mapping:
- PEACETIME: emerald-400 `PEACE`
- AMBER: amber-400 `AMBER`
- WARTIME: rose-400 `WAR` with `animate-pulse`

Clickable — links to `/cure?t=<token>`.

### 2. Lev Cells Linkified

Lev values in the Command Deck top strip are now `<a>` tags linking to `/cure?t=<token>`. Visual appearance unchanged (same Tailwind classes).

### 3. Cure Console (`/cure`)

Mobile-first layout with Tailwind responsive classes. Stacks vertically on narrow screens.

**Sections:**

**A. Glide Paths** — 2-column grid (1-col on mobile). Per-path card with:
- Household/Rule/Ticker header
- Status pill (GREEN/AMBER/RED/PAUSED) — rounded-full colored badges
- Progress bar (% of journey complete) — color matches status
- 4-metric grid: Baseline | Target | Expected | Actual
- Days elapsed / remaining
- Pause indicator when active

**B. Per-Household Sections** — one per household with:
- Header: NAV, Leverage, Active cycles count
- Concentration bar chart: horizontal bars with 20% limit marker, color-coded
- Rule evaluations table: Rule | Ticker | Value | Status dot | Message
- Responsive: Ticker column hidden on mobile, Message hidden on small screens

**C. Mode Transitions** — last 5 transitions with timestamp, old→new, trigger rule

### 4. HTMX Auto-Refresh

```html
<main id="cure-body" hx-get="/api/cure?t=<token>" hx-trigger="every 60s" hx-swap="innerHTML">
```

The `/api/cure` endpoint returns just the `cure_partial.html` content (no full page wrapper). The main `cure_console.html` wraps it with header + HTMX trigger.

### 5. Tailscale Bind

```python
# Before:
uvicorn.run(app, host="127.0.0.1", port=8787, log_level="info")

# After:
uvicorn.run(app, host="0.0.0.0", port=8787, log_level="info")
```

Token auth (`AGT_DECK_TOKEN`) protects all routes. No DNS, no reverse proxy.

---

## Live Data Rendering (ASCII equivalent)

### Top Strip (Command Deck)

```
AGT | VIX — | NAV $342,689 | Inception +$X | EL — | Vik EL — | Conc ADBE/Vikram 60.5% | Lev Y 1.59x V 2.15x | Sector SW-App: ADBE, CRM | Warn 0 | Mode PEACE | Sync 3h ago
```

### Glide Paths Section

```
Yash/R11/—     [..............................] 0.0%  GREEN
  Baseline: 1.60  Target: 1.50  Expected: 1.60  Actual: 1.59  28d left

Vikram/R11/—   [..............................] 0.0%  GREEN
  Baseline: 2.17  Target: 1.50  Expected: 2.17  Actual: 2.15  84d left

Yash/R1/ADBE   [..............................] 0.0%  GREEN
  Baseline: 46.70  Target: 25.00  Expected: 46.70  Actual: 45.95  140d left

Yash/R1/PYPL   [..............................] 0.0%  GREEN PAUSED
  Baseline: 39.90  Target: 25.00  Paused: earnings-gated
```

### Vikram Concentration Bars

```
ADBE  ######################                 59.6% [R]
MSFT  ##################                     45.9% [R]
PYPL  #################                      44.9% [R]
UBER  ##########                             26.7% [R]
CRM   #########                              22.8% [R]
QCOM  ######                                 15.3% [G]
      ─────────────────────|──────────────────────
                          20% limit
```

### Rule 3 (Sector) — UBER Override Verified

```
Software - Application: 2 names (ADBE, CRM) [G]  ← was 3 before UBER override
Consumer Cyclical: 1 names (UBER) [G]              ← UBER now correctly classified
```

---

## Key Verifications

| Check | Result |
|-------|--------|
| Mode computes PEACETIME on Day 1 | **YES** — all glide paths GREEN |
| UBER sector override works | **YES** — SW-App down to 2 names |
| PYPL glide paths show PAUSED | **YES** — earnings-gated |
| Actual values ≤ baseline on Day 0 | **YES** — slight market improvement |
| Template renders against live DB | **YES** — 59K chars HTML, all assertions pass |
| HTMX 60s refresh wired | **YES** — `hx-get="/api/cure" hx-trigger="every 60s"` |
| Lev cells link to /cure | **YES** — `<a href="/cure?t=...">` |
| Mobile-responsive | **YES** — Tailwind `grid-cols-1 lg:grid-cols-2`, hidden columns on sm/md |
| 0.0.0.0 bind for Tailscale | **YES** — line 511 of main.py |
| Token auth on /cure and /api/cure | **YES** — inherited from middleware |

---

## Backward Compatibility

- `/` (Command Deck): unchanged content, added Mode Badge + Lev links
- `/reconcile`: unchanged
- `/exit_math`: N/A (never implemented — merge into Cure Console deferred to later)
- `/orders`: unchanged
- SSE endpoint: unchanged

---

## Followups (Stage 3)

- Telegram commands: `/declare_wartime`, `/declare_peacetime`, `/mode`, `/cure`
- Mode transition push alerts
- CSP blocker via mode engine (AMBER blocks `/scan`, WARTIME blocks all except Cure)
- Update existing Rule 11 blocker to use mode engine

---

**STOP. Awaiting Yash review before Stage 3.**
