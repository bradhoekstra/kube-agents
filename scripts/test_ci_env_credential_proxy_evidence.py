"""The broker's log and metrics are captured on every eval run, bounded, and never fail the run.

`hack/ci-env.sh`'s `collect_credential_proxy_evidence` runs from the eval
script's EXIT trap on green and red exits alike, beside `collect_gateway_log`
and `collect_litellm_log`. The broker's admission queue is in its log and its
Prometheus listener alone: the log used to be taken by the failure dump only
and the listener was never read, so a green run kept no record of the queue it
built up. These tests run the real function, lifted from the real file with
the constants it reads, under bash with a `kubectl` stub on PATH, so the bounds
it applies and the exit status it leaves behind are the ones the script has.
"""

import os
import pathlib
import re
import stat
import subprocess
import tempfile
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
ENV_SCRIPT = REPO_ROOT / "hack" / "ci-env.sh"
EVAL_SCRIPT = REPO_ROOT / "hack" / "ci-eval-pr.sh"
FUNCTION = "collect_credential_proxy_evidence"
LOG_ARTIFACT = "envoy-credential-proxy.log"
METRICS_ARTIFACT = "credential-proxy-metrics.prom"
DEPLOYMENT = "deployment/platform-agent-credential-proxy"
CONTAINER = "envoy-credential-proxy"
# The series the scrape exists to keep (agents/platform/scripts/credential_proxy.py).
HISTOGRAM = "kubeagents_credential_proxy_admission_wait_seconds"

# A kubectl that answers `get` for the Deployment (or refuses it with NotFound
# when STUB_NO_DEPLOYMENT is set, or fails outright when STUB_FAIL is), answers
# `logs` with its arguments on one line and then STUB_LINES lines of
# STUB_LINE_BYTES each, and answers `exec` by printing its arguments and a
# metrics body, as the container's python would.
KUBECTL_STUB = """#!/usr/bin/env bash
if [ -n "${STUB_FAIL:-}" ]; then
  echo "error: unable to reach the cluster" >&2
  exit 1
fi
verb=""
for arg in "$@"; do
  case "$arg" in
    get)
      if [ -n "${STUB_NO_DEPLOYMENT:-}" ]; then
        echo "Error from server (NotFound): deployments.apps \\"platform-agent-credential-proxy\\" not found (args: $*)" >&2
        exit 1
      fi
      echo "deployment.apps/platform-agent-credential-proxy"
      exit 0
      ;;
    logs|exec) verb="$arg"; break ;;
  esac
done
echo "ARGS: $*"
if [ "$verb" = "exec" ]; then
  echo "# HELP kubeagents_credential_proxy_admission_wait_seconds how long admission waited"
  echo "kubeagents_credential_proxy_admission_wait_seconds_count{bound=\\"slot\\"} 40"
  exit 0
fi
line=$(head -c "${STUB_LINE_BYTES:-100}" /dev/zero | tr '\\0' 'a')
for _ in $(seq 1 "${STUB_LINES:-3}"); do echo "$line"; done
"""


def lifted() -> str:
    """The function and the constants it reads, as written in ci-env.sh."""
    src = ENV_SCRIPT.read_text(encoding="utf-8")
    constants = re.findall(r"^readonly CREDENTIAL_PROXY_[A-Z_]+=.*$", src, re.MULTILINE)
    if len(constants) < 8:  # pragma: no cover - a rename should say so loudly
        raise AssertionError(f"expected the CREDENTIAL_PROXY_ constants in {ENV_SCRIPT}, found {constants}")
    match = re.search(rf"^{FUNCTION}\(\) \{{\n.*?^\}}$", src, re.DOTALL | re.MULTILINE)
    if match is None:  # pragma: no cover
        raise AssertionError(f"{FUNCTION}() not found in {ENV_SCRIPT}")
    return "\n".join([*constants, match.group(0)])


def constant(name: str) -> int:
    src = ENV_SCRIPT.read_text(encoding="utf-8")
    match = re.search(rf"^readonly {name}=(.+)$", src, re.MULTILINE)
    assert match is not None, name
    value = match.group(1).strip()
    if value.startswith("$(("):
        return int(eval(value[3:-2]))
    return int(value)


def run_collect(*then: str, **stub_env: str) -> tuple[subprocess.CompletedProcess, pathlib.Path]:
    """Run the lifted function once, then each line of `then` in the same bash."""
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="proxyev-"))
    stubs = tmp / "bin"
    stubs.mkdir()
    kubectl = stubs / "kubectl"
    kubectl.write_text(KUBECTL_STUB, encoding="utf-8")
    kubectl.chmod(kubectl.stat().st_mode | stat.S_IXUSR)
    artifacts = tmp / "artifacts"
    script = "\n".join(
        [
            "set -euo pipefail",
            lifted(),
            f"{FUNCTION}",
            'echo "STATUS AFTER: $?"',
            *then,
        ]
    )
    env = dict(
        os.environ,
        PATH=f"{stubs}:{os.environ['PATH']}",
        ARTIFACTS=str(artifacts),
        TARGET_NAMESPACE="test-ns",
        **stub_env,
    )
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False, env=env)
    return proc, artifacts


class CredentialProxyEvidenceTest(unittest.TestCase):
    def test_the_log_is_written_from_the_bounded_tail_of_the_broker_container(self):
        proc, artifacts = run_collect(STUB_LINES="3")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        first = (artifacts / LOG_ARTIFACT).read_text(encoding="utf-8").splitlines()[0]
        self.assertIn(f"logs {DEPLOYMENT} -c {CONTAINER} -n test-ns", first)
        self.assertIn(f"--tail={constant('CREDENTIAL_PROXY_LOG_TAIL_LINES')}", first)

    def test_the_metrics_are_scraped_inside_the_broker_container_by_its_own_python(self):
        """The listener binds the container's port, so the scrape runs where
        the port is, and reads the port from the env the operator sets rather
        than from a copy here."""
        proc, artifacts = run_collect()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        body = (artifacts / METRICS_ARTIFACT).read_text(encoding="utf-8")
        # The stub echoes its arguments first; the scrape program is one of
        # them and spans lines, so the echo is read as a whole.
        args, _, scraped = body.partition(HISTOGRAM)
        self.assertTrue(args.startswith(f"ARGS: exec {DEPLOYMENT} -c {CONTAINER} -n test-ns"), args)
        self.assertIn("-- python3 -c", args)
        self.assertIn("CREDENTIAL_PROXY_METRICS_PORT", args)
        self.assertIn("/metrics", args)
        self.assertIn("_count", scraped)

    def test_the_byte_cap_holds_whatever_the_lines_carry(self):
        cap = constant("CREDENTIAL_PROXY_LOG_MAX_BYTES")
        proc, artifacts = run_collect(STUB_LINES="200", STUB_LINE_BYTES=str(cap // 100))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual((artifacts / LOG_ARTIFACT).stat().st_size, cap)

    def test_a_missing_deployment_is_a_note_not_a_failure(self):
        proc, artifacts = run_collect(STUB_NO_DEPLOYMENT="1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("STATUS AFTER: 0", proc.stdout)
        note = (artifacts / LOG_ARTIFACT).read_text(encoding="utf-8")
        self.assertIn("note: no credential-proxy evidence for this run", note)
        self.assertIn("NotFound", note)
        self.assertFalse((artifacts / METRICS_ARTIFACT).exists(), "no Deployment, no scrape")

    def test_a_kubectl_that_fails_leaves_the_status_alone(self):
        """The trap reads `$?` for the dumper after this runs, and a cluster
        that cannot be reached must cost the run its evidence and nothing else."""
        proc, artifacts = run_collect(STUB_FAIL="1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("STATUS AFTER: 0", proc.stdout)
        self.assertTrue((artifacts / LOG_ARTIFACT).is_file())

    def test_the_reads_are_pinned_to_the_agent_cluster_when_the_pin_is_known(self):
        proc, artifacts = run_collect(AGENT_CLUSTER_CONTEXT="gke_proj_region_host")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for artifact in (LOG_ARTIFACT, METRICS_ARTIFACT):
            first = (artifacts / artifact).read_text(encoding="utf-8").splitlines()[0]
            self.assertTrue(first.startswith("ARGS: --context gke_proj_region_host "), first)
        proc, artifacts = run_collect(AGENT_CLUSTER_CONTEXT="")
        first = (artifacts / LOG_ARTIFACT).read_text(encoding="utf-8").splitlines()[0]
        self.assertNotIn("--context", first)

    def test_a_second_call_in_the_same_process_keeps_the_first_capture(self):
        """ci-eval-pr.sh takes the evidence before the rollback round trip
        re-renders the proxy; a later call in the same process must not
        overwrite the eval's capture with the replacement pod's."""
        proc, artifacts = run_collect(f"STUB_LINES=7 {FUNCTION}", 'echo "SECOND STATUS: $?"', STUB_LINES="3")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("SECOND STATUS: 0", proc.stdout)
        self.assertEqual(len((artifacts / LOG_ARTIFACT).read_text(encoding="utf-8").splitlines()), 4)

    def test_the_eval_trap_the_round_trip_and_the_failure_dumper_all_call_it(self):
        env_src = ENV_SCRIPT.read_text(encoding="utf-8")
        eval_src = EVAL_SCRIPT.read_text(encoding="utf-8")
        dumper = re.search(r"^dump_prow_artifacts_on_failure\(\) \{\n.*?^\}$", env_src, re.DOTALL | re.MULTILINE)
        trap = re.search(r"^profile_and_dump_on_exit\(\) \{\n.*?^\}$", eval_src, re.DOTALL | re.MULTILINE)
        self.assertIsNotNone(dumper)
        self.assertIsNotNone(trap)
        self.assertIn(f"    {FUNCTION}\n", dumper.group(0))
        self.assertIn(f"  {FUNCTION}\n", trap.group(0))
        # Before the rollback round trip as well, beside the gateway log.
        self.assertRegex(eval_src, r"collect_agent_pod_diagnostics --keep-watch\n  " + FUNCTION + r"\n")
        # The dumper's own tail of the same file is gone: it would overwrite
        # the every-run capture with a later pod's on a red run.
        self.assertNotIn(f"> \"${{artifact_dir}}/{LOG_ARTIFACT}\"", dumper.group(0))


if __name__ == "__main__":
    unittest.main()
