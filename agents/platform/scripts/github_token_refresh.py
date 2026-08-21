#!/usr/bin/env python3
"""
GKE Platform Agent — Secure GitHub Token Refresher (Broker Client)

In the agent sandbox this script asks the credential sidecar to refresh. Only
the sidecar queries Minty and caches the short-lived repository-scoped token.
"""

import json
import os
import subprocess
import sys
import time
import urllib.request
import urllib.error

# Ships alongside this script in the same directory, which is sys.path[0] both
# when the shell runs it and when the credential proxy execs it by absolute path.
import wif_credentials

TOKEN_BROKER_URL = os.getenv("TOKEN_BROKER_URL", "http://github-token-minter.kubeagents-system.svc.cluster.local:8080/token")

def log(msg: str):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [SRE-AUTH] {msg}", file=sys.stderr, flush=True)

def get_current_git_repo() -> str:
    """Extract repository name (owner/repo) from local git config."""
    try:
        res = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            capture_output=True, text=True, check=True
        )
        url = res.stdout.strip().strip("/")
        # Parse owner/repo from URL (supports HTTPS and SSH formats)
        # e.g., git@github.com:owner/repo.git or https://github.com/owner/repo.git
        if url.endswith(".git"):
            url = url[:-4]
        # Remove protocol prefix if present (e.g. https://)
        if "://" in url:
            url = url.split("://", 1)[1]
        # If SSH format, split by ':' (e.g. git@github.com:owner/repo)
        if "@" in url and ":" in url:
            url = url.split(":", 1)[1]
        
        parts = url.split("/")
        if len(parts) >= 2:
            return f"{parts[-2]}/{parts[-1]}"
    except Exception as e:
        log(f"WARNING: Could not parse repository from git config: {e}")
    return None

def refresh_git_credentials(target_repo: str = None) -> str:
    """Query local Minty, retrieve token, and cache inside git credentials."""
    repository = target_repo.strip().strip("/") if target_repo else get_current_git_repo()
    if not repository or "/" not in repository:
        raise RuntimeError("Could not identify target repository as owner/name")

    proxy_url = os.getenv("CREDENTIAL_PROXY_URL", "").strip()
    if proxy_url:
        request = urllib.request.Request(
            proxy_url.rstrip("/") + "/v1/github/refresh",
            data=json.dumps({"repository": repository}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                if response.status != 200:
                    raise RuntimeError("credential sidecar rejected refresh")
        except Exception as exc:
            raise RuntimeError("Credential sidecar failed to refresh GitHub auth") from exc
        log(f"GitHub credentials refreshed in credential sidecar for {repository}.")
        return ""

    # 1. Retrieve Google OIDC identity token.
    #
    # Federation first, and only when the container is actually running on a
    # federated credential -- fetch_identity_token returns None otherwise and
    # this falls through to the metadata server via gcloud, which is what every
    # placement other than the co-located sandbox proxy uses. The federated
    # branch exists because gcloud refuses to mint an ID token from an
    # external_account credential at all, so without it the co-located proxy can
    # reach GCP but not GitHub.
    oidc_token = wif_credentials.fetch_identity_token(TOKEN_BROKER_URL)
    if oidc_token:
        log("Minted the broker OIDC token through Workload Identity Federation.")
    else:
        try:
            oidc_token = subprocess.run(
                ["gcloud", "auth", "print-identity-token", f"--audiences={TOKEN_BROKER_URL}"],
                capture_output=True, text=True, check=True,
                timeout=10
            ).stdout.strip()
        except Exception as e1:
            # If --audiences fails (e.g., when running with human user credentials), retry without flags
            try:
                oidc_token = subprocess.run(
                    ["gcloud", "auth", "print-identity-token"],
                    capture_output=True, text=True, check=True,
                    timeout=10
                ).stdout.strip()
            except Exception as e2:
                raise RuntimeError(f"Failed to retrieve Google OIDC token via gcloud: {e2}") from e2

        if not oidc_token:
            raise RuntimeError("Retrieved Google OIDC token via gcloud is empty")

    # 2. Dynamically identify target repository from workspace git remote or parameter
    org_name, repo_name = repository.split("/", 1)

    headers = {
        "Content-Type": "application/json",
        "X-OIDC-Token": oidc_token
    }
    body = {
        "org_name": org_name,
        "repositories": [repo_name],
        "scope": "platform-agent-scope"
    }
    req_data = json.dumps(body).encode("utf-8")

    log(f"Requesting scoped installation token from Minty for repository: {org_name}/{repo_name}...")
    
    try:
        req = urllib.request.Request(
            TOKEN_BROKER_URL,
            data=req_data,
            headers=headers,
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            token = response.read().decode("utf-8").strip()
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8")
        raise RuntimeError(f"Minty returned error (HTTP {e.code}): {error_body}") from e
    except Exception as e:
        raise RuntimeError(f"Failed to connect to Minty at {TOKEN_BROKER_URL}: {e}") from e

    if not token:
        raise RuntimeError("Token received from Minty is empty")

    # 2. Configure GitHub CLI to securely cache the token in its internal state.
    env = os.environ.copy()
    if "GITHUB_TOKEN" in env:
        del env["GITHUB_TOKEN"]
    if "GH_TOKEN" in env:
        del env["GH_TOKEN"]
    subprocess.run(["gh", "auth", "login", "--with-token"], input=token, text=True, env=env, check=True)
    
    # 3. Configure Git to use gh as the credential helper
    subprocess.run(["gh", "auth", "setup-git"], env=env, check=True)
    
    log("Git credentials store successfully refreshed from Token Broker! Token cached.")
    return token

def main():
    try:
        target_repo = sys.argv[1] if len(sys.argv) > 1 else None
        refresh_git_credentials(target_repo)
    except Exception as e:
        log(f"FATAL: Failed to refresh git credentials: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
