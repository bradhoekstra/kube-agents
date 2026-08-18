# Direct Agent Routing (`direct_agent_routing`)

A one-hook plugin on the `default` (Chat Agent) profile that sends
`/<agent> <text>` straight to that agent's Hermes profile. `/platform scale the
frontend` runs as a real gateway turn on the `platform` profile — its config,
skills, tools and credentials — with no Chat Agent turn and no kanban card.

## Why

The normal path costs three steps before the specialist reads the request: a
Chat Agent turn to pick an assignee, a `kanban_create` card, and a worker spawn.
That is the right shape when the user describes a problem and the front door has
to work out who owns it. It is pure latency when the user already knows, which
is most of the time for anyone using the harness daily.

## What routes, and what does not

| Typed message                   | Result                                                |
| ------------------------------- | ----------------------------------------------------- |
| `/platform scale the frontend`  | turn on `platform`, text `scale the frontend`         |
| `/cluster-prod why is it down?` | turn on `cluster-prod`                                |
| `<@U123> /platform scale it`    | same; a leading Slack bot mention is stripped         |
| `/Platform scale it`            | same; the name is matched case-insensitively          |
| `/platform`                     | unchanged — no text, so Hermes' unknown-command reply |
| `/nosuchagent hello`            | unchanged — Hermes' unknown-command reply             |
| `/hermes sethome`, `/sethome`   | unchanged — `legacy_slash_commands` owns these        |
| `ask /platform about it`        | unchanged — the match is anchored, Chat Agent answers |

Names are resolved against the live roster, `discover()` in
`agents/chat/scripts/agent_roster.py` — the same list the `list_agents` MCP tool
returns and the `agent_roster` plugin injects into every turn, so the names that
route are exactly the names the front door tells the user about. It is re-read
per message, so a cluster agent scaffolded a moment ago is addressable on the
next one, and it excludes `default`: the front door is not a routing target.

Everything unrecognised falls through unchanged rather than erroring. That is
what keeps the other slash commands working — no profile is named `hermes` or
`sethome`. What happens to a typo after that is Hermes', not ours: the gateway
resolves `/word` against its own commands, plugins and skills, and a word that
matches none of them gets `Unknown command …. Type /commands to see what's
available, or resend without the leading slash`. The Chat Agent is not woken.
That reply is what any unrecognised slash has always produced, and falling
through is what keeps it reachable.

## How it reaches another profile

`pre_gateway_dispatch` fires once per inbound user message, before command
resolution and before auth (`gateway/run.py`). The hook does two things:

1. Sets `event.source.profile` to the target. That field is what
   `_resolve_profile_home_for_source` consults first when deciding whose
   `HERMES_HOME` serves the turn, and Hermes assigns it itself on the
   adapter-ownership path.
2. Returns `{"action": "rewrite", "text": <message without the prefix>}`. The
   gateway applies a rewrite with `dataclasses.replace(event, text=…)`, which
   carries the **same `source` object** through — so the stamp survives.

Two properties of that arrangement are load-bearing:

- **`rewrite`, not `skip`.** Authorization runs after this hook, so a routed
  message is checked against the same allowlist as any other. Routing is not an
  authorization signal and cannot be used to reach a profile the sender could
  not otherwise talk to. A `skip` would take the turn out of the gateway's hands
  and lose that.
- **A message with no `source` is not rewritten.** Stripping the prefix without
  stamping a profile would run the text on the front door as though no agent had
  been named — worse than leaving it alone.

## Enablement

Two switches, both set from one CR field
(`spec.harness.experimental.directProfileRouting`):

- `gateway.multiplex_profiles`, via `GATEWAY_MULTIPLEX_PROFILES=true`. Without
  it Hermes ignores `source.profile` entirely and the turn runs on the front
  door with the prefix silently stripped.
- `KUBE_AGENTS_DIRECT_ROUTING=true`, this plugin's own gate, checked before
  anything else. The plugin ships listed in `plugins.enabled` and does nothing
  until the gate is set, so the image and the CR field move independently.

The plugin must be enabled on the profile that receives chat ingress, and three
places do that between them:

- `agents/chat/config.yaml`, the `default` profile's list in the image. This is
  where a fresh volume gets it.
- `directRoutingFrontDoorOverlay`
  (`k8s-operator/internal/controller/platformagent_manifests.go`), which puts it
  in `profile-default.overlay.yaml` while the CR field is on. An install whose
  PVC predates this plugin needs it: the entrypoint's config back-fill restores
  only whole keys the live file does not hold, and that file already has a
  `plugins.enabled`, so the image's new entry never reaches it. The overlay merge
  unions lists, so it does. Hermes calls `register(ctx)` only for enabled
  plugins, which makes the gap silent — the directory is in the pod, the env var
  says the feature is on, and nothing routes.
- `frontDoorPlugins`, in the same file, so that `/cluster-<name>` still routes
  when `experimental.platformFrontDoor` has re-homed the gateway onto the
  `platform` profile. Enabling it there resolves because this directory ships to
  `/opt/defaults/plugins`, which `profile_scaffold.py` copies into the platform
  profile's own `plugins/` on every start — the same route
  `legacy_slash_commands` takes.

## Maintenance rules

- **Never fail the turn.** The hook runs before auth on every inbound message.
  Any exception is caught and returns `None`, so a bug here degrades to the
  front-door path rather than dropping messages.
- **Anchored match only.** The prefix must start the message, after an optional
  leading bot mention. Rewriting a mid-sentence mention would mangle prose that
  quotes a command.
- **Resolve against the roster, never against a hardcoded list.** The fleet is
  dynamic, and a list here would disagree with what `list_agents` reports — the
  reason the roster logic lives in one shared script.
- **An unreadable roster does not route.** `discover()` returns `None`, distinct
  from `[]`, when the profiles directory could not be listed. Neither routes: a
  fault is not evidence that the agent does not exist.
- **Mind the other `pre_gateway_dispatch` hook.** `legacy_slash_commands` shares
  this hook on the same profile, and the first to rewrite decides what the
  second sees. The two vocabularies do not overlap today; keep it that way.

## What a routed turn gives up

- **The rest of the conversation.** The prefix is per-message only for the
  first one. Hermes namespaces session keys by profile under multiplexing, so a
  routed turn asks for `agent:<profile>:…`, which does not exist yet;
  `_recover_session_from_db` then rebuilds it, and the finder it calls
  (`find_latest_gateway_session_for_peer`) ranks rows by recency for the
  `(platform, user, chat)` peer and ignores the namespace it was asked for. It
  hands back the conversation's existing session and the durable row's
  `session_key`, `profile_name` and system prompt are rewritten to the target.
  Later messages in that conversation continue there, prefix or not.
  `_recovered_row_allowed_for_active_profile` is the guard against a
  cross-profile revival and it applies only while multiplexing is off — which
  is also why clearing the CR field puts the conversation back on `agent:main`.
  `/new` and `/reset` start a clean session; nothing else expires the binding
  under the default reset policy, which is `none`. Nothing in this plugin can
  prevent it: `source.profile` selects the session namespace and the profile
  home together, and the recovery sits below both.
- The Chat Agent's framing. An allowlisted user reaches the target's full tool
  surface with no card and no worker turn between — the trade
  `experimental.platformFrontDoor` documents, which is why the CR field is
  experimental and defaults off.
- Per-user memory. `memory.provider: multiuser_memory` is configured on the
  `default` profile, so a routed turn has no personal-memory recall and cannot
  resolve possessives like "my cluster".
- Progress notifications. There is no card, so there is no rolling `⏳` message;
  the answer arrives as an ordinary reply when the turn finishes.

## Tests

`test_plugin.py` covers the table above, the hook contract, and the gate,
patching `_load_roster_module` since the roster script is loaded by path at
runtime. Run from the repository root:

```bash
python3 -m unittest agents/chat/defaults/plugins/direct_agent_routing/test_plugin.py
```
