<!--
Template for campaigns/<slug>/program.md, written by /setup-harness (or fill
the {{PLACEHOLDERS}} by hand). Keep it to what is specific to this campaign;
the research method (loop, batches, queries, lessons, machine use) lives in
.claude/skills/research/SKILL.md and is started with `/research <slug>`.
Limits a computer can check go in config.json (`fixed`, `bounds`, `budget`),
not here. Do not add strategy advice or recommended ranges: every guardrail
removes agent capability. Delete these comments in the generated file.
-->

# {{CAMPAIGN_TITLE}}

Run with `/research {{CAMPAIGN_SLUG}}`. Method, commands and lessons protocol:
`.claude/skills/research/SKILL.md`.

## Mission

{{MISSION}}
<!-- 2-5 sentences: what system, what is optimized, and why. -->

## Goals and stopping criteria

{{GOALS}}
<!-- The measurable claim that defines success and how it is verified (a
     metric threshold, a validated=pass run, ...), how to rank conflicting
     goals, and when to stop (criteria met, plateau after N runs, ...). -->

## Hard rules

{{HARD_RULES}}
<!-- Numbered. Only limits no code can check (conventions, judgment calls,
     what counts as a valid comparison). "None." is valid. -->

## Enforced in code

Parameter constraints (`fixed`, `bounds`) and the run budget are in
`config.json` and enforced by `run.py`; `run.py brief --campaign
{{CAMPAIGN_SLUG}}` shows them with the budget used. Metric directions come
from the adapter's `METRICS`.
