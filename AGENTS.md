# AGENTS.md

## Project Overview

This repository contains the Kubernetes Agentic Harness (`kube-agents`). It is a collection of agent configurations, personas, and skills designed to manage Kubernetes/GKE operations.

## Repository Layout

- `agents/`: Source of truth for agent blueprints (personas and skills).
  - `chat/`: The Planning Agent front door — the `default` Hermes profile that receives chat ingress, plans the work, and delegates each piece to a specialist.
  - `platform/`: Configuration for the Platform Agent, scaffolded at pod startup into the `platform` profile.
  - `cluster/`: The Cluster Agent profile _template_ (persona, scoped config, and runtime-debugging skills). The Platform Agent scaffolds this into per-cluster Hermes profiles at runtime; it is not deployed directly.
  - `contributor/`: The contributor-agent protocol: the claim/PR/review/escalation loop for external bots (e.g. Kyber, Codebot Robot) coordinating over GitHub alone. Not a runtime blueprint; not shipped in the images.
- `.agents/skills/`: Repository-level skills, not shipped in the agent images — review skills (adversarial change review, security audits, docs-drift, skill quality) run against pull requests and clusters, with `review-preflight` running the pre-PR set of them in a context that did not write the change, plus the `install-kube-agents`/`uninstall-kube-agents`/`upgrade-kube-agents` lifecycle skills that drive the repository's installer scripts.
- `.agents/rules/`: Repository-level rules an agent follows, one file per family: the code (`core_engineering.md`), workflows (`github_actions.md`), the pre-PR passes (`pre_pr_review.md`), eval-driven development (`eval_driven_development.md`), docs (`documentation.md`).
- `a2a/`: Go module for the agent-to-agent bus — wire-protocol library and `a2a` topics CLI per `docs/designs/spec-a2a-payloads.md`, plus persona, gateway and auth-callout.
- `charts/`: Canonical Helm charts (`kube-agents`) for deploying the Kube-Agents operator and profiles.
- `terraform/`: Companion reusable Terraform modules (`gke-cluster`, `kube-agents-iam`, `kube-agents-scope-resolver`, `chat-pubsub`, `github-minter`, `gke-backup-plan`, `drift-pubsub`) for infrastructure provisioning, plus `examples/full-install/`, the single-apply composition that installs the Helm chart on top.
- `deploy/`: Deployment infrastructure code (Dockerfile, Kustomize bases, shared runtime assets).
- `docs/`: Documentation.
  - `site/`: The published documentation site (Astro + Starlight) — the canonical home for
    user-facing docs.
  - `architecture/`: The end-state architecture specification (`01`–`09`). Describes the target, not
    what ships today.
  - `designs/`: Per-feature design documents.
- `k8s-operator/`: Go/Kubebuilder operator reconciling `PlatformAgent` Custom Resources.
- `scripts/`: Repository tooling — `installer/` (what the front doors share), `dev/`, `release/`.
- `examples/`: Example integrations (LiteLLM provider configs, vLLM serving, inference replay).
- `bench/`: Evaluation harness that runs [kubernetes-sigs/devops-bench](https://github.com/kubernetes-sigs/devops-bench) against the Platform Agent as a pip-installed library.
- `images.json`: Inventory of every container image an install pulls, with its upstream reference
  and pin. Read by `make mirror-images`, the kustomize deploy targets, and the docs generator.
- `INSTALL.md`: Installation guide.
- `README.md`: Project overview.

## Where Tests Go

Tests live in many places with different runners. **Decide by asking whether a model call is in the
loop:**

- **No** — it is a test and runs on every pull request. Put it beside the module it covers; in
  `tests/` when there is nothing to sit beside (shell scripts, rendered manifests); in
  `tests/integration/` when it spans two components ([`tests/integration/README.md`](tests/integration/README.md));
  or in `bench/tests/` when one component is the bench harness. Carve-out: **security and
  permissions invariants** go in `tests/conformance/`.
- **Yes, and you plant the defect it has to find** — it is an eval in
  `bench/tasks/<name>/task.yaml` and runs in CI. [`docs/designs/bench-case-format.md`](docs/designs/bench-case-format.md)
  is the contract (`make bench-case-check`, `scripts/test_task_registration.py`). **A change to
  agent behaviour starts from one:** red locally, implement, green three times, registered —
  [`.agents/rules/eval_driven_development.md`](.agents/rules/eval_driven_development.md).
- **Yes, and it checks an install you already have** — it is a critical user journey in
  `bench/cuj/` ([`bench/cuj/README.md`](bench/cuj/README.md)). Manual by design: it grades a live
  deployment rather than planting defects, and CI does not run it.
- **Yes, and it is the release gate** — `tests/e2e/`, run on a schedule by the release-candidate
  pipeline.

A new Python test directory only runs if a `PYTHON_TEST_DIRS` glob in the `Makefile` reaches it
(`tests/conformance/` excepted by design); add the glob in the same change. Full map:
[`docs/testing-map.md`](docs/testing-map.md).

## Agent Setup & Integration

This repository is primarily configuration and documentation for AI agents, plus the Go modules in
`k8s-operator/` and `a2a/`.

1. Follow [INSTALL.md](INSTALL.md) to set up and register the Platform Agent.
2. Refer to [docs/site/src/content/docs/](docs/site/src/content/docs/) for architecture, concepts,
   and operational guides.

## Before Starting a Task

### Branch from a `main` you have just fetched

`main` takes roughly ten commits a day. Always fetch first and branch from the fetched ref:

```bash
# Substitute `origin` for `upstream` if your clone points `origin` at gke-labs/kube-agents.
git fetch upstream main
# --no-track prevents a bare `git push` from targeting upstream/main. Push to your fork instead.
git switch -c <branch> --no-track upstream/main
```

Already on a branch? Measure whether `main` moved underneath the files you are changing with the
drift check in
[`docs/pull-request-workflow.md`](docs/pull-request-workflow.md#measure-how-far-a-branch-has-drifted-from-main).
If it lists any file, rebase onto `upstream/main` and re-read those files before writing more; if
nothing is listed, being behind is a merge-conflict risk to settle later, not a reason to stop.
([`CONTRIBUTING.md`](CONTRIBUTING.md) points here rather than restating this.)

### Check whether someone is already doing it

Before writing code on a non-trivial task, scan open PRs and issues using the queries in
[`docs/pull-request-workflow.md`](docs/pull-request-workflow.md#check-whether-someone-is-already-doing-it)
and report to the user (skip only when the user already named the issue/PR or asked for a direct
one-liner):

- **An open pull request touches your files or solves your problem.** Give the number, author, and
  URL, and say how your task differs. Do not push to someone else's branch or open a competing PR
  without the user's go-ahead. File overlap alone is a merge-conflict warning, not a stop sign —
  say which it is.
- **An open issue describes the task and is unassigned.** Give the number and title, offer to claim
  it, and say what you would comment. Assign or comment only after the user agrees (the token is a
  person's account).
- **The issue is assigned to someone else.** Report it and ask before starting.
- **Nothing matches.** Say so in one line and carry on.

Record the result in the PR's **Context** section (`Closes #<number>`, or the related open PR and
how yours differs) per [`.github/PULL_REQUEST_TEMPLATE.md`](.github/PULL_REQUEST_TEMPLATE.md). Do
not apply `status:` labels here — those belong to the runtime claim loop in
[`agents/platform/skills/github-issue-resolver/SKILL.md`](agents/platform/skills/github-issue-resolver/SKILL.md).

## Skills Guidelines

- Skills live under `agents/platform/skills/` (Platform Agent) and `agents/cluster/skills/` (Cluster Agent); each holds a `SKILL.md` for an AI agent.
- Place a skill by persona: fleet, provisioning and GitOps-write skills go to the Platform Agent; read-only, single-cluster runtime debugging to the Cluster Agent.
- `agents/platform/skills/gke-*` (a reserved prefix) are copies of `google/skills`. One with an `upstream.lock` in `agents/platform/skill-overlays/<skill>/` is edited in place and recorded with `make skills-refresh`; the rest are overwritten by `scripts/sync-upstream-skills.py`, so also put their changes in its `SKILL_SUBSTITUTIONS` or `SKILL_FOOTERS`.

## Engineering Rules

Read the matching file in [`.agents/rules/`](.agents/rules/) before writing code:

- **No magic constants.** Declare every hardcoded value (number, string, duration, path, limit) as a
  named constant at the top of the file, after imports and before the first function, on lines you
  write or touch in Go, Python, and Bash (exempt: `0`, `1`, `-1`, `""`, a literal that is the
  subject of its line, and test files). Details:
  [`.agents/rules/core_engineering.md`](.agents/rules/core_engineering.md).
- **Name it for what it holds.** CodeQL treats `secret` or `trusted` in an identifier as a
  credential; see [`.agents/rules/core_engineering.md`](.agents/rules/core_engineering.md) for the
  word lists.

## Documentation Guidelines

Every fact has one home; check whether the topic has an owner before adding prose:

| Content                                                   | Canonical home                               |
| --------------------------------------------------------- | -------------------------------------------- |
| Installing and running kube-agents yourself; nothing else | `docs/site/src/content/docs/`                |
| End-state architecture                                    | `docs/architecture/`                         |
| Per-feature design rationale                              | `docs/designs/`                              |
| Shared installer defaults and the `install.env` model     | `scripts/installer/README.md`                |
| Which container images an install pulls, and their pins   | `images.json`                                |
| The install procedure (self-contained, agent-executable)  | `INSTALL.md`                                 |
| The commands behind this file's pull-request rules        | `docs/pull-request-workflow.md`              |
| What the agent is and is not permitted to do              | the site's `reference/security-and-iam.md`   |
| How to develop a specific directory                       | that directory's `README.md` (keep it short) |
| Maintainer environments and workflow secret/variable maps | `docs/environment-reconcile.md`              |
| Release runbooks                                          | `scripts/release/README.md`                  |
| The evaluation project pool and Prow configuration        | `docs/ci-pool-projects.md`                   |
| Agent rules, by family (code, CI, pre-PR, evals, docs)    | `.agents/rules/`                             |
| Who to ask about an area, and who owns a running service  | `docs/ownership.md`                          |

Rules (see also [`.agents/rules/documentation.md`](.agents/rules/documentation.md)):

- **Do not hand-write a table that mirrors a machine-readable file.** Edit the source and run
  `make docs-generate` to update `<!-- BEGIN GENERATED -->` regions.
- **Do not restate `make` targets.** `make help` prints them; new targets get a `## description`.
- **Link rather than summarise** when another page owns the topic.
- **The site carries no maintainer identifier** (no App/installation ID, workflow secret/variable,
  internal project, service account, Workload Identity pool, repo path, or maintainer environment).
- **Do not document pull-request status** or cite a PR/issue number as the reason a behaviour exists
  in user/maintainer docs.
- **Verify identifiers against source, not against other docs** (`install.defaults.env`,
  `k8s-operator/go.mod`, and the identifier table in `docs/README.md`).
- **Link a new document from the page that owns its topic; add no map entry** to `docs/README.md`.
- **Write it straight and match length to the task.** Lead with the fact; cut hype (`comprehensive`,
  `robust`, `seamless`), "not X, but Y", and filler sections; prefer prose to `**Bold term:**`
  lists (`SKILL.md` excepted per `.agents/skills/skill-review/SKILL.md`).

Run `make docs-check` before pushing (checks generated regions, links/reachability, terminology,
site audience, and this file plus `CLAUDE.md` against the context budget).

## Contributing as an agent

Unattended agents (collaborating on issues and PRs without a human in the loop)
must read [`agents/contributor/AGENTS.md`](agents/contributor/AGENTS.md). It
defines the agent-to-agent loop (claims, escalations, and review tiers) and
governs where unattended execution conflicts with "ask the user" clauses here.
Agents with a user in the loop follow this file.

## Pull Request Hygiene

- Keep changes scoped to the request; do not commit unrelated formatting changes.
- Maintain the structure and intent of the agent configuration files.
- **Conventional Commits & PR Title Enforcement:** PR titles and commit messages must use
  `type(optional-scope): description` (`feat`, `fix`, `docs`, `style`, `refactor`, `perf`, `test`,
  `build`, `ci`, `chore`, `revert`; breaking changes marked with `!` before `:` or a
  `BREAKING CHANGE:` footer). Confirm the PR title prefix classification with the author before
  opening a PR.
- Push PR branches to a fork, not to the upstream repository.
- **Pin every third-party GitHub Action to a full commit SHA with a version comment**
  (`uses: actions/checkout@3d3c42e… # v7.0.1`), and **guard automatically-triggered credentialed
  workflows against forks** with `if: github.repository == 'gke-labs/kube-agents'` on every job.
  Details and exemptions: [`.agents/rules/github_actions.md`](.agents/rules/github_actions.md).
- Use [`.github/PULL_REQUEST_TEMPLATE.md`](.github/PULL_REQUEST_TEMPLATE.md) (never `gh pr create --fill`).
  A bug fix must fill in **Bug Fix: Preventing Recurrence** naming why it shipped, what catches it
  now, and where else it lives ([`.agents/rules/pre_pr_review.md`](.agents/rules/pre_pr_review.md)).
- **AI Agent Attribution:** Never add AI co-author trailers (`Co-Authored-By:`) to commits; note AI
  assistance in the PR description instead.
- **Write PR titles, bodies, commits, and review replies straight:** plain declaratives leading with
  the outcome, without self-grading (`comprehensive`, `production-ready`).
- **Adversarial and docs-drift self-review before opening a PR:** run `review-adversarial`
  (`.agents/skills/review-adversarial/SKILL.md`) and `review-docs-drift`
  (`.agents/skills/review-docs-drift/SKILL.md`) against your branch diff **in a clean context that
  did not write the change** (spawned via `/pr-preflight`). Fix confirmed findings and record one
  merged disposition list under **Self-Review** naming what you looked for, what was found, and the
  context used. Mechanics: [`.agents/rules/pre_pr_review.md`](.agents/rules/pre_pr_review.md).
- **Live-test the change before opening a PR:** fill in **Testing → Live validation** with how the
  change was exercised against a running installation ([INSTALL.md](INSTALL.md)), or the red-to-green
  eval loop for agent behaviour changes, or "Not live-tested" with the reason when the change cannot
  reach an installation. Mechanics and shared-install lease rules:
  [`.agents/rules/pre_pr_review.md`](.agents/rules/pre_pr_review.md).
- **Keep `Self-Review` and `Live validation` current, not chronological:** fold later passes and
  fixes into the existing sections rather than appending rounds beneath them; keep round-by-round
  history in the review threads ([`.agents/rules/pre_pr_review.md`](.agents/rules/pre_pr_review.md)).
- **The install has one engine: Terraform + Helm.** `terraform/examples/full-install` (via
  `lifecycle.sh`) owns every GCP resource and the chart owns every Kubernetes resource;
  `install.sh` / `uninstall.sh` / `upgrade.sh` only generate `terraform.tfvars` and drive it. Do not
  add a second expression of an install step. Operator-owned YAML mirrored into the chart is held in
  step by `make chart-check`.
- **Expect an automated review after opening a PR** from `kube-agents-bot`; see
  [Automated Review After Opening a Pull Request](#automated-review-after-opening-a-pull-request).
- **Leave no conversation unresolved.** Open threads block merge and keep the PR counted as
  [its author's outstanding work](docs/pull-request-workflow.md#who-owns-an-open-pull-request).
  Reply first, then resolve every addressed thread per
  [`docs/pull-request-workflow.md`](docs/pull-request-workflow.md#resolving-conversations).
- **You do not merge it; Tide does** once a reviewer's `lgtm` and an `OWNERS` approver's `approved`
  are present and required checks pass. Never post `/lgtm` or `/approve` on someone's behalf unless
  asked. Mechanics: [`docs/pull-request-workflow.md`](docs/pull-request-workflow.md#how-a-change-merges).
- **Local Validation Checks:** Before committing, run the checks for what you touched —
  `prettier --write` on changed Markdown/YAML, `make shellcheck` on shell scripts, a
  `--platform linux/amd64` Docker build (and `scripts/check_image_layers.py` if adding `RUN`/`COPY`
  to `deploy/docker/Dockerfile`), `go build` in `k8s-operator/` or `a2a/`, and
  `make terraform-test` on Terraform modules. Commands and constraints:
  [`docs/pull-request-workflow.md`](docs/pull-request-workflow.md#local-validation-before-committing).

### The behavioural presubmit gate

`pull-kube-agents-smoke-test` runs the eval matrix in `hack/ci-eval-pr.sh` (3 repetitions per active
case, 1.5–3.5 hours against a 360-minute ceiling). Non-inert pushes restart it, so open the PR early
and batch changes; Tide retests when `main` moves reuse the head's green in minutes. It goes red
when a case on `BOOTSTRAP_ADMITTED` in `hack/ci-eval-pr.sh` fails all repetitions, or any case trips
an absolute rung (forbidden cluster mutation, verifier error, or inconsistent liveness signals; a
record with no run at all is excluded as infrastructure unless every case hits one). See
`docs/eval-gate-roster.md` for demotion and
[`docs/designs/testing-strategy.md`](docs/designs/testing-strategy.md) §4.2 for the verdict ladder.
On a red, check the health bot comment and
<https://storage.cloud.google.com/kube-agents-dashboards/evals/index.html>: fix regressions caused
by your PR, or file a `presubmit-gate` issue for gate flakes. One `/retest` is reasonable for a
suspected transient; never merge around a red gate, and never instruct anyone to. `/override`
(admin-only) is only for reds the eval crew classified as not the PR's
([how a change merges](docs/pull-request-workflow.md#how-a-change-merges)).

## Automated Review After Opening a Pull Request

Every pull request is reviewed automatically by `kube-agents-bot`, which comments only and never
pushes or merges (its intro comment states its live contract — if it disagrees with what follows,
believe the comment and fix this section; polling and reply commands are in
[`docs/pull-request-workflow.md`](docs/pull-request-workflow.md#the-automated-review) and
[resolving the threads](docs/pull-request-workflow.md#resolving-conversations)).

**What any reviewer reads first — human or agent, this bot included.** Read **Self-Review** before
the diff:

- **Absent, empty, or a bare "reviewed it"** → report that first; the section is required.
- **A claim the diff does not support** → report that as a serious finding in its own right.
- **A finding the author rejected with a reason** → engage with the reason rather than restating the
  finding.

**When it runs.** On `opened`, `reopened`, and draft-marked-ready. Pushing more commits does not
re-trigger a review unless the bot's last review said the branch does not merge. Comment `/review`
(owners, members, collaborators) for a strict pass over the current commit, or `/review all` for a
first-review-width pass. The `agent:ignore` label opts out.

**A human reviewer is requested once its check passes, or at the bot's third round.**
`.github/workflows/auto_request_review.yml` assigns from `.github/auto_request_review.yml` when the
`AI Review` check run goes green — zero findings on the first review, and no 🔴 High on later
reviews (🟠 Medium is posted, not held —
[the cases](docs/pull-request-workflow.md#what-the-check-means)) — or, once, when the bot has
reviewed three commits and the check is still grey. The first request posts a hand-off comment:
from there the reviewer decides, and you reply in the threads rather than asking for another round.
Bot-opened PRs assign immediately on check completion, and `/request-review` (at the start of the
comment, by owners, members, or collaborators) overrides the gate for a disputed finding or missing
review.

**What agents must do.** After opening a ready PR (not a draft, which sits outside the queue until
marked ready), tell the user the bot review is on its way and **offer to wait for it**. When
findings arrive, summarise each and let the user decide whether to fix, push back, or defer before
changing code. Answer every disagreed finding in its thread. After pushing fixes, ask the user
whether to comment `/review` or `/review all`; once the last `/review` settles, fold the fixes and
any re-run live tests into **Self-Review** and **Live validation** and resolve the addressed
threads. Resolve a thread only when **fully confident the issue is addressed** — the fix is on the
PR head with its commit named, or the finding is factually wrong against the current merge target
(plus, with a user in the loop, declined `kube-agents-bot` findings other than the description
thread once replied to and recorded in **Self-Review**). Leave a judgment call, a request you chose
not to do, or an unanswered rebuttal open for the reviewer — resolving says the conversation is
finished, not a way to end a disagreement. Reply first, always: a resolved thread collapses, so the
reply is the only record a reviewer may ever see.

## Before Reviewing Someone Else's Pull Request

Before running a review you were asked for, check whether `kube-agents-bot` and the author's
**Self-Review** + **Live validation** already cover the current head. If both hold and neither is
stale, **ask rather than decide**: show the evidence and let the requester choose whether to spend
another review round. Watch for two traps:

- **Currency:** a review at an older commit is stale unless the only commits since are merges from
  the base branch, and any unresolved review thread means work is still outstanding.
- **Unanswered Self-Review:** "no findings" counts only alongside what was looked for.

Mechanics, queries, and verdicts live in
[`.claude/commands/pr-review-batch.md`](.claude/commands/pr-review-batch.md).
