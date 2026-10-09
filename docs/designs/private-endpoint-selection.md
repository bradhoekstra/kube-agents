# Reaching a fleet cluster over its private endpoint

The Platform Agent onboards a second cluster with `gcloud container clusters get-credentials`.
`agents/platform/scripts/gke_endpoint.py` decides which flags that command gets. Until this
design it knew two answers: `--dns-endpoint` when the cluster publishes a DNS endpoint that
accepts external traffic, and nothing otherwise, which leaves gcloud writing the cluster's
public IP whenever one exists.

An enterprise estate breaks that second answer. Clusters there run private nodes with Master
Authorized Networks restricted to corporate ranges, and the DNS endpoint closed to external
traffic. The agent pod reaches such a cluster's public IP through Cloud NAT, and the NAT
address is not on the list, so every `kubectl` times out. The same cluster's private endpoint
is one hop away on the VPC the agent pod already sits in, and `get-credentials --internal-ip`
would have written it. Nothing recorded which endpoint was chosen, so the operator saw a
timeout and had to work out the rest.

## The decision rule

`gke_endpoint.endpoint_decision()` reads the target cluster once and picks, in order:

1. **DNS endpoint**, `--dns-endpoint`, when `dnsEndpointConfig.endpoint` is set and
   `allowExternalTraffic` is `true`. Unchanged. It is first because the DNS endpoint ignores
   Master Authorized Networks and routes from anywhere.
2. **DNS endpoint without a flag** when `ipEndpointsConfig.enabled` is `false`. gcloud writes
   the DNS host by itself in that shape and refuses `--internal-ip`
   (`IPEndpointsIsDisabledError`), and authorized networks do not gate that host.
3. **Private endpoint**, `--internal-ip`, when the cluster publishes
   `privateClusterConfig.privateEndpoint` (gcloud's `MissingPrivateEndpointError` otherwise;
   the nested `ipEndpointsConfig.privateEndpoint` is deliberately not read, because gcloud
   reads only the first) and the endpoint is both
   routable from the agent pod and willing to admit it:
   - **routable**: the target's `networkConfig.network` equals the agent's own cluster's
     network, and either the target is in the agent cluster's region or control-plane global
     access is on (`privateClusterConfig.masterGlobalAccessConfig.enabled` or
     `ipEndpointsConfig.globalAccess`). GKE answers a private
     endpoint only from its own region unless control-plane global access is on, and every
     VPC-native cluster reports a private endpoint, so without the region test an ordinary
     public cluster in another region would lose a working endpoint for one it cannot reach.
   - **admitted**: the authorized-network list is not enabled, or it is not enforced on the
     private endpoint (`privateEndpointEnforcementEnabled` absent or `false`; gcloud's
     `--enable-authorized-networks-on-private-endpoint` is what turns it on, and it defaults
     off), or the two clusters share a subnetwork, or the agent cluster's Pod range
     (`clusterIpv4Cidr`) lies inside a listed block. The shared-subnet clause rests on an
     observation, not a document: a cluster on the agent's subnet with enforcement on and a
     list that excluded every agent range still answered the agent pod on its private
     endpoint. The Pod range is what the traffic carries: GKE does not masquerade RFC 1918
     destinations. Without the admission test an estate that had listed the agent's NAT
     address would have lost a working public endpoint on upgrade.
4. **gcloud's default**, no flag. Unchanged. When rule 3 failed only on admission, the remedy
   names the Pod range to add.

Rule 3 reads the configuration up front, which keeps the module's existing contract: the flag
is never passed blind and never probed by attempting it.

A cluster on a peered VPC is not detected: the network paths differ and nothing short of a
routes query says whether the peering exports the control-plane route.

## How the agent knows its own cluster

The operator sets `GKE_PROJECT_ID`, `GKE_LOCATION` and `GKE_CLUSTER_NAME` on the agent
container, the shell sandbox forwards them, and the credential proxy carries them too.
`gke_endpoint.own_cluster()` describes that cluster once with
`--format=value(networkConfig.network,networkConfig.subnetwork,clusterIpv4Cidr)`, derives its
region from `GKE_LOCATION`, and keeps the answer for the life of the process. The install's VPC
cannot change under a running pod, so this memo has no TTL, unlike the per-target decision,
which keeps its 60-second one. A describe that fails, or answers fewer than three fields, is not
cached, for the reason the module already gives for the target describe: the credential proxy
is a daemon.

With any of the three variables unset the answer is "unknown", and rule 3 never fires. That is
the position of every workstation and test caller, and it is what keeps the predicate copies
in agreement without changing them.

A Shared VPC matches naturally: a service-project cluster reports the host project's network
resource (`projects/<host>/global/networks/<name>`) in `networkConfig.network`.

## What the decision carries

`endpoint_decision()` returns an `EndpointDecision`, or `None` when nothing could be decided
(an incomplete identity, a gcloud without `--dns-endpoint`, a describe that failed with nothing
cached):

| Field                 | Content                                                                           |
| --------------------- | --------------------------------------------------------------------------------- |
| `flags`               | `("--dns-endpoint",)`, `("--internal-ip",)` or `()`                               |
| `kind`                | `dns`, `internal-ip` or `ip`                                                      |
| `address`             | the hostname or IP the kubeconfig will name                                       |
| `same_network`        | `True`, `False`, or `None` when the agent's own network was not needed or unknown |
| `authorized_networks` | the listed CIDRs when the list is enabled (possibly empty), or `None`             |
| `remedy`              | one sentence naming what would make the cluster reachable, or `""`                |

`dns_endpoint_args()` stays as the wrapper returning only `flags` as a list. Its callers,
`platform_mcp_server.py`, `stall_watch.py` and `credential_proxy.py`, inherit rule 3 without
a signature change; `cluster_agent_profile.py` calls `endpoint_decision()` itself because it
writes the decision out. The describe widens to
`json(controlPlaneEndpointsConfig,privateClusterConfig,networkConfig.network,networkConfig.subnetwork,masterAuthorizedNetworksConfig,endpoint)`;
gcloud's `json()` projection drops any nested key not named, which is why `subnetwork` is
spelled out.

The remedies are the module's `REMEDY_*` constants, composed once so the scaffold log and the
preflight card read the same words:

- rule 3 failed on admission: `REMEDY_ADMIT_POD_RANGE`, with the Pod range spliced in;
- `ip` with the list enabled, on another network with a private endpoint: `REMEDY_OTHER_NETWORK`;
- `ip` with the list enabled otherwise: `REMEDY_IP`;
- `dns`, `internal-ip`, or any cluster whose list is not enabled: no remedy. An `internal-ip`
  decision never carries one, because it is only made where the list already admits the agent.

## The diagnostic

`cluster_agent_profile.create_profile()` already writes the cluster's identity into the
profile's `USER.md` as `- key: value` bullets and mirrors that file into the sandbox. The
decision joins it:

```
- endpoint: internal-ip
- endpoint-address: 10.128.0.6
- authorized-networks: 10.0.0.0/8, 172.16.0.0/12
- endpoint-remedy: Add 10.92.0.0/14 to this cluster's authorized networks ...
```

`authorized-networks` reads `unrestricted` when the list is not enabled and
`enabled, no ranges listed` when it is enabled and empty; the `endpoint-remedy` bullet is
written only when there is a remedy, and no endpoint bullet is written when nothing could be
decided. After the mirror, the scaffold probes the cluster with
`kubectl version --request-timeout=5s` in the sandbox under the pinned kubeconfig, as the
`agent` login that owns the file. When kubectl exits non-zero it logs one line with the endpoint
kind, the address, the list, and kubectl's last line, adding the remedy only when kubectl's
output is a connection failure (a timeout, no route, a refused dial) rather than an answer the
server gave (401, 403, NotFound). When the probe itself does not finish inside its outer bound,
or cannot run, the log says that and nothing about the endpoint. The scaffold still returns
normally: a cluster that is unreachable now may be reachable after the operator acts, and a
scaffold that failed would only be retried on the next reconcile tick with the same result.

`cluster_preflight.sh` check 5 reads the bullets back, `endpoint` through the existing
`user_md_field()` and the other three through `user_md_text()`, which keeps a value's case and
spacing. It appends the endpoint and list to its `reason`, and prefixes its `remediation` with
the remedy when kubectl's output matches the same connection-failure pattern, which a test
holds equal to the Python one. A profile scaffolded before this change
carries no bullets and preflight prints what it prints today.

The credential proxy decides again whenever it rebuilds its managed kubeconfig for a cluster,
and in a sandboxed install that kubeconfig is the one every brokered `kubectl` uses. The two
decisions run the same rule over the same describe, so `USER.md` and the live choice diverge
only after the cluster's endpoint configuration changes, which the remedy already answers with
a re-run of the onboarding.

## What stays as it is, and why

Four other copies of the endpoint rule exist. None changes here.

- **`scripts/installer/gke_dns_endpoint.sh`** runs on a workstation or in CI, outside any GKE
  VPC, to reach the management cluster. Its header comment names this design as the reason it
  carries no `--internal-ip` branch. The bring-your-own-CI case belongs to the Terraform split
  tracked in the epic that owns this issue (#2591).
- **The awk program in `platformagent_manifests.go`** fetches the agent's own cluster's
  credentials at credential-proxy bootstrap. It has the exposure this design describes when the
  agent's own cluster restricts Master Authorized Networks, and every VPC-native cluster reports
  a private endpoint, so switching it changes every install at once. That is a separate change,
  decided on evidence the live validation below gathered.
- **`ClientConfigForIdentity` in `k8s-operator/internal/clusterprofiles/endpoint.go`** serves
  the event watcher and the drift detector, which run in the agent image. Rule 3 would apply
  to it unchanged, with its own describe of the host cluster; it does not yet, and its comment
  names the gap until a follow-up closes it.
- **`dns_endpoint_args()` in the fleet-upgrade-verification skill** decides from the record
  `clusters list` returned, without a describe. In a sandboxed install the broker's own
  decision governs the connection whatever flags the skill passes, so it keeps today's rule.

`test_gke_endpoint_parity.py` keeps holding the Python, shell and awk copies to one DNS truth
table. One case is added: with the agent's own identity absent, a same-shape private cluster
yields no flag from all three.

## Tests

- `test_gke_endpoint.py`: the decision matrix. The enterprise shape (list enforced on the
  private endpoint, a different subnet, the agent's Pod range inside a listed block); the
  NAT-only list, which keeps the public IP and names the Pod range; the shared subnet; the
  list not enforced; another region with and without global access; a zonal location in the
  same region; another network; no private endpoint; IP endpoints disabled; DNS open on a
  same-network cluster; the identity unset or partial; the own-cluster describe failing, empty,
  or short, none of which is cached.
- `test_cluster_agent_profile.py`: the bullets land in `USER.md`; the probe runs as the `agent`
  login; a connection failure logs the diagnostic with the remedy and a 403 logs it without;
  the scaffold still returns the profile name; a passing probe logs nothing.
- `test_cluster_preflight.py`: check 5's JSON carries the bullets and the remedy on a connection
  failure, the bullets without the remedy on a 403, and today's text when there are no bullets.
- `test_gke_endpoint_parity.py`: the added case above.

## Live validation

A throwaway zonal cluster on the same VPC as a running install, private nodes, Master
Authorized Networks restricted to a documentation range, DNS endpoint closed. Observed: the
previous image wrote the public IP into the onboarded profile's kubeconfig; this change wrote
the private IP and the four bullets; from the agent pod and the sandbox the private endpoint
answered in under a second and the public IP timed out; the branch's preflight named the
endpoint, the list and the remedy once the cluster was gone. The two clusters shared a subnet
and the list was enforced on the private endpoint, and the private endpoint still admitted the
agent: that observation is what the shared-subnet clause of rule 3 rests on. The agent pod
also reached its own cluster's private endpoint, the evidence the bootstrap follow-up needs.
