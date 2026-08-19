---
description: Run the required pre-PR review passes — adversarial, docs-drift, IaC parity — each in its own subagent
argument-hint: [base-ref]
---

Run the pre-PR review passes over this branch, against base: **$ARGUMENTS** (empty means `main`).

Follow `.agents/skills/review-preflight/SKILL.md`. It is the procedure; this file only supplies the
base ref and the fact that you were asked.

**You were asked.** Invoking this command is the request to delegate, so spawn the subagents without
asking me again — one per applicable pass, all in a single message so they run concurrently. Review
nothing yourself: your job is the range, the mechanical gates, the fan-out, and the relay. A pass you
run in this context is the one configuration the passes exist to avoid, and I have just removed the
only reason to do it.

Resolve the base before anything else, with the recipe in `review-preflight` §1 — it finds the
remote, fetches it, and stops rather than falling back to a base it could not refresh. Substitute
the base I named above for its `BASE_BRANCH`, if I named one.

Report before you act: the merged disposition list, which passes ran and which you skipped, and
anything the passes said they could not cover. Then fix what the passes confirmed, per
`review-preflight` §6 — the reading order is the point, not a freeze. Leave the PR body until after
I have read the list.
