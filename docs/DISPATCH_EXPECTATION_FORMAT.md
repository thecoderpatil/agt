# Dispatch Expectation Block Format

Every AGT dispatch markdown file that triggers a `POST /repository/commits`
must include a machine-readable `expected_delta` block. The pre-commit gate
(`scripts/precommit_loc_gate.py`) reads this block and rejects the commit if
reality diverges from the declared expectation.

---

## Block format

Place the block in the final section of the dispatch markdown:

````
```yaml expected_delta
files:
  path/to/file.py:
    added: 125        # lines added (difflib insert + replace)
    removed: 14       # lines removed (difflib delete + replace)
    net: 111          # added - removed
    tolerance: 10     # actual net may differ by ±tolerance without fail
    required_symbols: # top-level def/class/assign names that MUST exist post-patch
      - function_name
      - ClassName
      - MODULE_CONSTANT
    required_sentinels: # literal strings that MUST appear in file bytes
      - "staging_callback([ticket])"
      - "return result"
  tests/test_file.py:
    added: 230
    removed: 0
    net: 230
    tolerance: 20
    required_sentinels:
      - "pytestmark = pytest.mark.sprint_a"
  .gitlab-ci.yml:
    added: 1
    removed: 1
    net: 0
    tolerance: 0
    required_sentinels:
      - "tests/test_new_file.py"

# Explicit opt-in for shrinkage > tolerance. Omit section if pure growth.
shrinking:
  - file: agt_equities/foo.py
    reason: "Refactor X consolidated into Y helper"
    expected_net: -45
```
````

---

## Gate rules

| Rule | Action |
|------|--------|
| `abs(actual_net - declared_net) > tolerance` | HALT |
| `actual_net < declared_net - tolerance` and no `shrinking:` clause | HALT (undeclared shrinkage) |
| `shrinking:` clause present but `abs(actual_net - clause.expected_net) > tolerance` | HALT |
| Any `required_symbols` missing from AST walk of `.py` file | HALT |
| Any `required_sentinels` not found as literal string in file bytes | HALT |
| Staged file not declared in `files:` | HALT |
| File declared in `files:` but not in staged set | HALT |
| `expected_delta` block missing from dispatch entirely | HALT (exit 2) |

No `--force` flag. No bypass path. A dispatch without the block is not
ready to ship.

---

## How to count LOC delta

Use `diff_stats` from `scripts/precommit_loc_gate.py` locally, or count
manually:

- `added` = lines in new file not in old (inserts + replace-adds)
- `removed` = lines in old file not in new (deletes + replace-removes)
- `net` = `added - removed`

For new files (no origin): `old_text = ""`, so `added` = line count of
new file, `removed` = 0, `net` = line count.

For modified files: use `git diff --stat` as a rough check, then validate
with the gate's own `diff_stats` function if uncertain.

---

## Tolerance guidance

| Case | Suggested tolerance |
|------|---------------------|
| New file (pure addition) | 20–50 (minor authoring drift) |
| Small targeted patch | 5–10 |
| CI file line append | 0 (exact) |
| CLAUDE.md single step insert | 2–3 |
| Large restoration / refactor | 30–50 |

Don't set tolerance so high it defeats the gate. The default is 10 LOC.
Bump per-file only with a reason in the dispatch.

---

## Shrinking clause

Use `shrinking:` when a patch intentionally reduces LOC beyond tolerance:

```yaml
shrinking:
  - file: agt_equities/foo.py
    reason: "Extracted helper consolidates 3 inline blocks"
    expected_net: -45
```

The gate enforces that `actual_net` is within `tolerance` of `expected_net`.
A vague reason is better than no clause. Never use shrinking to mask
unintended truncation — that is exactly the failure mode this gate prevents.

---

## Architect commitment

Every dispatch drafted in Cowork must include this block before sending to
Coder. Coder MUST reject at the gate if the block is absent or if reality
diverges beyond tolerance. Architect re-drafts the dispatch with corrected
counts; no workaround.

This discipline is forward-only. Existing MRs are not retroactively gated.
