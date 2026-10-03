# Solver Lessons — toy

Append-only memory about the `toy` adapter's solver, shared by every campaign
that uses it. `run.py brief` lists the latest titles in its "solver lessons"
line. A lesson lands here when it is `confirmed` in a campaign's `LESSONS.md`
and its scope does not depend on that campaign's goal. **Never edit or delete
past entries** — if a lesson turns out to be wrong, append a correction that
references it.

## Rules

- One lesson per entry, under a dated heading (`## YYYY-MM-DD — title`).
  Keep an entry to a few lines.
- Use the fields below with the closed vocabularies given, so entries can be
  scanned and counted.
- `source` names where the lesson was learned: the campaign, the title of its
  campaign lesson, and the run ids behind it. A lesson seen in several
  campaigns lists each one.
- `status: confirmed` here means the claim held in at least two campaigns;
  until then it is a `hypothesis`, whatever its status in the campaign.
- Record negatives. "X does not work because Y" saves more compute than
  champions do.

## Format

```markdown
## YYYY-MM-DD — short title

- kind: recipe | dead-end | crash-cause | metric-caveat | correction
- scope: the mode / target / parameter region the lesson covers
- claim: one falsifiable sentence, with numbers
- evidence: run ids, or the query that reproduces it
- action: how this changes experiment selection
- status: hypothesis | confirmed | superseded by <date — title>
- source: <campaign> — <campaign lesson title> — <run ids>
```

A `correction` names the entry it corrects in `scope`.

---

<!-- Entries below. Newest last. -->
