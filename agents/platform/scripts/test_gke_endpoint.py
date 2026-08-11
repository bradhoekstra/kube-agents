"""Unit tests for gke_endpoint.dns_endpoint_args (the --dns-endpoint decision).

Run: python3 -m unittest agents.platform.scripts.test_gke_endpoint

Every case drives a fake runner rather than gcloud, so the predicate is pinned
without a project or a network. The shapes below are real describe output: the
`allowExternalTraffic: false` one is `kube-agents-cluster` in `bhoekstra-gkedemos`,
the cluster that proved passing the flag blindly yields a kubeconfig which 403s.
"""

import io
import json
import subprocess
import sys
import unittest
from contextlib import redirect_stderr
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import gke_endpoint  # noqa: E402

HELP_WITH_FLAG = "    --dns-endpoint\n        Whether to use the DNS-based endpoint.\n"
HELP_WITHOUT_FLAG = "    --internal-ip\n        Use the internal IP address.\n"

# Both endpoints present, DNS open to the outside: the case this feature exists for.
DNS_EXTERNAL = {
    "controlPlaneEndpointsConfig": {
        "dnsEndpointConfig": {
            "allowExternalTraffic": True,
            "endpoint": "gke-abc123.us-central1.gke.goog",
        },
        "ipEndpointsConfig": {"enabled": True, "enablePublicEndpoint": True},
    }
}

# A DNS endpoint exists but refuses external traffic. gcloud only errors for
# non-Googlers here, so the flag must be withheld on the configuration, not on
# whether the command happened to fail.
DNS_INTERNAL_ONLY = {
    "controlPlaneEndpointsConfig": {
        "dnsEndpointConfig": {
            "allowExternalTraffic": False,
            "endpoint": "gke-a13c947a2043445a8340cc7620e4b30d2389-757207957170.us-central1.gke.goog",
        },
        "ipEndpointsConfig": {
            "enabled": True,
            "enablePublicEndpoint": True,
            "privateEndpoint": "10.128.0.6",
            "publicEndpoint": "35.253.54.92",
        },
    }
}

# A cluster old enough to predate DNS endpoints entirely.
NO_DNS_BLOCK = {"controlPlaneEndpointsConfig": {"ipEndpointsConfig": {"enabled": True}}}


class FakeRunner:
    """Answers the help probe and the describe, and records what it was asked."""

    def __init__(self, describe=None, help_text=HELP_WITH_FLAG, describe_exit=0):
        self.describe = describe
        self.help_text = help_text
        self.describe_exit = describe_exit
        self.calls: list[list[str]] = []

    def __call__(self, argv):
        self.calls.append(argv)
        if "--help" in argv:
            return 0, self.help_text
        if "describe" in argv:
            if self.describe_exit != 0:
                return self.describe_exit, ""
            payload = self.describe if isinstance(self.describe, str) else json.dumps(self.describe)
            return 0, payload
        raise AssertionError(f"unexpected command: {argv}")

    @property
    def describe_calls(self):
        return [c for c in self.calls if "describe" in c]


def decide(runner, project="p", cluster="c", location="us-central1"):
    """Run the decision with a clean cache and stderr swallowed."""
    gke_endpoint.reset_cache()
    with redirect_stderr(io.StringIO()):
        return gke_endpoint.dns_endpoint_args(project, cluster, location, run=runner)


class PredicateTest(unittest.TestCase):
    def test_external_dns_endpoint_gets_the_flag(self):
        self.assertEqual(decide(FakeRunner(DNS_EXTERNAL)), ["--dns-endpoint"])

    def test_external_traffic_disabled_gets_no_flag(self):
        # The regression this whole module guards: gcloud would have accepted the
        # flag for an internal caller and produced a kubeconfig that 403s.
        self.assertEqual(decide(FakeRunner(DNS_INTERNAL_ONLY)), [])

    def test_cluster_without_a_dns_endpoint_gets_no_flag(self):
        self.assertEqual(decide(FakeRunner(NO_DNS_BLOCK)), [])

    def test_empty_describe_gets_no_flag(self):
        self.assertEqual(decide(FakeRunner({})), [])

    def test_endpoint_present_but_allow_external_traffic_absent(self):
        # Absent is a no, not a maybe.
        shape = {"controlPlaneEndpointsConfig": {"dnsEndpointConfig": {"endpoint": "x.gke.goog"}}}
        self.assertEqual(decide(FakeRunner(shape)), [])

    def test_allow_external_traffic_true_but_no_endpoint(self):
        shape = {
            "controlPlaneEndpointsConfig": {
                "dnsEndpointConfig": {"allowExternalTraffic": True, "endpoint": ""}
            }
        }
        self.assertEqual(decide(FakeRunner(shape)), [])


class DegradesQuietlyTest(unittest.TestCase):
    """A cluster we cannot ask about must behave exactly as it did before."""

    def test_describe_failure_is_not_fatal(self):
        self.assertEqual(decide(FakeRunner(DNS_EXTERNAL, describe_exit=1)), [])

    def test_unparseable_describe_is_not_fatal(self):
        self.assertEqual(decide(FakeRunner("not json at all")), [])

    def test_runner_raising_is_not_fatal(self):
        def explode(argv):
            if "--help" in argv:
                return 0, HELP_WITH_FLAG
            raise subprocess.TimeoutExpired(argv, 30)

        self.assertEqual(decide(explode), [])

    def test_oserror_from_missing_gcloud_is_not_fatal(self):
        def no_gcloud(argv):
            raise OSError("No such file or directory: 'gcloud'")

        self.assertEqual(decide(no_gcloud), [])

    def test_incomplete_target_is_not_described(self):
        runner = FakeRunner(DNS_EXTERNAL)
        gke_endpoint.reset_cache()
        self.assertEqual(gke_endpoint.dns_endpoint_args("p", "", "us-central1", run=runner), [])
        self.assertEqual(runner.calls, [])


class GcloudSupportTest(unittest.TestCase):
    def test_old_gcloud_gets_no_flag_and_is_never_asked_to_describe(self):
        runner = FakeRunner(DNS_EXTERNAL, help_text=HELP_WITHOUT_FLAG)
        self.assertEqual(decide(runner), [])
        self.assertEqual(runner.describe_calls, [])

    def test_support_probe_is_memoised(self):
        runner = FakeRunner(DNS_EXTERNAL)
        gke_endpoint.reset_cache()
        with redirect_stderr(io.StringIO()):
            gke_endpoint.dns_endpoint_args("p", "c1", "us-central1", run=runner)
            gke_endpoint.dns_endpoint_args("p", "c2", "us-central1", run=runner)
        self.assertEqual(len([c for c in runner.calls if "--help" in c]), 1)


class CacheTest(unittest.TestCase):
    def test_same_cluster_is_described_once(self):
        runner = FakeRunner(DNS_EXTERNAL)
        gke_endpoint.reset_cache()
        with redirect_stderr(io.StringIO()):
            first = gke_endpoint.dns_endpoint_args("p", "c", "us-central1", run=runner)
            second = gke_endpoint.dns_endpoint_args("p", "c", "us-central1", run=runner)
        self.assertEqual(first, ["--dns-endpoint"])
        self.assertEqual(second, ["--dns-endpoint"])
        self.assertEqual(len(runner.describe_calls), 1)

    def test_distinct_clusters_are_described_separately(self):
        runner = FakeRunner(DNS_EXTERNAL)
        gke_endpoint.reset_cache()
        with redirect_stderr(io.StringIO()):
            gke_endpoint.dns_endpoint_args("p", "c", "us-central1", run=runner)
            gke_endpoint.dns_endpoint_args("p", "c", "europe-west1", run=runner)
        self.assertEqual(len(runner.describe_calls), 2)

    def test_caller_cannot_mutate_the_cached_answer(self):
        runner = FakeRunner(DNS_EXTERNAL)
        gke_endpoint.reset_cache()
        with redirect_stderr(io.StringIO()):
            first = gke_endpoint.dns_endpoint_args("p", "c", "us-central1", run=runner)
            first.append("--internal-ip")
            second = gke_endpoint.dns_endpoint_args("p", "c", "us-central1", run=runner)
        self.assertEqual(second, ["--dns-endpoint"])


class DescribeCommandTest(unittest.TestCase):
    def test_describe_is_scoped_to_the_named_cluster(self):
        runner = FakeRunner(DNS_EXTERNAL)
        decide(runner, project="proj", cluster="clus", location="europe-west1")
        argv = runner.describe_calls[0]
        self.assertEqual(argv[:5], ["gcloud", "container", "clusters", "describe", "clus"])
        self.assertIn("--location=europe-west1", argv)
        self.assertIn("--project=proj", argv)


if __name__ == "__main__":
    unittest.main()
