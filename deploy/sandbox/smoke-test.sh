#!/usr/bin/env bash
# Smoke test for the agent shell sandbox image: starts a container, connects to
# it the way the agent pod does, and checks the things this image exists to
# provide.
#
# Worth having as a file rather than a checklist because none of it is visible
# from the Dockerfile. Whether a variable reaches the agent's shell depends on
# sshd's parser, the session type, and which of three mechanisms sets it; the
# first version of this image got PATH right and CREDENTIAL_PROXY_URL wrong,
# and every static check in the repository passed on it.
#
# Usage: deploy/sandbox/smoke-test.sh [image] [port]
#
# shellcheck disable=SC2016
#   Remote commands are single-quoted throughout and that is the point: the
#   expansion has to happen in the sandbox, not in this shell. A double-quoted
#   `echo "$PATH"` would test the caller's PATH and pass.
#
# No `set -e`: a failing check is data this script reports, not a reason to
# abandon the run. `check` counts them and the exit status at the bottom is the
# verdict.
set -uo pipefail

IMAGE="${1:-agent-sandbox:latest}"
PORT="${2:-12222}"
NAME="sandbox-smoke-$$"
WORK=$(mktemp -d)
# Named volumes rather than bind mounts, for the ownership. A bind mount arrives
# owned by whoever ran this script; a named volume is seeded from the image, so
# /opt/data arrives agent-owned and /var/lib/sandbox-sshd root-owned — which is
# what a PVC does and what the entrypoint's root-ownership check expects. It
# also means nothing here has to chown a 0700 directory back out of the
# container before `rm -rf` can finish.
DATA_VOL="$NAME-data"
SSHD_VOL="$NAME-sshd"
PASS=0
FAIL=0

cleanup() {
  docker rm -f "$NAME" "$NAME-nourl" "$NAME-badsshd" >/dev/null 2>&1
  docker volume rm -f "$DATA_VOL" "$SSHD_VOL" >/dev/null 2>&1
  rm -rf "$WORK"
}
trap cleanup EXIT

check() { # check <label> <expected-substring> <actual>
  if [[ -n "$2" && "$3" == *"$2"* ]]; then
    echo "PASS  $1"
    PASS=$((PASS + 1))
  else
    echo "FAIL  $1"
    # An empty expectation matches everything, so it is a broken assertion
    # rather than a passing one. Say which, or it reads as a real failure.
    [ -n "$2" ] || echo "        (empty expectation — the assertion is wrong, not the image)"
    echo "        want substring: $2"
    echo "        got: $3"
    FAIL=$((FAIL + 1))
  fi
}

check_absent() { # check_absent <label> <forbidden-substring> <actual>
  if [[ "$3" != *"$2"* ]]; then
    echo "PASS  $1"
    PASS=$((PASS + 1))
  else
    echo "FAIL  $1"
    echo "        must not contain: $2"
    echo "        got: $3"
    FAIL=$((FAIL + 1))
  fi
}

ssh-keygen -q -t ed25519 -N '' -f "$WORK/id" -C sandbox-smoke
mkdir -p "$WORK/keys"
cp "$WORK/id.pub" "$WORK/keys/authorized_keys"
chmod 644 "$WORK/keys/authorized_keys"

# IdentitiesOnly: without it ssh also offers every key in the caller's agent, and
# a refused login comes back as "Too many authentication failures" — which passes
# a naive check for a refusal while proving nothing about why.
SSH_OPTS=(-i "$WORK/id" -p "$PORT" -o IdentitiesOnly=yes
  -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null
  -o LogLevel=ERROR -o BatchMode=yes -o ConnectTimeout=5)
SSH=(ssh "${SSH_OPTS[@]}" agent@127.0.0.1)

# Waits for sshd to answer rather than sleeping: the host-key generation on a
# first start is slow enough on a loaded runner to lose a fixed sleep to, and a
# flaky smoke test gets deleted rather than debugged.
start_sandbox() {
  docker rm -f "$NAME" >/dev/null 2>&1
  docker run -d --name "$NAME" -p "$PORT:2222" \
    -v "$WORK/keys:/etc/ssh-authorized:ro" \
    -v "$DATA_VOL:/opt/data" \
    -v "$SSHD_VOL:/var/lib/sandbox-sshd" \
    -e CREDENTIAL_PROXY_URL=http://127.0.0.1:9999 \
    "$IMAGE" >/dev/null
  for _ in $(seq 30); do
    ssh-keyscan -p "$PORT" -t ed25519 127.0.0.1 >/dev/null 2>&1 && return 0
    sleep 1
  done
  echo "FAIL  sandbox never accepted connections; logs follow" >&2
  docker logs "$NAME" >&2
  return 1
}

echo "== 1. a sandbox with no key mounted must fail loudly =="
# sshd would otherwise start happily and refuse every connection with
# "Permission denied (publickey)", which reads as a key mismatch on the agent
# side and sends whoever is debugging it to the wrong pod.
check "exits with a pointed message when no key is mounted" "the agent could not log in" \
  "$(docker run --rm "$IMAGE" 2>&1)"

echo
echo "== 2. startup =="
start_sandbox || exit 1
logs=$(docker logs "$NAME" 2>&1)
check "generated host keys on first start" "generating ed25519 host key" "$logs"
check "reached exec" "ready; starting" "$logs"
check "sshd is pid 1" "sshd" "$(docker exec "$NAME" ps -o comm= -p 1 2>&1)"
# From inside the container: the state directory is 0700 root:root, so listing it
# from the host would fail for reasons unrelated to whether the keys are there.
check "host keys landed on their volume" "ssh_host_ed25519_key" \
  "$(docker exec "$NAME" ls /var/lib/sandbox-sshd 2>&1)"
check "the data volume is the agent's" "1000" \
  "$(docker exec "$NAME" stat -c '%u' /opt/data 2>&1)"

echo
echo "== 3. who may log in =="
check "the agent's key works" "agent" "$("${SSH[@]}" whoami 2>&1)"
# sshd's own default, which is the home. It is not where the agent works: Hermes
# is sent TERMINAL_CWD=/opt/data by the operator, because this home is the
# container's ephemeral overlay and everything written here is gone on the next
# pod recycle. The image cannot enforce that — asserted here so the two halves of
# the arrangement are visible together.
check "the session starts in the agent's home" "/home/agent" "$("${SSH[@]}" pwd 2>&1)"
check "the data volume is writable" "ok" \
  "$("${SSH[@]}" 'touch /opt/data/probe && echo ok' 2>&1)"
# One path, two directories: /opt/data is also the agent pod's Hermes home, and
# the marker is how a script or a person tells which side of the SSH connection
# it is looking at.
check "the data volume says which /opt/data it is" "shell sandbox" \
  "$("${SSH[@]}" 'cat /opt/data/.sandbox' 2>&1)"
check "root is refused" "Permission denied" \
  "$(ssh "${SSH_OPTS[@]}" root@127.0.0.1 whoami 2>&1)"
# Two things stop a third account from using the same key: AllowUsers names the
# two that may log in, and AuthorizedKeysFile is %h-relative so an account with
# no authorized_keys of its own has nothing to authenticate against. Refusing
# root alone would prove only PermitRootLogin. uid 1002 because 1001 is hermes,
# and a useradd that fails on a duplicate uid would make this pass for the wrong
# reason.
docker exec "$NAME" useradd -m -u 1002 intruder >/dev/null 2>&1
check "AllowUsers refuses another account holding the same key" "Permission denied" \
  "$(ssh "${SSH_OPTS[@]}" intruder@127.0.0.1 whoami 2>&1)"

echo
echo "== 3b. the hermes principal =="
# The account trusted agent-pod code connects as. It exists so that a caller
# reaching in for a cluster command does not run as the login whose home the
# model owns; see deploy/sandbox/Dockerfile.
HERMES_SSH=(ssh "${SSH_OPTS[@]}" hermes@127.0.0.1)
check "the same key opens a hermes session" "hermes" "$("${HERMES_SSH[@]}" whoami 2>&1)"
check "hermes gets the forwarded proxy URL too" "http://127.0.0.1:9999" \
  "$("${HERMES_SSH[@]}" 'echo "$CREDENTIAL_PROXY_URL"' 2>&1)"
check "hermes gets the wrapper PATH too" "/opt/credential-proxy/bin/kubectl" \
  "$("${HERMES_SSH[@]}" 'command -v kubectl' 2>&1)"

# The property the whole account exists for. Bash sources ~/.bashrc for a
# non-interactive `ssh host cmd`, so a shim planted by the model in its own home
# would be executed by an agent-pod caller and its output returned as a trusted
# tool result. Written above Debian's non-interactive guard, because below it
# the guard hides the problem rather than fixing it.
"${SSH[@]}" 'mkdir -p ~/bin && printf "#!/bin/sh\necho FORGED\n" > ~/bin/kubectl && chmod +x ~/bin/kubectl' >/dev/null 2>&1
"${SSH[@]}" 'printf "export PATH=$HOME/bin:\$PATH\n%s" "$(cat ~/.bashrc)" > ~/.bashrc.new && mv ~/.bashrc.new ~/.bashrc' >/dev/null 2>&1
check_absent "a shim planted in the agent's home does not reach a hermes session" "FORGED" \
  "$("${HERMES_SSH[@]}" 'kubectl version' 2>&1)"
# Same command as the agent proves the planted shim actually works, so the check
# above is testing isolation rather than a typo in the setup.
check "the planted shim does run for the agent itself" "FORGED" \
  "$("${SSH[@]}" 'kubectl version' 2>&1)"

# The model must not be able to reach hermes' authorized_keys, either by writing
# into its home or by authorising a new key in its own.
check "the agent cannot enter the hermes home" "Permission denied" \
  "$("${SSH[@]}" 'ls /home/hermes/.ssh' 2>&1)"

# The kubeconfig directory the platform MCP server writes to. A kubeconfig names
# an exec credential plugin and kubectl runs it, so one the model can author is
# arbitrary code execution as hermes. It lives inside the 0700 home for that
# reason and not for tidiness.
check "hermes can write its kubeconfig directory" "700 hermes" \
  "$("${HERMES_SSH[@]}" 'stat -c "%a %U" /home/hermes/.kubeconfigs' 2>&1)"
check "the agent cannot write a kubeconfig for hermes to use" "Permission denied" \
  "$("${SSH[@]}" 'touch /home/hermes/.kubeconfigs/planted.yaml' 2>&1)"
# Generated on the host so the private half never has to be copied back out of
# the container: the model only needs the public half to authorise it.
ssh-keygen -q -t ed25519 -N '' -f "$WORK/rogue" -C sandbox-smoke-rogue
"${SSH[@]}" "printf '%s\n' '$(cat "$WORK/rogue.pub")' >> ~/.ssh/authorized_keys" >/dev/null 2>&1
check "the model can authorise a new key for its own account" "agent" \
  "$(ssh -i "$WORK/rogue" "${SSH_OPTS[@]:2}" agent@127.0.0.1 whoami 2>&1)"
# Asserted as a refusal rather than as the absence of "hermes" in the output:
# sshd's own denial names the account it refused ("hermes@127.0.0.1: Permission
# denied"), so a check for that substring can never pass however well the image
# behaves.
check "the same key does not open a hermes session" "Permission denied" \
  "$(ssh -i "$WORK/rogue" "${SSH_OPTS[@]:2}" hermes@127.0.0.1 whoami 2>&1)"

# Undo the sabotage: later sections use the agent session and would otherwise
# run against a hijacked PATH.
"${SSH[@]}" 'rm -rf ~/bin && sed -i "1{/^export PATH=/d}" ~/.bashrc && sed -i "/sandbox-smoke-rogue/d" ~/.ssh/authorized_keys' >/dev/null 2>&1

echo
echo "== 3c. the host keys are not the model's =="
# Both clients pin the host key with StrictHostKeyChecking=accept-new, which is
# worth nothing if the sandboxed account holds the private half. An earlier build
# kept these under the model's volume and chowned them to uid 1000 — the pin
# still looked configured, and the model could read the key it pinned.
check "the agent cannot read the host private key" "Permission denied" \
  "$("${SSH[@]}" 'cat /var/lib/sandbox-sshd/ssh_host_ed25519_key' 2>&1)"
# Mode bits on the key file alone would not settle this. Ownership of the
# directory is what stops the model renaming it aside and having the entrypoint
# populate a replacement it controls on the next start.
check "the host key directory is root's" "700 root" \
  "$(docker exec "$NAME" stat -c '%a %U' /var/lib/sandbox-sshd 2>&1)"
check "the agent cannot write the host key directory" "Permission denied" \
  "$("${SSH[@]}" 'touch /var/lib/sandbox-sshd/planted' 2>&1)"
# And the split has to stay a split: nested under the data volume, everything
# above is undone by the mount point the model owns.
check_absent "the host keys are not under the model's volume" "/opt/data" \
  "$(docker exec "$NAME" sh -c 'grep "^HostKey" /etc/ssh/sshd_config' 2>&1)"
# The entrypoint refuses rather than trusting the deployment to get this right.
# --entrypoint, then chown, then the real entrypoint: the only way to hand it a
# state directory the sandboxed account owns.
check "the entrypoint refuses a state directory the model could write" "not root" \
  "$(docker run --rm --name "$NAME-badsshd" -v "$WORK/keys:/etc/ssh-authorized:ro" \
    --entrypoint bash "$IMAGE" -c \
    'chown agent:agent /var/lib/sandbox-sshd && exec /usr/local/bin/sandbox-entrypoint /usr/sbin/sshd -D -e' 2>&1)"

echo
echo "== 4. what the agent's tools need to find =="
check "python3 exists (execute_code probes for it)" "python3" \
  "$("${SSH[@]}" 'command -v python3' 2>&1)"
check "tar exists (file sync is tar over ssh, not sftp)" "tar" \
  "$("${SSH[@]}" 'command -v tar' 2>&1)"
# Not SSH_OPTS: sftp spells the port -P, and -p means something else entirely.
check "no sftp subsystem is advertised" "subsystem request failed" \
  "$(sftp -i "$WORK/id" -P "$PORT" -o IdentitiesOnly=yes -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o BatchMode=yes \
    agent@127.0.0.1 </dev/null 2>&1)"

echo
echo "== 5. credential-proxy wrappers =="
for cli in kubectl gcloud gh git; do
  check "$cli resolves to the wrapper, not 'command not found'" "/opt/credential-proxy/bin/$cli" \
    "$("${SSH[@]}" "command -v $cli" 2>&1)"
done
# Non-login is the shape of every command the agent sends, and the case that
# reads no /etc/profile. This is the check that caught the original bug: PATH
# arrived and CREDENTIAL_PROXY_URL did not, so the wrappers resolved and then
# refused to run.
check "CREDENTIAL_PROXY_URL crosses into a non-login session" "http://127.0.0.1:9999" \
  "$("${SSH[@]}" 'echo "$CREDENTIAL_PROXY_URL"' 2>&1)"
check "and into a login session" "http://127.0.0.1:9999" \
  "$("${SSH[@]}" 'bash -l -c "echo \$CREDENTIAL_PROXY_URL"' 2>&1)"
check "the wrapper dispatches rather than refusing to start" "credential proxy" \
  "$("${SSH[@]}" 'kubectl version 2>&1' 2>&1)"
check "the wrappers are ahead of anything else on PATH" "/opt/credential-proxy/bin:" \
  "$("${SSH[@]}" 'echo "$PATH"' 2>&1)"
# A login shell runs /etc/profile, which overwrites PATH wholesale; profile.d is
# what puts the wrappers back. Both paths, because only one of them is sshd's.
check "PATH survives /etc/profile in a login shell" "/opt/credential-proxy/bin/kubectl" \
  "$("${SSH[@]}" 'bash -l -c "command -v kubectl"' 2>&1)"

echo
echo "== 6. a restart must not change the host key or lose the model's work =="
# Hermes connects with StrictHostKeyChecking=accept-new, which accepts a key it
# has never seen and refuses one that changed. A regenerated host key is not a
# prompt, it is every later command failing until known_hosts is cleared by hand.
# Two files, one on each side of the durability line: /opt/data/probe was
# written in section 3 and this one goes in the home the shell would default to.
"${SSH[@]}" 'touch ~/ephemeral-probe' >/dev/null 2>&1
before=$(ssh-keyscan -p "$PORT" -t ed25519 127.0.0.1 2>/dev/null | awk '{print $3}')
start_sandbox || exit 1
after=$(ssh-keyscan -p "$PORT" -t ed25519 127.0.0.1 2>/dev/null | awk '{print $3}')
check "same host key after a recycle" "$before" "$after"
check_absent "the second start reused the volume's keys" "generating ed25519" \
  "$(docker logs "$NAME" 2>&1)"
# The other half of the reason the volumes exist, and the reason the operator
# sends TERMINAL_CWD=/opt/data: without it the shell defaults to `~`, which is
# the container overlay below, and a live install ran for five days with the
# model's work on the wrong side of this line.
check "the model's files on the data volume survived the recycle" "probe" \
  "$("${SSH[@]}" 'ls /opt/data' 2>&1)"
check_absent "the ones in the home did not" "ephemeral-probe" \
  "$("${SSH[@]}" 'ls -a ~' 2>&1)"

echo
echo "== 7. an unconfigured proxy warns, it does not crash =="
# Expected state until #737 Part C makes the credential proxy reachable from
# outside the agent pod: file and code-execution tools still have to work.
docker rm -f "$NAME-nourl" >/dev/null 2>&1
docker run -d --name "$NAME-nourl" -v "$WORK/keys:/etc/ssh-authorized:ro" "$IMAGE" >/dev/null
sleep 3
check "says so in the log" "CREDENTIAL_PROXY_URL is unset" "$(docker logs "$NAME-nourl" 2>&1)"
check "starts sshd anyway" "sshd" "$(docker exec "$NAME-nourl" ps -o comm= -p 1 2>&1)"
docker rm -f "$NAME-nourl" >/dev/null 2>&1

echo
echo "== 8. a newline in a forwarded value is an sshd_config injection =="
# The pod environment is not attacker-controlled today. It is the only untrusted
# input this entrypoint copies into a file that decides who may log in, which is
# a short enough distance to be worth a guard and a test.
out=$(docker run --rm -v "$WORK/keys:/etc/ssh-authorized:ro" \
  -e $'CREDENTIAL_PROXY_URL=http://x\nPermitRootLogin yes' "$IMAGE" 2>&1)
check "refuses the value" "contains a newline, quote or backslash" "$out"
check_absent "and does not start sshd with it" "ready; starting" "$out"

echo
docker image inspect "$IMAGE" --format '{{len .RootFS.Layers}} {{.Size}}' 2>/dev/null |
  awk '{printf "== %s layers, %.0f MB ==\n", $1, $2/1024/1024}'

echo
echo "$PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
