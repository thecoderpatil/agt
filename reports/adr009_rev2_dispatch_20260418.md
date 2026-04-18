# Dispatch — ADR-009 rev 2 commit (docs-only, standalone)

**SLOT: claude-coder**
**Coder effort:** trivial. Single-file markdown overwrite. No schema, no code, no tests. Zero live-capital path.

This lands the Heterogeneous Coder Pair ADR on main **before** the Codex spike (`reports/codex_spike_dispatch_20260418.md`) so the spike MR has a committed architectural decision to cite and clean commit lineage.

---

## Target

- **Base tip:** main at `e9b733f0` (post-MR !110). Re-verify via `git ls-remote origin main` immediately before POST; if main advanced, re-base against the new tip — no content changes needed.
- **Branch:** `adr-009/rev2-heterogeneous-pair`
- **Target MR iid:** whatever GitLab assigns (expected !111).
- **CI expectation:** post-merge main pipeline GREEN with **zero test-count delta** (~835 passed / 3 skipped / 8 deselected). ADR is docs-only.

## Context

ADR-009 was drafted 2026-04-18 as a 4-slot homogeneous Claude fleet (v3 Fleet). Scope collapsed same day to a 2-slot heterogeneous pair: 1 Claude Code CLI + 1 Codex CLI. Trigger: (a) live-capital stability dominates throughput, (b) Codex ~20% Claude compute on ChatGPT Pro means a 2nd Claude slot buys ~1.8× at doubled MCP cost while a Codex slot buys ~0.2-0.3× at zero Claude-side cost. Option A Codex isolation (GitLab REST only, no local `.git`) removes the need for `fleet_slots`, `NO_RUNAWAY_WAL`, `NO_DEAD_FLEET_SLOT`, filesystem mutex, or PreToolUse hook. The old 4-slot ADR text has already been overwritten in place on disk; this MR commits the rev-2 bytes to main.

MR F-0 (4-slot ADR commit) and MR F-1 (fleet_slots + invariants + wal_checkpoint cron) are shelved on disk with explicit `SHELVED` banners on top of `reports/F-0_dispatch_20260418.md` and `reports/F-1_dispatch_20260418.md`.

## Scope

Single file. Single action (**update**, not create — file already exists on origin/main from rev 1 commit).

| File | Action | Source |
|---|---|---|
| `docs/adr/ADR-009_FLEET_WORKFLOW.md` | update | Local bytes at `C:\AGT_Telegram_Bridge\docs\adr\ADR-009_FLEET_WORKFLOW.md` (112 lines) |

Verify via `GET /repository/files/docs%2Fadr%2FADR-009_FLEET_WORKFLOW.md?ref=main` that the file currently exists at origin/main (it does — rev 1 landed earlier). POST action must be `update`, not `create` — a `create` will return 400 "file already exists".

No code surface. No `.gitlab-ci.yml` edit. No test file. No scheduler wiring.

## Execution

1. `git -C C:\AGT_Telegram_Bridge fetch origin main` — refresh remote ref; confirm tip matches `e9b733f0` (or note the new tip and proceed).
2. Sanity-check local file before POST:
   ```
   wc -l C:\AGT_Telegram_Bridge\docs\adr\ADR-009_FLEET_WORKFLOW.md          # expect 112
   grep -c "## 2. Decision" C:\AGT_Telegram_Bridge\docs\adr\ADR-009_FLEET_WORKFLOW.md       # expect 1
   grep -c "Heterogeneous Coder Pair" C:\AGT_Telegram_Bridge\docs\adr\ADR-009_FLEET_WORKFLOW.md   # expect >=1
   grep -c "Option A" C:\AGT_Telegram_Bridge\docs\adr\ADR-009_FLEET_WORKFLOW.md              # expect >=1
   grep -c "SLOT: codex" C:\AGT_Telegram_Bridge\docs\adr\ADR-009_FLEET_WORKFLOW.md           # expect >=1
   grep -c "Appendix A" C:\AGT_Telegram_Bridge\docs\adr\ADR-009_FLEET_WORKFLOW.md            # expect >=1
   grep -c "FLEET_ABORT" C:\AGT_Telegram_Bridge\docs\adr\ADR-009_FLEET_WORKFLOW.md                    # expect 0 (rev-1 residual; rev 2 has no abort token)
   grep -c "Phase A" C:\AGT_Telegram_Bridge\docs\adr\ADR-009_FLEET_WORKFLOW.md                         # expect 0 (rev-1 rollout phasing scrubbed)
   grep -c "CREATE TABLE fleet_slots" C:\AGT_Telegram_Bridge\docs\adr\ADR-009_FLEET_WORKFLOW.md        # expect 0 (rev-1 schema scrubbed)
   grep -c "wal_checkpoint" C:\AGT_Telegram_Bridge\docs\adr\ADR-009_FLEET_WORKFLOW.md                  # expect 0 (rev-1 cron job scrubbed)
   grep -c "PreToolUse" C:\AGT_Telegram_Bridge\docs\adr\ADR-009_FLEET_WORKFLOW.md                      # expect 0 (rev-1 mutex hook scrubbed)

   # NOTE: `NO_RUNAWAY_WAL`, `NO_DEAD_FLEET_SLOT`, and `fleet_slots` DO appear in the rev-2 file —
   # but only as eliminated-by references in §2.1 and §4 ("No fleet_slots table, no NO_RUNAWAY_WAL..."
   # is the explicit rationale for Option A). These are load-bearing text for readers comparing
   # against rev 1. Do NOT strip them; the sentinel list below accounts for this.
   ```
3. Read local file bytes → base64 encode → `POST /projects/:id/repository/commits` with:
   ```json
   {
     "branch": "adr-009/rev2-heterogeneous-pair",
     "start_branch": "main",
     "commit_message": "docs(adr-009): rev 2 — heterogeneous coder pair (1 Claude CLI + 1 Codex CLI)",
     "actions": [
       {
         "action": "update",
         "file_path": "docs/adr/ADR-009_FLEET_WORKFLOW.md",
         "encoding": "base64",
         "content": "<base64>"
       }
     ]
   }
   ```
4. Open MR: `POST /merge_requests` with `source_branch=adr-009/rev2-heterogeneous-pair`, `target_branch=main`, `remove_source_branch=true`, `squash=true`. Squash commit message: `docs(adr-009): rev 2 heterogeneous coder pair`.
5. Poll pipeline until GREEN. Verify passed-count unchanged (expected ~835).
6. `PUT /merge_requests/:iid/merge?squash=true` — approval rules retired, no approval round-trips.
7. Post-merge verify: `GET /repository/files/docs%2Fadr%2FADR-009_FLEET_WORKFLOW.md/raw?ref=main` returns bytes byte-identical to local (sha256 or md5 compare), line count 112.

## Commit message (canonical)

```
docs(adr-009): rev 2 — heterogeneous coder pair (1 Claude CLI + 1 Codex CLI)

Scope collapse from 4-slot Claude fleet. Live-capital stability dominates
throughput; Codex ~20% Claude compute means heterogeneous asymmetry beats
homogeneous scaling. Option A isolation: Codex commits via GitLab REST only,
never touches local .git — no fleet_slots, no mutex, no WAL-checkpoint cron.

Spatial fence via one-line SLOT header (codex | claude-coder), enforced at
Architect dispatch authoring time. Routing rule: SLOT: codex only when ALL
hold (<=100 LOC, single module, exact diff verbatim, no investigation, no
cross-module reasoning, no live-capital path, no ADR/incident work). Default
claude-coder for everything else.

Gating: one 30-min Codex spike MR. Green CI + clean byte-match = pair live.
Any failure mode shelves the pair entirely; stay at 1 Claude Coder.

Appendix A carries the DR hallucination filter forward unchanged.

Supersedes rev 1 4-slot fleet draft. F-0/F-1 dispatches shelved on disk.
```

## Verification

Pre-commit (already covered in step 2 above).

Post-merge:
- Remote `docs/adr/ADR-009_FLEET_WORKFLOW.md` raw bytes match local byte-for-byte (md5sum or sha256sum compare).
- Main pipeline GREEN, passed-count delta = 0 (±1 tolerance for non-determinism).
- `reports/mr<iid>_ship.md` written per standard.

## Report format

Standard DISPATCH / STATUS / FILES / COMMIT / CI / VERIFICATION / NOTES block. Include:

- Branch name used.
- MR iid opened.
- Post-merge main pipeline ID + passed-count + delta vs baseline.
- md5/sha256 compare of local vs remote post-merge.
- Any surprises (e.g., if origin/main advanced during the push and the merge rebased) — log verbatim.

---

```yaml expected_delta
files:
  docs/adr/ADR-009_FLEET_WORKFLOW.md:
    added: 112
    removed: 270
    net: -158
    tolerance: 15
    required_sentinels:
      - "# ADR-009 — Heterogeneous Coder Pair (1 Claude CLI + 1 Codex CLI)"
      - "## 2. Decision"
      - "### 2.1 Codex isolation (Option A — no local git)"
      - "### 2.3 Routing rule (load-bearing)"
      - "### 2.4 Compute asymmetry"
      - "## 3. Gating — Codex 30-min spike"
      - "## Appendix A — DR hallucination filter"
      - "SLOT: codex"
      - "claude --help"
    forbidden_sentinels:
      - "FLEET_ABORT"
      - "CREATE TABLE fleet_slots"
      - "wal_checkpoint_fleet"
      - "PreToolUse"
```

Note on `tolerance: 15` — the added/removed counts are approximate (local file is 112 lines, rev 1 on main was ~270 lines; gate treats this as a full rewrite diff, so tolerance is generous). The sentinel list is the load-bearing gate, not the LOC counts.

**Important on negative sentinels:** the rev-2 file legitimately contains `NO_RUNAWAY_WAL`, `NO_DEAD_FLEET_SLOT`, and `fleet_slots` — but only in §2.1 and §4 as eliminated-by references (e.g., "No fleet_slots table, no NO_RUNAWAY_WAL, no NO_DEAD_FLEET_SLOT. Reader count stays at 1."). These are load-bearing rationale for Option A and must stay. The `forbidden_sentinels` above are narrower strings that only appear in the rev-1 draft's active design (table DDL, cron job name, mutex-hook proper noun, abort-token).
