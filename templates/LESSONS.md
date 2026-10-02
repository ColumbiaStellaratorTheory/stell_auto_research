# Lessons Learned

Append-only research memory. The agent reads this file at the start of every
session and appends a lesson whenever a finding generalizes beyond a single
run. Humans may also append. **Never edit or delete past entries** — if a
lesson turns out to be wrong, append a correction that references it.

## Rules

- One lesson per entry. Dated, titled, falsifiable.
- Cite evidence: run ids from `results.db`, or a query that reproduces the
  observation. A lesson without evidence is a hypothesis — label it as one.
- Record negatives. "X does not work because Y" saves more compute than
  champions do.
- Corrections reference the entry they correct by date + title.

## Format

```markdown
## YYYY-MM-DD — short title

**Observation:** what happened, with numbers.
**Evidence:** run ids / SQL query / artifact paths.
**Action:** how this changes future experiment selection.
```

---

<!-- Entries below. Newest last. -->
