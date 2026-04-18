# ADR-010 — CSP Approval Digest (live Telegram approval gate)

**Status:** Draft (Phase 1 boundaries locked; Phase 2/3 deferred)
**Date:** 2026-04-18
**Author:** Architect (Cowork, Opus)
**Related:** ADR-008 (Shadow Scan, closes via MR 6a), MR !69 (CSPCandidate Protocol + approval_gate seam, `60f65be9`)
**Supersedes:** the "live CSP approval digest" tail of `project_end_state_vision.md` (memory) — that vision is now decomposed into this ADR's three phases.

---

## 1. Problem

Per `project_end_state_vision.md`:

> **Live (final production state):** Everything autonomous EXCEPT CSP selection. The bot sends Yash a Telegram digest with: today's top news + key market figures, 3–5 CSP setups ranked, reasoning per setup. Yash approves which of the 3–5 get allocated. Allocation + staging + execution remain autonomous post-approval.

MR !69 (`60f65be9`, 2026-04-16) shipped the structural seam: `run_csp_allocator(..., approval_gate: Callable[[list[CSPCandidate]], list[CSPCandidate]] = identity)`. Today every call site passes the identity gate — paper, live, dev, scheduler. Live CSP entries flow through the allocator → pending_orders → IB Gateway with zero human review.

We need to wire a non-identity gate for the live path, with a Telegram surface Yash interacts with from his phone, that:

1. Holds a candidate batch in a pending state until Yash decides
2. Survives bot restarts mid-decision (state must be on disk, not in process memory)
3. Times out conservatively (no candidate sits forever; auto-reject is fail-closed for live capital)
4. Doesn't block the allocator's other concerns (mode gates, household routing, dedup)
5. Composes with the existing seam — not a rewrite

The "right ADR-010" must also resist scope creep: the end-state vision bundles state machine + LLM ranking + news ingestion + multi-day reasoning into a single "digest." Bundling those at v1 inflates blast radius and pollutes the debug signal of any single piece. Per the MR !69 seam memory: **"Do NOT build the LLM digest tool during the allocator sprint — that's a separate ticket."** Same discipline applies here: do not build LLM during the state machine sprint.

---

## 2. Rejected alternatives

### 2.1 Single MR shipping the full end-state

Bundle approval state machine + LLM ranking + news feed + multi-day reasoning into one ~1400 LOC MR. Recon estimate: 850–1400 LOC.

**Rejected on:**
- Blast radius. A bug in the LLM prompt poisons the approval-gate test signal — we wouldn't know if a misbehavior was state machine, prompt, news pull, or callback routing.
- Multi-week ship under a single banner — violates the chunk-and-ship cadence.
- Live-capital exposure. Phase 1 alone touches the allocator's gate path; layering LLM on top compounds the risk surface before Phase 1 has produced operator data.

### 2.2 Inline approval (no state machine, in-memory pending)

Allocator constructs the digest, sends to Telegram, polls for callback inline (blocking), returns the approved subset.

**Rejected on:**
- Bot restart loses pending decisions — Yash taps approve, restart eats it, allocator times out, candidate rejected.
- Scheduler runs allocator on a clock; an inline-blocking gate stalls the allocator past its window.
- No audit trail for "which candidates were shown, when, what did Yash do" — just process memory.

### 2.3 Reuse `pending_orders` with a new pre-stage status

Add `'awaiting_approval'` status to `pending_orders.status` enum. Allocator writes rows in awaiting_approval state; on Yash's tap, status flips to staged.

**Rejected on:**
- Conflates two domains. `pending_orders` is the IB-bound staging queue; an approval queue is an allocator-level concern, upstream of staging. Mixing them couples the auto-executor's status machine to a Telegram workflow.
- `pending_orders` already has 9+ statuses; adding more makes the state diagram even harder to reason about.
- The auto-executor's safety queries (Rule 7 dedup, Rule 1 NLV check) would have to start filtering out 'awaiting_approval' rows — extra surface for accidental skip-or-include bugs.

### 2.4 Read-only digest with no approval (notification only)

Send Yash the ranked candidates via Telegram; allocator proceeds autonomously regardless. Yash uses /halt if he disagrees.

**Rejected on:**
- Doesn't match the end-state vision. The end state is approval-gated, not notification-only.
- /halt is a blunt instrument — kills all engines, not just one CSP batch.
- Provides no on-the-record approval audit trail.

---

## 3. Decision

Adopt a three-phase migration. Each phase is independently mergeable, each closes a specific risk surface, and each builds on the prior phase without rework.

### 3.1 Phase 1 — Approval state machine + bare digest (no LLM)

**Scope:** ~300 LOC per recon. Ships first.

**Components:**

1. New table `csp_pending_approval`:
   ```sql
   CREATE TABLE csp_pending_approval (
       id INTEGER PRIMARY KEY AUTOINCREMENT,
       run_id TEXT NOT NULL,           -- correlates with allocator run
       household_id TEXT NOT NULL,
       candidates_json TEXT NOT NULL,   -- list of {ticker,strike,expiry,annualized_yield,mid}
       sent_at_utc TEXT NOT NULL,
       timeout_at_utc TEXT NOT NULL,    -- sent_at + 30 min
       telegram_message_id INTEGER,     -- for callback routing
       status TEXT NOT NULL DEFAULT 'pending',  -- pending | approved | rejected | timeout | error
       approved_indices_json TEXT,      -- list[int] indices into candidates_json
       resolved_at_utc TEXT,
       resolved_by TEXT                 -- 'yash' | 'timeout' | 'system'
   );
   CREATE INDEX idx_csp_pending_approval_status ON csp_pending_approval(status);
   CREATE INDEX idx_csp_pending_approval_telegram_msg ON csp_pending_approval(telegram_message_id);
   ```

2. New gate function `telegram_approval_gate(candidates: list[CSPCandidate]) -> list[CSPCandidate]`:
   - Insert row into `csp_pending_approval` with `status='pending'`, candidates serialized.
   - Render bare digest: per-candidate bullet `<ticker> <strike>P {expiry} ${mid:.2f} ({annualized_yield:.1%}/yr) [{household}]`.
   - Build inline keyboard: per-candidate ✅ / ⏭ buttons (callback_data = `f"approve:{row_id}:{idx}"` / `f"skip:{row_id}:{idx}"`), plus single "Submit" button (`callback_data = f"submit:{row_id}"`).
   - `send_telegram_message(...)` returns telegram_message_id; UPDATE row with the id.
   - Poll `csp_pending_approval` row every 5s until `status != 'pending'` or `now > timeout_at_utc`.
   - On timeout: UPDATE status='timeout', resolved_by='timeout', return `[]` (empty approved list — fail-closed, no candidates allocated).
   - On status='approved': return `[candidates[i] for i in approved_indices]`.
   - On status='rejected' (Yash skipped all): return `[]`.

3. Telegram callback_query handler `_handle_csp_approval_callback(update, ctx)`:
   - Parses callback_data: `(action, row_id, idx?)`.
   - For approve/skip: toggles per-candidate state in a temp dict (or in `approved_indices_json` directly).
   - For submit: UPDATE row status='approved' with the accumulated indices, edit message to show "Submitted at HH:MM by Yash."
   - Idempotent: re-tapping the same button is a no-op.

4. WARTIME interaction:
   - Allocator's mode-check runs UPSTREAM of `approval_gate`. WARTIME → allocator skips entry candidates entirely, gate never fires.
   - This requires verifying the current `run_csp_allocator` sequence: mode check before approval_gate call. If today's order is reversed, MR 6b Phase 1 fixes it as a precondition.

5. Composition root wiring:
   - **Live live path** (telegram_bot.py CSP scheduled job): pass `approval_gate=telegram_approval_gate`.
   - **Paper path** (paper-only invocations): pass `approval_gate=identity` (current behavior preserved).
   - **dev_cli scan-csp**: identity (no gate in dev).
   - Single env-driven switch is acceptable: `if os.environ.get("AGT_CSP_REQUIRE_APPROVAL", "false") == "true": approval_gate = telegram_approval_gate`.

**Phase 1 invariants:**
- `NO_PENDING_APPROVAL_BEYOND_TIMEOUT` — incidents tick: any `csp_pending_approval` row with `status='pending'` and `now > timeout_at_utc + 60s` raises a high-severity incident (race between poll loop death and timeout enforcement).
- `NO_APPROVED_INDICES_OUT_OF_BOUNDS` — pre-merge sentinel: any approved_indices_json index ≥ len(candidates) is logged + dropped (never let a corrupted index reach allocator).

**Phase 1 explicitly does NOT include:**
- LLM ranking
- News feed
- Reasoning text per candidate
- Prompt caching
- Multi-day memory
- Multi-message threading
- Auto-resend on no-tap

### 3.2 Phase 2 — LLM ranking + reasoning injection

**Scope:** ~400–500 LOC on top of Phase 1.

**Components:**
1. Finnhub news pull per ticker (existing infra; recon confirmed Finnhub client live).
2. Anthropic SDK call (Sonnet, not Opus, for cost) ranking the candidate set with per-candidate reasoning bullets.
3. Render Phase 2 digest: same format as Phase 1 plus a `reasoning:` bullet per candidate.
4. `candidate.reasoning` set per MR !69 seam: `{rank: int, rationale: str, news_bullets: list[str]}`. Surfaces in `AllocatorResult.candidate_reasoning[*].upstream_reasoning` for retrospective audit.
5. Fail-open: if LLM or news errors, fall back to Phase 1 bare digest with no reasoning. Log error.

**Phase 2 invariants:**
- `NO_LLM_REASONING_LEAKED_TO_CANDIDATE_LIST` — sentinel test: ranking changes display order but does NOT mutate the underlying CSP-screener output ordering used downstream of allocator.

### 3.3 Phase 3 — Prompt caching + multi-day memory + ranking feedback

**Scope:** TBD. Defer all design decisions until Phase 1 + Phase 2 have produced live operator data.

**Open questions for Phase 3:**
- Caching strategy (per-day system prompt cache? per-week macro context cache?)
- Memory shape (rolling window? thematic summarization?)
- Feedback signal (which candidates Yash approved → reinforces ranking; which got skipped → Penalizes?)

---

## 4. Migration plan

| MR | Title | Scope | Blast | Sequence |
|---|---|---|---|---|
| MR 6b.1 | csp_pending_approval table + bare digest gate | Phase 1 §3.1 components 1–3 | Medium (live-capital path) | After MR 6a |
| MR 6b.2 | Composition-root wiring + env switch | Phase 1 §3.1 components 4–5 | Low | After 6b.1 |
| MR 6b.3 | NO_PENDING_APPROVAL_BEYOND_TIMEOUT invariant + incidents tick wiring | Phase 1 invariants | Low | After 6b.2 |
| MR 6b.4 | One full Phase 1 dry-run on paper + smoke confirmation | Phase 1 acceptance | Zero | After 6b.3 |
| MR 6b.5+ | LLM ranking + news (Phase 2) | Phase 2 §3.2 | Medium | After Phase 1 has 2+ weeks live operator data |
| MR 6b.N | Phase 3 | TBD | TBD | Deferred |

Phase 1 = 4 MRs total. Phase 2 = 1+ MRs deferred. Phase 3 = unscoped.

---

## 5. Out of scope (explicit)

- **Existing engine behavior** (CC, harvest, roll). They retain their identity gates.
- **Paper allocator path.** Stays identity gate forever (per `project_end_state_vision.md` — paper is fully autonomous including CSP, intentionally).
- **Multi-user approval.** Yash is the sole approver. Vikram_Household candidates also gate to Yash (he's the operator for both households).
- **Approval scopes other than CSP entry.** Roll approvals, CC approvals, harvest approvals — all stay autonomous in the live path. Per end-state vision: CSP entry is the one judgment-laden choice; everything else is mechanical.
- **Approval delegation.** No "auto-approve if Yash hasn't responded in N min" — that's just identity gate with extra steps. Fail-closed only.

---

## 6. Invariant additions (Phase 1)

Under `agt_equities/invariants/`:

- **`NO_PENDING_APPROVAL_BEYOND_TIMEOUT`** — manifest severity=high, scrutiny_tier=architect_only. Tripped if any `csp_pending_approval.status='pending'` row's `timeout_at_utc + 60s < now`. Suggests poll loop crashed or callback handler broke.
- **`NO_APPROVED_INDICES_OUT_OF_BOUNDS`** — pre-allocation runtime check inside `telegram_approval_gate` return path. Drops invalid indices, logs warning, never propagates corrupted index to allocator.

---

## 7. Success criteria (Phase 1 only)

1. `AGT_CSP_REQUIRE_APPROVAL=true` in live env wires `telegram_approval_gate` into the live allocator path; paper unaffected.
2. A scheduled CSP allocator run with 3 candidates produces 1 Telegram message with 3 ✅/⏭ button pairs + 1 Submit. Yash's taps update the message in-place.
3. Approval roundtrip end-to-end: candidate → digest → Yash taps approve on 2 of 3 → submit → allocator receives approved subset of 2 → 2 rows hit `pending_orders`, 1 doesn't.
4. Bot restart mid-decision: kill bot, restart, callback_query for the still-pending row resolves correctly via DB-backed state.
5. Timeout: send digest, do not respond for 30 min, allocator returns empty approved list, `csp_pending_approval.status='timeout'`, no rows hit `pending_orders`.
6. WARTIME: gate never fires (mode check upstream blocks the allocator entirely).
7. CI: +12 sprint_a tests minimum (state machine, callback routing, timeout, idempotency, WARTIME-skip, bot-restart-recovery).

---

## 8. Pre-resolved design decisions (locked for MR 6b.1 — Sonnet-authorable)

All 5 items below are **resolved** by Architect (this ADR). MR 6b.1 dispatch author (Sonnet-Orchestrator) proceeds with the locked choice; only the "revisit trigger" conditions below would force re-escalation to Opus-Architect.

- **Q1 — Polling vs notification: LOCKED = polling.** Phase 1 §3.1 component 2 polls `csp_pending_approval` row every 5s. Polling is simpler + survives restart natively vs in-memory event channels. **Revisit only if:** MR 6b.1 recon surfaces concrete cost concern (e.g., >10% scheduler CPU overhead) or production profile shows polling interferes with other scheduler jobs.
- **Q2 — Per-candidate vs all-or-nothing: LOCKED = per-candidate approve/skip.** Matches end-state vision (genuine selection, not yes/no on the whole batch). **Revisit only if:** UX testing during MR 6b.4 paper dry-run shows Yash never actually selects a subset (always approves all or rejects all) — then collapse to batch mode in a follow-up.
- **Q3 — Edit-in-place vs new message per tap: LOCKED = edit-in-place.** Cleaner UX. **Revisit only if:** Telegram bot lacks `edit_message_text` permission (trivial fix — grant permission).
- **Q4 — Submit button vs auto-submit on N taps: LOCKED = explicit Submit button.** Operator might tap-and-untap mid-decision. Auto-submit would race. **Revisit:** none anticipated.
- **Q5 — WARTIME ordering: RECON-ROUTED.** MR 6b.1 dispatch must include a Coder sub-agent recon precondition: confirm `_run_csp_logic` calls `mode_gate_check(household)` before `approval_gate(candidates)`. If reversed, MR 6b.1 scope expands to include the ordering fix as first line item. If correct, proceed with Phase 1 as written. **This is routing, not architectural judgment — Sonnet dispatches the recon, reads the report, adjusts scope.**

---

## 9. Notes

This ADR was authored from `reports/mr6_scope_recon_20260418.md` Sub-agent 2's output. Recon flagged Finnhub news + Anthropic SDK as live but no LLM-ranking-or-approval surface — Phase 2 leverages that infra without building it from scratch.

The MR !69 seam is preserved verbatim. `approval_gate` signature does not change. `candidate_reasoning` payload structure does not change. Phase 2 layers reasoning into `candidate.reasoning` per the seam memory's documented shape — zero allocator-internal changes for Phase 2.

End of ADR-010 draft.
