---
date: 2026-10-02
problem: parallel first-time run.py processes refused a half-created results.db as "schema version 0"
tags: [sqlite, concurrency, schema-versioning]
---

# Create the schema and stamp its version in one transaction

## Problem
A 4-way parallel batch on a fresh campaign: 1 of 8 child `run.py` processes exited
without a result. Its stderr (in `batches/<id>.log`) said `results.db has schema
version 0, this harness needs 4`. Sequential runs never showed it, and the unit
tests passed, because the failure needs two processes creating the DB at the
same moment.

## Dead ends
None. The child's stderr named the refusal directly. The work was in seeing
why a version-checked DB could ever read as version 0.

## Working approach
1. `open_db` treats "runs table exists" as "schema created" and then checks
   `PRAGMA user_version`.
2. `_create_schema` ran `executescript(schema.sql)`, which commits, and then a
   separate `PRAGMA user_version = N`. Between those two commits, another
   process sees the table with version 0 and refuses it.
3. Fix: run `PRAGMA journal_mode = WAL` first (it cannot run inside a
   transaction), then
   `executescript("BEGIN IMMEDIATE; <CREATE ... IF NOT EXISTS>; PRAGMA user_version = N; COMMIT;")`.
   `schema.sql` now holds only CREATE statements. A second creator blocks on
   the write lock and then does nothing, because of IF NOT EXISTS.
4. Verified by re-running the same parallel batch: 12/12 children recorded.

## Why it worked
`user_version` lives in the database header and is written as part of the
enclosing transaction. Putting the DDL and the version stamp in one
`BEGIN IMMEDIATE … COMMIT` makes "table exists" and "version stamped" a single
atomic state change, so no reader can observe one without the other.
`executescript` commits any pending transaction before running its script, so
the BEGIN/COMMIT has to be inside the script text.

## Reusable rule
When code decides "already initialized" from one marker (a table exists) and
validates with another (a version number), check whether both are written in
one transaction. If they come from separate statements or commits, wrap both
in one explicit transaction and test with ≥2 processes initializing at once.

## Pointers
- `run.py` `_create_schema` and `open_db`; `schema.sql` header comment
- commit 37014fdd on `general-harness`
- Same step, separate bug: the batch launch loop `while queue or running`
  spun forever after early stop. A loop over a work queue must also end when
  launching is disabled: `while running or (queue and stop is None)`.
