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
PASS=0
FAIL=0

cleanup() {
  docker rm -f "$NAME" "$NAME-nourl" >/dev/null 2>&1
  # The container writes into the volume as uid 1000 and creates a 0700
  # directory there, so an unprivileged `rm -rf` on the host cannot finish.
  # --entrypoint: without it this runs sandbox-entrypoint, which exits on the
  # missing authorized_keys long before it would reach the chown.
  docker run --rm --entrypoint chown -v "$WORK:/w" "$IMAGE" \
    -R "$(id -u):$(id -g)" /w >/dev/null 2>&1
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
mkdir -p "$WORK/keys" "$WORK/vol"
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
    -v "$WORK/vol:/workspace" \
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
# From inside the container: .sshd is 0700 owned by uid 1000, so listing it from
# the host would fail for reasons unrelated to whether the keys are there.
check "host keys landed on the volume" "ssh_host_ed25519_key" \
  "$(docker exec "$NAME" ls /workspace/.sshd 2>&1)"

echo
echo "== 3. who may log in =="
check "the agent's key works" "agent" "$("${SSH[@]}" whoami 2>&1)"
check "the session starts in the agent's home" "/home/agent" "$("${SSH[@]}" pwd 2>&1)"
check "the workspace is writable" "ok" \
  "$("${SSH[@]}" 'touch /workspace/probe && echo ok' 2>&1)"
check "root is refused" "Permission denied" \
  "$(ssh "${SSH_OPTS[@]}" root@127.0.0.1 whoami 2>&1)"
# AuthorizedKeysFile is an absolute path, so every account on the box would
# authenticate with the agent's key. AllowUsers is the only thing stopping it;
# refusing root alone would prove only PermitRootLogin.
docker exec "$NAME" useradd -m -u 1001 intruder >/dev/null 2>&1
check "AllowUsers refuses another account holding the same key" "Permission denied" \
  "$(ssh "${SSH_OPTS[@]}" intruder@127.0.0.1 whoami 2>&1)"

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
echo "== 6. a restart must not change the host key =="
# Hermes connects with StrictHostKeyChecking=accept-new, which accepts a key it
# has never seen and refuses one that changed. A regenerated host key is not a
# prompt, it is every later command failing until known_hosts is cleared by hand.
before=$(ssh-keyscan -p "$PORT" -t ed25519 127.0.0.1 2>/dev/null | awk '{print $3}')
start_sandbox || exit 1
after=$(ssh-keyscan -p "$PORT" -t ed25519 127.0.0.1 2>/dev/null | awk '{print $3}')
check "same host key after a recycle" "$before" "$after"
check_absent "the second start reused the volume's keys" "generating ed25519" \
  "$(docker logs "$NAME" 2>&1)"

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
