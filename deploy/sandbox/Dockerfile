# The agent shell sandbox: the container the agent's terminal, file and
# code-execution tools actually run in, reached over SSH from the agent pod.
# See docs/designs/agent-shell-sandbox.md for why the shell moved out of the
# agent container at all.
#
# Two things this image is deliberately not:
#
#   - It is not the agent image. Nothing here can reach /opt/data, the session
#     database, or a service account token, and that separation is the entire
#     point of #737. It holds only while this image stays free of anything that
#     would give code running here a path back — which is why the credential
#     CLIs below are wrappers and the guard at the end of that stage fails the
#     build if a real one ever appears.
#   - It is not a general-purpose developer image. It carries a shell, Python,
#     an SSH server, and the credential-proxy wrappers. Everything else is a
#     deliberate addition, reviewed on the same terms.
#
# Base args mirror deploy/docker/Dockerfile so this can be rebuilt behind a
# firewall from a mirrored base. The pins live in images.json and
# hack/check-image-inventory.sh fails CI if the two drift apart.
#
# python:3.11-slim rather than a bare distro: execute_code runs `python3
# script.py` in the sandbox (tools/code_execution_tool.py probes for it with
# `command -v python3` and refuses the environment without it), so a Python is
# not optional here. Taking one that is already in the inventory costs nothing
# and adds no new upstream to mirror.
ARG PYTHON_IMAGE=python
ARG PYTHON_VERSION=3.11-slim
FROM ${PYTHON_IMAGE}:${PYTHON_VERSION}

# openssh-server is baked rather than installed by the startup command. The
# prototype this image replaces ran `apt-get install openssh-server` in the
# Sandbox pod's args on every cold start: slow, and a hard dependency on
# reaching a Debian mirror from a namespace whose whole purpose is to have no
# egress worth having.
#
# No rsync, deliberately. Hermes' ssh backend moves files with tar over the
# SSH connection (tools/environments/ssh.py: `tar cf - -C /` on the way back,
# `tar xf -` on the way in) and never shells out to rsync, so shipping it would
# only widen the image.
RUN apt-get update && \
    apt-get install -y --no-install-recommends openssh-server && \
    rm -rf /var/lib/apt/lists/*

# The login the agent connects as. `usermod -p '*'` matters: useradd leaves a
# '!' in /etc/shadow, which sshd reads as a locked account and refuses even for
# public-key auth. '*' means "no password login is possible", which is what is
# actually wanted.
#
# /run/sshd is sshd's privilege-separation directory and must exist before it
# will start. /workspace is created here so the image is runnable without a
# volume; when the Sandbox CR mounts its PVC over the top, the entrypoint
# re-establishes ownership.
RUN useradd --create-home --home-dir /home/agent --shell /bin/bash --uid 1000 agent && \
    usermod -p '*' agent && \
    install -d -m 0700 -o agent -g agent /home/agent/.ssh && \
    install -d -m 0755 /run/sshd && \
    install -d -m 0755 -o agent -g agent /workspace

# The credential-aware CLIs, exactly as the agent image provides them: symlinks
# to one client that forwards argv to the credential proxy over HTTP and prints
# back what the proxy's own copy of the binary produced. The agent never holds
# the credential, here no differently than in the agent container.
#
# credential-proxy-path.sh is shared with the agent image rather than copied,
# so the two cannot drift: it puts /opt/credential-proxy/bin ahead of the
# native binaries on the PATH of login shells. Hermes' ssh backend snapshots
# its environment once with `bash -l -c` and replays that snapshot for every
# later command (tools/environments/base.py), so a file in /etc/profile.d is
# read exactly the way it is for the local backend.
#
# The wrappers need CREDENTIAL_PROXY_URL to point at a reachable proxy. In the
# agent pod that is loopback; a sandbox in its own pod needs the proxy to be
# addressable over the network, which is Part C of #737 and is not done yet
# (docs/designs/credential-proxy-placement.md). Until it lands these wrappers
# resolve, run, and report "credential proxy unavailable" — which is the honest
# answer, and a much better one than a kubectl that is silently absent.
COPY --chmod=0755 agents/platform/scripts/credential_proxy_client.py /usr/local/bin/credential-proxy-exec
COPY --chmod=0755 deploy/shared/credential-proxy-path.sh /etc/profile.d/credential-proxy-path.sh
COPY --chmod=0644 deploy/sandbox/sshd_config /etc/ssh/sshd_config
COPY --chmod=0755 deploy/sandbox/entrypoint.sh /usr/local/bin/sandbox-entrypoint

# The same guard the agent image runs, for the same reason and with more at
# stake: a real gcloud or kubectl in this image is a credential path out of the
# sandbox that no network policy would see. /opt/credential-proxy/bin is not on
# the build PATH — profile.d puts it there at login — so `command -v` here can
# only be finding a native binary.
RUN mkdir -p /opt/credential-proxy/bin && \
    for binary in gcloud kubectl gh git; do \
        ln -s /usr/local/bin/credential-proxy-exec "/opt/credential-proxy/bin/${binary}"; \
    done && \
    for binary in gcloud kubectl gh git; do \
        if command -v "${binary}" >/dev/null 2>&1; then \
            echo "unexpected credential-aware CLI in the sandbox image: ${binary}" >&2; \
            exit 1; \
        fi; \
    done

# Not 22: sshd runs on an unprivileged port so this image has the option of
# running as a non-root user later without the port being what blocks it.
EXPOSE 2222

ENTRYPOINT ["/usr/local/bin/sandbox-entrypoint"]
CMD ["/usr/sbin/sshd", "-D", "-e"]
