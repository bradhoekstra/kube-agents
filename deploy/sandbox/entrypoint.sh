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

WORKSPACE="${SANDBOX_WORKSPACE:-/workspace}"
AUTHORIZED_KEYS_SRC="${SANDBOX_AUTHORIZED_KEYS:-/etc/ssh-authorized/authorized_keys}"

# 1. The workspace. A PVC mounts over the image's /workspace and arrives owned
#    by root, so the agent could not write to its own working directory. Not
#    recursive: only the mount point needs fixing, and a recursive chown over a
#    volume that has been in use for a while is a slow way to start a pod.
if [ ! -d "$WORKSPACE" ]; then
  log "workspace $WORKSPACE does not exist"
  exit 1
fi
chown agent:agent "$WORKSPACE"

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

# 3. Host keys, on the volume rather than in the container. sshd_config
#    explains why they must survive a pod recycle; this creates them the first
#    time and leaves them alone afterwards.
install -d -m 0700 -o agent -g agent "$WORKSPACE/.sshd"
for type in ed25519 rsa; do
  key="$WORKSPACE/.sshd/ssh_host_${type}_key"
  if [ ! -f "$key" ]; then
    log "generating $type host key (first start on this volume)"
    ssh-keygen -q -t "$type" -N '' -f "$key"
  fi
  chown agent:agent "$key"
  chmod 600 "$key"
  # Guarded rather than assumed: a volume carrying a private key whose public
  # half was deleted is unusual but not impossible, and under `set -e` an
  # unguarded chown on the missing file would fail the pod start with an error
  # about a file sshd does not even read.
  if [ -f "$key.pub" ]; then
    chown agent:agent "$key.pub"
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
SANDBOX_SSHD_DROPIN=/etc/ssh/sshd_config.d/10-sandbox-env.conf
SANDBOX_PATH=/opt/credential-proxy/bin:/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin
setenv_args="PATH=\"$SANDBOX_PATH\""
# A one-element allowlist is still an allowlist. It is written as a loop because
# the next variable to cross this boundary should be added to a list, not have
# a second copy of this block written for it.
# shellcheck disable=SC2043
for name in CREDENTIAL_PROXY_URL; do
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
