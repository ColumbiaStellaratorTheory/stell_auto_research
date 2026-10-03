# Lessons Learned

Append-only research memory. The agent reads this file at the start of every
session and appends a lesson whenever a finding generalizes beyond a single
run. Humans may also append. **Never edit or delete past entries** — if a
lesson turns out to be wrong, append a correction that references it.

This template serves two levels:

- **Campaign lessons** — `campaigns/<name>/LESSONS.md`, findings of one campaign.
- **Solver lessons** — `lessons/<adapter>.md` at the repo root, findings about
  the solver that hold whatever the goal. The agent adds entries by promotion
  from a campaign (see the `/research` skill) and must carry `source:`.

## Rules

- One lesson per entry, under a dated heading (`## YYYY-MM-DD — title`);
  `run.py brief` lists the latest titles. Keep an entry to a few lines.
- Use the fields below with the closed vocabularies given, so entries can be
  scanned and counted.
- Cite evidence: run ids, or a `run.py query` that reproduces the
  observation. A lesson without evidence is a hypothesis — say so in `status`.
- Record negatives. "X does not work because Y" saves more compute than
  champions do.
- Before running an experiment or batch, name the lessons it applies and the
  ones it deliberately tests or rejects.

## Format

```markdown
## YYYY-MM-DD — short title

- kind: recipe | dead-end | crash-cause | metric-caveat | correction
- scope: the mode / target / parameter region the lesson covers
- claim: one falsifiable sentence, with numbers
- evidence: run ids, or the query that reproduces it
- action: how this changes experiment selection
- status: hypothesis | confirmed | superseded by <date — title>
- source: campaign · campaign-lesson title · run ids   (solver lessons only)
```

In a campaign file, `confirmed` means the claim held in at least two
independent runs (different seeds or replicates). In a solver file, it means
the claim held in at least two campaigns (one `source:` per campaign);
otherwise it stays `hypothesis`. A `correction` names the entry it corrects in
`scope`.

---

<!-- Entries below. Newest last. -->
