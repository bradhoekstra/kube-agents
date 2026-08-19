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
#
# The rest of the list is deploy/docker/Dockerfile's own apt layer, minus what
# the sandbox must not have. Those packages are in the agent image because the
# agent's shell tools call them, and the shell is what moved here: a skill that
# pipes kubectl through jq works in the agent container today and has to keep
# working. bsdextrautils is the one addition rather than a copy — `column`
# appears 24 times across the shipped skills and is in neither image, because
# trixie split it out of util-linux.
#
# Left behind on purpose: apt-transport-https and gnupg exist in the agent
# image only to add Google's apt repository in the credential-proxy stage, and
# a sandbox that can add an apt repository is a sandbox that can install a real
# gcloud. ca-certificates is already in the python base.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      bsdextrautils \
      curl \
      dnsutils \
      iputils-ping \
      jq \
      nano \
      openssh-server \
      patch \
      vim \
      wget && \
    rm -rf /var/lib/apt/lists/*

# The credential-free utility CLIs, at the versions the agent image carried
# before #737 moved them here. This is now their only home — deploy/docker's
# step 2 says so and its guard enforces it — so a version bump has one place to
# happen. k9s did not come across: it is a full-screen terminal UI, and every
# command here arrives over a non-interactive `ssh host -- cmd` with no tty, so
# it could not run even if a skill asked for it. Nothing in agents/ does.
RUN ARCH=$(dpkg --print-architecture) && \
    curl -sSLf -o /usr/local/bin/yq "https://github.com/mikefarah/yq/releases/download/v4.53.3/yq_linux_${ARCH}" && \
    chmod +x /usr/local/bin/yq && \
    curl -sSLf -o /tmp/helm.tar.gz "https://get.helm.sh/helm-v3.15.2-linux-${ARCH}.tar.gz" && \
    tar -xzf /tmp/helm.tar.gz -C /tmp && \
    mv /tmp/linux-${ARCH}/helm /usr/local/bin/helm && \
    rm -rf /tmp/helm.tar.gz /tmp/linux-${ARCH}

# In the agent container `python3` resolves through PATH into the Hermes venv,
# so a shell command there gets these for free; here python3 is the bare base
# image. No skill script imports a third-party module today — the ones that do
# (credential_proxy.py, platform_mcp_server.py, session_kv_server.py,
# profile_cron_tick.py, cluster_agent_profile.py) all run in the agent pod — but
# that is an undeclared contract. Two packages rather than the venv's ~150: a
# script written on the fly parses YAML or makes an HTTP call, and reaches for
# the standard library for everything else.
#
# Not the google-cloud or kubernetes clients, which are the bulk of what is left
# there. Both authenticate before they are useful and the sandbox holds no
# credential for either to find, so they would import and then fail; a skill
# needing one goes through the wrapped CLIs, which is the whole arrangement.
RUN pip install --no-cache-dir pyyaml requests

# The login the agent connects as. `usermod -p '*'` matters: useradd leaves a
# '!' in /etc/shadow, which sshd reads as a locked account and refuses even for
# public-key auth. '*' means "no password login is possible", which is what is
# actually wanted.
#
# /run/sshd is sshd's privilege-separation directory and must exist before it
# will start.
#
# /opt/data is the model's durable directory, created here so the image is
# runnable without a volume; when the StatefulSet mounts its PVC over the top,
# the entrypoint re-establishes ownership. It carries the same path as the agent
# pod's Hermes home on purpose — the SOPs, skills and model-written scripts that
# name /opt/data resolve on both sides of the SSH connection instead of failing
# on a directory that exists in only one pod. The two are separate volumes and
# nothing is copied between them; the marker file the entrypoint writes is how a
# script or a person tells which side they are on.
#
# /var/lib/sandbox-sshd holds the host keys and is root-owned, deliberately on a
# different volume from the one above. The model owns /opt/data's mount point, so
# it can rename any entry there — a private key on that volume is one the model
# can read today or displace tomorrow, whatever the mode on the file itself.
RUN useradd --create-home --home-dir /home/agent --shell /bin/bash --uid 1000 agent && \
    usermod -p '*' agent && \
    install -d -m 0700 -o agent -g agent /home/agent/.ssh && \
    install -d -m 0755 /run/sshd && \
    install -d -m 0755 -o agent -g agent /opt/data && \
    install -d -m 0700 -o root -g root /var/lib/sandbox-sshd

# The second principal, for trusted code in the agent pod reaching in to run a
# cluster command. It exists because `agent` cannot be used for that: `agent`
# owns its own home directory, bash sources ~/.bashrc for a non-interactive
# `ssh host cmd`, and the model can therefore choose what an agent-pod caller
# sees — shadow a binary on PATH, and the forged output comes back as a trusted
# tool result. Debian's stock guard at the top of .bashrc returns early for
# non-interactive shells and hides this; the model owns the file and can delete
# the guard.
#
# Both principals are authorised by the same key. A second keypair would buy
# nothing: the private half lives only in the agent pod, which the model has no
# way to execute in once the shell, the file tools and execute_code are all
# over here.
#
# --no-user-group and an explicit gid keep this account out of the `agent`
# group. No skeleton files: /etc/skel/.bashrc in this home would be one more
# thing to reason about, and a non-interactive ssh command needs none of them.
# The home is 0700 and owned by hermes, so uid 1000 cannot read or write any
# part of it, .ssh included.
#
# .kubeconfigs is where the platform MCP server's `get-credentials` writes and
# its kubectl reads (platform_mcp_server._thread_kubeconfig_path). It has to be
# somewhere uid 1000 cannot write: a kubeconfig names an `exec` credential
# plugin — a binary and its argv — and kubectl runs it, so a kubeconfig the
# model can author is arbitrary code execution as the one account here it is not
# meant to reach. /opt/data and /tmp are both shared with `agent`. Created in
# the image rather than at first use, which keeps the runtime path to one round
# trip and the mode out of the hands of whatever creates it.
RUN groupadd --gid 1001 hermes && \
    useradd --no-create-home --no-user-group --gid 1001 --uid 1001 \
        --home-dir /home/hermes --shell /bin/bash hermes && \
    usermod -p '*' hermes && \
    install -d -m 0700 -o hermes -g hermes /home/hermes && \
    install -d -m 0700 -o hermes -g hermes /home/hermes/.ssh && \
    install -d -m 0700 -o hermes -g hermes /home/hermes/.kubeconfigs

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
