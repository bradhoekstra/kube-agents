#!/usr/bin/env bash
# Startup for the agent shell sandbox. Everything here is state that cannot be
# baked into the image: it depends on the mounted volume, the mounted key, or
# the pod's environment.
#
# Deliberately short. The prototype this replaces did its package installs and
# wrote its sshd config from a heredoc in the Sandbox CR's `args`, which meant
# the sandbox's actual configuration lived in a YAML string that no linter,
# test or review tool could see. Anything that can be a file in the image is
# one; what is left is below.
set -euo pipefail

log() { echo "sandbox-entrypoint: $*" >&2; }

DATA="${SANDBOX_DATA:-/opt/data}"
SSHD_STATE="${SANDBOX_SSHD_STATE:-/var/lib/sandbox-sshd}"
AUTHORIZED_KEYS_SRC="${SANDBOX_AUTHORIZED_KEYS:-/etc/ssh-authorized/authorized_keys}"
DEFAULTS="${SANDBOX_DEFAULTS:-/opt/defaults}"

# Which Hermes homes get a copy of the image's trees, as paths under $DATA with
# `.` meaning $DATA itself. The agent pod keeps one home per profile and its
# instructions name both levels: `/opt/data/scripts/forge.py` for the shared
# scripts and `/opt/data/profiles/platform/governance/inventory_prioritize_sop.md`
# for the Platform Agent's own SOPs. Both are read over SSH now, so both paths
# have to resolve here.
#
# `platform` is named rather than discovered because the profile list lives on
# the agent pod's PVC, which this container cannot see. The cluster profiles are
# deliberately not in the list: everything agents/cluster names is under
# /opt/data/scripts, which the machine home already carries. What the agent pod
# does push in is the *empty* layout for every profile it has, including those —
# deploy/shared/sandbox_mirror.py, which does know the list.
SANDBOX_HOME_ROOTS="${SANDBOX_HOME_ROOTS:-. profiles/platform}"

# 1. The model's durable directory. A PVC mounts over the image's /opt/data and
#    arrives owned by root, so the agent could not write to it. Not recursive:
#    only the mount point needs fixing, and a recursive chown over a volume that
#    has been in use for a while is a slow way to start a pod.
if [ ! -d "$DATA" ]; then
  log "data directory $DATA does not exist"
  exit 1
fi
chown agent:agent "$DATA"

# Which /opt/data this is. The path is deliberately the same as the agent pod's
# Hermes home so that a script naming it resolves wherever it runs, and the cost
# of that is one path naming two different directories. A missing file used to
# be the signal that a path belonged to the other side; this marker is what
# replaces it.
cat >"$DATA/.sandbox" <<'MARKER'
This is the shell sandbox's /opt/data, on the sandbox's own volume.

It is not the agent pod's Hermes home, which carries the same path and holds
the profiles, the session databases and the model API keys. Nothing is copied
between them and nothing can read across. A handoff that writes a file on one
side and reads it on the other will not work, however identical the path looks.
MARKER
chown agent:agent "$DATA/.sandbox"

# 1a. The skills, SOPs and shared scripts the agent's shell runs, from the image
#     onto the volume. The Dockerfile explains what is in each tree and why the
#     staging directory exists at all: the PVC mounts over /opt/data, so anything
#     baked there directly would be invisible the moment a volume is attached.
#
#     Replace rather than merge. `cp` over the top leaves behind a skill deleted
#     from the image and a script renamed in it, and both then sit on the volume
#     looking current for as long as the PVC lives — the same failure the agent
#     pod's step 2.6a exists to prevent, arriving here by the same route. The
#     model's own files belong in $DATA/scratch and $DATA/gitops, which this does
#     not touch; a helper it writes into $DATA/scripts is gone at the next start,
#     and that is the contract rather than an accident.
#
#     Not swallowed. A half-synced tree fails later and somewhere else — as a
#     skill whose script is missing, or a stale one that no longer matches the
#     SKILL.md the agent pod put in the prompt.
#     Once per home root in $SANDBOX_HOME_ROOTS, so the same tree is reachable
#     by the machine-home path and by the profile-home path the SOPs use. They
#     are copies rather than symlinks: a symlinked profile tree makes an `rm -rf`
#     inside one home delete the other's, and the model owns both.
if [ -d "$DEFAULTS" ]; then
  for root in $SANDBOX_HOME_ROOTS; do
    if [ "$root" = "." ]; then
      home="$DATA"
    else
      home="$DATA/$root"
    fi
    install -d -o agent -g agent "$home"
    # -o/-g reach the last component only. `profiles/platform` therefore leaves
    # $DATA/profiles owned by root, and 0755 root:root is readable and traversable
    # enough that nothing looks wrong: the platform profile is agent-owned, the
    # shell works, every skill works. What fails is creating anything *beside*
    # platform, which is exactly what sandbox_mirror.py does — it extracts one
    # home per profile the agent pod has, and each cluster profile is a mkdir in
    # this directory. tar exits 2, the mirror raises before writing its marker,
    # and the model's pre-upgrade files stay on the agent's volume where the
    # shell can no longer see them. The only trace is a line in
    # logs/sandbox_mirror.log. Walk back up to $DATA so the parents match the leaf.
    #
    # The walk starts at $home and not at its parent, so that the `.` root --
    # where $home IS $DATA -- runs zero iterations. Starting one level up instead
    # sends that case climbing out of the volume: /opt next, which owns
    # /opt/credential-proxy, and an agent-owned /opt is uid 1000 able to rename
    # the shims aside and put its own there.
    dir="$home"
    while [ "$dir" != "$DATA" ] && [ "$dir" != "/" ] && [ "$dir" != "." ]; do
      chown agent:agent "$dir"
      dir="$(dirname "$dir")"
    done
    for entry in "$DEFAULTS"/*; do
      [ -e "$entry" ] || continue
      name="$(basename "$entry")"
      rm -rf "${home:?}/$name"
      cp -a "$entry" "$home/$name"
      chown -R agent:agent "$home/$name"
    done
    log "synced $(cd "$DEFAULTS" && echo *) from $DEFAULTS into $home"
  done
else
  log "no $DEFAULTS in this image — the agent's skills, SOPs and shared scripts"
  log "will be absent from $DATA and every skill that names one will fail."
fi

# 2. The agent's public key. Failing loudly here is the point: without it sshd
#    starts perfectly happily and every connection is refused with "Permission
#    denied (publickey)", which reads like a key mismatch on the agent side and
#    sends whoever is debugging it to the wrong pod.
if [ ! -r "$AUTHORIZED_KEYS_SRC" ]; then
  log "no authorized_keys at $AUTHORIZED_KEYS_SRC — the agent could not log in."
  log "Mount the sandbox key secret there, or set SANDBOX_AUTHORIZED_KEYS."
  exit 1
fi
install -m 0600 -o agent -g agent "$AUTHORIZED_KEYS_SRC" /home/agent/.ssh/authorized_keys
# The same key also authorises `hermes`, the principal trusted agent-pod code
# connects as instead of `agent`. The Dockerfile comment on that account says
# why the two cannot be the same login. Nothing else here needs changing: the
# SetEnv drop-in written in step 4 is global, so `hermes` inherits PATH and
# CREDENTIAL_PROXY_URL on the same terms.
install -m 0600 -o hermes -g hermes "$AUTHORIZED_KEYS_SRC" /home/hermes/.ssh/authorized_keys

# 3. Host keys, on a volume of their own rather than in the container.
#    sshd_config explains why they must survive a pod recycle; this creates them
#    the first time and leaves them alone afterwards.
#
#    Root-owned, and on a different volume from $DATA. An earlier version kept
#    them under the model's volume and chowned them to uid 1000, which handed
#    the model the private half of the key both clients pin with
#    StrictHostKeyChecking=accept-new. Mode bits alone would not fix that: the
#    model owns $DATA's mount point, so it can rename any directory inside it
#    aside and have this loop populate a replacement it controls. A separate
#    volume it cannot write is what actually settles it, and sshd reads these as
#    root, so uid 1000 needs no access at all.
if [ ! -d "$SSHD_STATE" ]; then
  log "sshd state directory $SSHD_STATE does not exist"
  exit 1
fi
# Refuses the misconfiguration this split exists to prevent, rather than
# silently accepting a state directory the model can write.
sshd_state_owner="$(stat -c '%U' "$SSHD_STATE")"
if [ "$sshd_state_owner" != "root" ]; then
  log "$SSHD_STATE is owned by $sshd_state_owner, not root — refusing to keep"
  log "host keys somewhere the sandboxed account could read or replace them."
  exit 1
fi
chmod 0700 "$SSHD_STATE"
for type in ed25519 rsa; do
  key="$SSHD_STATE/ssh_host_${type}_key"
  if [ ! -f "$key" ]; then
    log "generating $type host key (first start on this volume)"
    ssh-keygen -q -t "$type" -N '' -f "$key"
  fi
  chown root:root "$key"
  chmod 600 "$key"
  # Guarded rather than assumed: a volume carrying a private key whose public
  # half was deleted is unusual but not impossible, and under `set -e` an
  # unguarded chown on the missing file would fail the pod start with an error
  # about a file sshd does not even read.
  if [ -f "$key.pub" ]; then
    chown root:root "$key.pub"
    chmod 644 "$key.pub"
  fi
done

# 4. The pod's environment, for the agent's shell. sshd does not pass its own
#    environment to sessions — by design, and PermitUserEnvironment is off — so
#    a variable the pod spec sets would otherwise be invisible to every command
#    the agent runs. This forwards an allowlist, not the environment: the pod
#    may hold values that have no business inside the sandbox, and copying it
#    wholesale is how one of them ends up readable there.
#
#    CREDENTIAL_PROXY_URL is the one that has to make it across. Without it the
#    kubectl and gcloud wrappers exit 1 with "CREDENTIAL_PROXY_URL is not
#    configured" (agents/platform/scripts/credential_proxy_client.py).
#
#    A generated sshd drop-in rather than an /etc/profile.d script, which is
#    what this originally was: profile.d is read by login shells only, and
#    `ssh sandbox kubectl get pods` — the shape of every command Hermes sends
#    once its environment snapshot is taken — is not one. The first build of
#    this image reached the sandbox with PATH correct and CREDENTIAL_PROXY_URL
#    empty, so the wrappers resolved and then refused to run.
#
#    PATH is written here too, on the same line, and it has to be: sshd keeps
#    the first SetEnv directive and discards every later one whole, so this
#    cannot be split into a static PATH in sshd_config plus a generated line
#    here. Whichever came first would be the only one that survived. The
#    sshd_config comment carries the same warning from the other side.
#    HERMES_HOME and PLATFORM_AGENT_HOME are static, and set from $DATA rather
#    than forwarded from the pod. Both name a data root, and the two pods' roots
#    are different volumes that only happen to share a path: forwarding the agent
#    container's value would point every skill here at a directory this container
#    does not have the moment an install moves `spec.harness.hermes.agentHome`.
#
#    They have to be set at all because step 1a is only half the delivery. A
#    SKILL.md says `"$HERMES_HOME"/scripts/github_token_refresh.py` as often as it
#    says the literal path, cluster_preflight.sh defaults HERMES_HOME to /opt/data
#    and would silently check the wrong tree if that default ever moved, and
#    gitops_workspace.agent_home() reads PLATFORM_AGENT_HOME to decide where a
#    leased clone goes. sshd starts sessions with neither.
SANDBOX_SSHD_DROPIN=/etc/ssh/sshd_config.d/10-sandbox-env.conf
SANDBOX_PATH=/opt/credential-proxy/bin:/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin
setenv_args="PATH=\"$SANDBOX_PATH\" HERMES_HOME=\"$DATA\" PLATFORM_AGENT_HOME=\"$DATA\""
# CREDENTIAL_PROXY_TOKEN_FILE is a path, not a token: the file it names is a
# projected volume, and forwarding the name is what lets the client read it. It
# has to cross with the URL rather than after it, because the broker authenticates
# every caller once it is off the agent's pod — which the sandbox being here
# already means — so a session that has the URL and not this one reaches the
# listener and is refused by it.
for name in CREDENTIAL_PROXY_URL CREDENTIAL_PROXY_TOKEN_FILE; do
  value="${!name-}"
  if [ -z "$value" ]; then
    continue
  fi
  # sshd_config is line-oriented, so a value carrying a newline would not be a
  # broken variable — it would be an extra directive, written by whoever
  # controls the pod's environment into the file that decides who may log in.
  # Quotes and backslashes go the same way: sshd's tokeniser, not ours.
  case $value in
  *[$'\n\r"\\']*)
    log "refusing to forward $name: the value contains a newline, quote or backslash"
    exit 1
    ;;
  esac
  setenv_args="$setenv_args $name=\"$value\""
done
install -d -m 0755 /etc/ssh/sshd_config.d
{
  echo "# Generated by sandbox-entrypoint from the pod environment. Do not edit."
  echo "SetEnv $setenv_args"
} >"$SANDBOX_SSHD_DROPIN"
chmod 0644 "$SANDBOX_SSHD_DROPIN"
# Fail here rather than in sshd. An invalid drop-in makes sshd exit during
# startup with a message about /etc/ssh/sshd_config.d/10-sandbox-env.conf, a
# file that exists in no source tree; `-t` names it while the entrypoint is
# still the thing running.
if ! sshd -t; then
  log "generated sshd config is invalid; refusing to start"
  exit 1
fi
if [ -z "${CREDENTIAL_PROXY_URL:-}" ]; then
  log "CREDENTIAL_PROXY_URL is unset — kubectl, gcloud, gh and git will report"
  log "that they are not configured. Expected until #737 Part C makes the"
  log "credential proxy reachable from outside the agent pod."
fi

log "ready; starting $*"
exec "$@"
