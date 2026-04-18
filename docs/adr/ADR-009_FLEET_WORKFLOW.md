# ADR-009 — Heterogeneous Coder Pair (1 Claude CLI + 1 Codex CLI)

**Status:** Draft
**Date:** 2026-04-18 (rev 2 — scope collapsed from 4-slot Claude fleet)
**Author:** Architect (Cowork)
**Supersedes:** n/a — extends v2 workflow
**Complements:** ADR-007 (Self-Healing Loop), ADR-008 (Shadow Scan)

---

## 1. Problem

v2 workflow is one warm Claude Code CLI session applying dispatches sequentially. Throughput bound = one MR at a time. The rev-1 draft of this ADR proposed a 2→3→4 homogeneous Claude fleet with filesystem git mutex, WAL checkpoint cron, fleet_slots observability, and a 4-MR foundation. That scope was rejected 2026-04-18 for two reasons:

1. **Stability dominates throughput** on live-capital infra. Every slot is a new failure surface; homogeneous Claude slots multiply MCP memory duplication + GitLab API contention without proportional return.
2. **Codex compute is ~20% of Claude's** on ChatGPT Pro. A second Claude slot doubles Claude's MCP stack for ~1.8× throughput; a Codex slot adds ~0.2-0.3× at zero Claude-side cost. Heterogeneous is the right asymmetry.

## 2. Decision

One Claude Code CLI (primary Coder, warm) + one Codex CLI (mechanical tail, on-demand). Claude owns the critical path; Codex absorbs a narrow slice of mechanical work that would otherwise queue behind substantive dispatches.

### 2.1 Codex isolation (Option A — no local git)

Codex commits via GitLab REST API only. It never touches the local `C:\AGT_Telegram_Bridge\.git\`. Its workspace is `/tmp/codex-scratch/` or a separate shallow clone — read-for-diffing, discard after push. Consequences:

- Zero local `.git` contention → no filesystem mutex, no PreToolUse hook, no stale-lock sweep.
- No `fleet_slots` table, no `NO_RUNAWAY_WAL`, no `NO_DEAD_FLEET_SLOT`. Reader count stays at 1.
- GitLab-side concurrency is already handled — unique branches, API atomic commits, existing CI queue.
- Observability stays at operator eyeballs; 2-slot health does not need SQL infrastructure.

### 2.2 Spatial fence via dispatch convention

Every dispatch opens with a one-line header:

```
SLOT: codex
```
or
```
SLOT: claude-coder
```

No file-system fence, no mutex. Architect discipline at dispatch authoring time is the fence. Two dispatches with overlapping module bounds never go out concurrently.

### 2.3 Routing rule (load-bearing)

`SLOT: codex` ONLY when ALL hold:

- ≤100 LOC change
- Single module touched
- Exact diff verbatim in the dispatch (no Read-and-verify at apply time)
- No shell investigation (no grep, no DB queries, no script runs)
- No cross-module reasoning
- No live-capital path (allocator, compliance gates, `flex_sync`, `walker`, `ib_order_builder` — all Claude)
- No ADR / incident triage / dispatch authoring

Appropriate Codex shape: docstring fixes, typos, parametrize-case adds, mechanical renames + import reorgs, simple schema migrations with defaults, small test-file additions with assertions spelled out, report templating.

`SLOT: claude-coder` (default): everything else. When in doubt → Claude.

### 2.4 Compute asymmetry

Codex ≈ 20% of Claude compute on ChatGPT Pro. Pair speedup is 1.2-1.3× steady-state, not 2×. Value prop is freeing Claude cycles for higher-judgment dispatches, not raw parallelism.

## 3. Gating — Codex 30-min spike

Single throwaway MR before pair goes live. Unambiguously in-lane dispatch (docstring expansion or parametrize add). Codex consumes the dispatch markdown via `codex exec`, applies the diff, pushes via GitLab REST, opens MR. Green CI + clean byte-match = pair live. Failure (wrong bytes, mutex surprise, API auth issue, can't parse dispatch) = shelve the pair entirely, stay at 1 Claude Coder.

## 4. Consequences

**Positive.** Codex absorbs the mechanical tail that would stall in Claude's queue. Claude's warm context stays loaded for high-judgment work. Rate-limit distribution across Anthropic + OpenAI. No new infra surface to maintain.

**Negative.** Mis-routing judgment-heavy work to Codex is expensive (rework + lost trust). Under-routing is cheap — dispatch stays in Claude queue, no harm. Discipline at the routing call.

**Risks mitigated.** No fleet_slots / invariants / mutex surface → nothing to debug when a slot wedges. Abort is trivial: stop dispatching `SLOT: codex`; Claude is untouched.

**Residual.** Codex quality on our dispatch format is unverified — resolved by §3 spike.

## 5. Open questions

1. **Codex dispatch markdown format compatibility** — resolved by spike.
2. **GitLab MR author attribution** — Codex commits use ChatGPT account token; confirm attribution is acceptable for audit trail.
3. **Dispatch routing heuristic calibration** — expect the ≤100 LOC cutoff to drift; observe 3-5 Codex dispatches post-spike, revise if needed.

## 6. References

- `reports/gemini_dr_prompt_v3_fleet_workflow_20260418.md` — original DR prompt.
- Gemini DR response consumed inline 2026-04-18; not persisted. Verified Codex CLI framing absorbed here; refuted claims catalogued in Appendix A.
- ADR-007 `SELF_HEALING_LOOP.md`, ADR-008 `SHADOW_SCAN.md` — prior architecture ADRs.
- Memory `feedback_codex_compute_asymmetry.md` — 20% compute ratio + routing rule.
- Memory `feedback_architect_earns_keep_or_cut.md` — three-shape dispatch taxonomy.
- Memory `project_v2_workflow_live_2026_04_17.md` — v2 split precedent.

---

## Appendix A — DR hallucination filter

Gemini DR (2026-04-18) mixed verified and unverified claims. This ADR acts only on the verified subset. The following are REFUTED or unverified and NOT design inputs:

- `claude --worktree` / `-w` flag — nonexistent; standard `git worktree add` is the primitive if ever needed.
- `.worktreeinclude` file — nonexistent.
- `claude --name` / `/rename` / `--fork-session` — unverified syntax.
- `& Build a REST API` dispatched to "Anthropic's managed cloud infrastructure" — false; Claude Code runs locally.
- `/schedule` command — that's Cowork's scheduled-tasks feature, not Claude Code CLI.
- Anthropic "Agent Teams" native multi-agent in v2.1.32+ — unverified, likely hallucinated.
- KB5079473 March 2026 Windows cumulative update regression — specific unverified; generic Windows caution retained.
- OpenAI Codex CLI v0.121.0 specifics (Rust codebase %, `supports_parallel_tool_calls`, `AGENTS.md` philosophy) — unverified versioning; the spike is the empirical test, not DR's version claims.
- "Gas Town" / "IttyBitty" / "Multi-Agent Channel" case-study specifics — structural takeaways (filesystem-is-the-bus, spatial isolation) restated without reliance on case-study scales.
- "Claude Code CLI native Remote Control feature" — unverified, likely hallucinated.

Flip any entry only with `claude --help` / `claude --version` from the live Windows CLI, or an official anthropic.com/claude-code documentation page. Re-pastes of the same DR response do not flip entries.
