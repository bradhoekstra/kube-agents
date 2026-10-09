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
2. **Private endpoint**, `--internal-ip`, when all of these hold:
   - the target's `networkConfig.network` equals the agent's own cluster's network;
   - `privateClusterConfig.privateEndpoint` is set;
   - `controlPlaneEndpointsConfig.ipEndpointsConfig.enabled` is not `false`.
3. **gcloud's default**, no flag. Unchanged.

The second and third conditions are gcloud's own: `_GetClusterEndpoint` in
`googlecloudsdk/api_lib/container/util.py` raises `MissingPrivateEndpointError` without a private
endpoint and `IPEndpointsIsDisabledError` with IP endpoints off. Reading the configuration up
front keeps the module's existing contract: the flag is never passed blind and never probed by
attempting it.

Two shapes the rule deliberately does not cover. A cluster on a peered VPC is not detected,
because the network paths differ and nothing short of a routes query says whether the peering
exports the control-plane route. A cluster whose private endpoint is on the same VPC but whose
Master Authorized Networks exclude the agent's pod range still gets `--internal-ip`: the
private IP is the endpoint most likely to be reachable, and the diagnostic below names the
missing range.

## How the agent knows its own network

The operator sets `GKE_PROJECT_ID`, `GKE_LOCATION` and `GKE_CLUSTER_NAME` on the agent
container, the shell sandbox forwards them, and the credential proxy reads them at bootstrap.
`gke_endpoint.own_network()` describes that cluster with
`--format=value(networkConfig.network)` and keeps the answer for the life of the process. The
install's VPC cannot change under a running pod, so this memo has no TTL, unlike the per-target
decision, which keeps its 60-second one. A describe that fails is not cached, for the reason
the module already gives for the target describe: the credential proxy is a daemon.

With any of the three variables unset the answer is "unknown", and rule 2 never fires. That is
the position of every workstation and test caller, and it is what keeps the three predicate
copies in agreement without changing two of them.

A Shared VPC matches naturally: a service-project cluster reports the host project's network
resource (`projects/<host>/global/networks/<name>`) in `networkConfig.network`.

## What the decision carries

`endpoint_decision()` returns an `EndpointDecision`:

| Field                 | Content                                                            |
| --------------------- | ------------------------------------------------------------------ |
| `flags`               | `["--dns-endpoint"]`, `["--internal-ip"]` or `[]`                  |
| `kind`                | `dns`, `internal-ip` or `ip`                                       |
| `address`             | the hostname or IP the kubeconfig will name                        |
| `same_network`        | `True`, `False`, or `None` when the agent's own network is unknown |
| `authorized_networks` | the `masterAuthorizedNetworksConfig.cidrBlocks` CIDRs, or `None`   |
| `remedy`              | one sentence naming what would make the cluster reachable          |

`dns_endpoint_args()` stays as the wrapper every caller uses today and returns only `flags`.
The three runtime callers, `cluster_agent_profile.py`, `platform_mcp_server.py` and
`credential_proxy.py`, therefore inherit rule 2 without a signature change. The describe
widens from `json(controlPlaneEndpointsConfig)` to
`json(controlPlaneEndpointsConfig,privateClusterConfig,networkConfig.network,masterAuthorizedNetworksConfig,endpoint)`.

`remedy` is composed once, in Python, from the fields above:

- `internal-ip` with a non-empty authorized list: "Add the agent's Pod and node ranges to the
  cluster's authorized networks, or open the DNS endpoint with
  `gcloud container clusters update --enable-dns-access`."
- `ip` on a cluster the agent does not share a network with, or whose network is unknown:
  "The agent is not on this cluster's VPC; open the DNS endpoint with `--enable-dns-access`,
  or add the agent's egress address to the authorized networks."
- `dns`: no remedy; the endpoint is reachable by construction.

## The diagnostic

`cluster_agent_profile.create_profile()` already writes the cluster's identity into the
profile's `USER.md` as `- key: value` bullets, the one shape `cluster_preflight.sh` can read,
and mirrors that file into the sandbox. The decision joins it as four more bullets:

```
- endpoint: internal-ip
- endpoint-address: 10.128.0.6
- authorized-networks: 10.0.0.0/8, 172.16.0.0/12
- endpoint-remedy: Add the agent's Pod and node ranges to ...
```

`authorized-networks` reads `unrestricted` when the config is absent or disabled, and the
`endpoint-remedy` bullet is written only when there is a remedy to give. After the
mirror, the scaffold probes the cluster with `kubectl version --request-timeout=5s` in the
sandbox under the pinned kubeconfig. On failure it logs one line with the endpoint kind, the
address, the authorized list and the remedy, and returns normally: a cluster that is
unreachable now may be reachable after the operator acts, and a scaffold that failed would
only be retried on the next reconcile tick with the same result.

`cluster_preflight.sh` check 5, which fails today with the raw `kubectl cluster-info` error,
reads the four bullets with its existing `user_md_field()` and appends them to its `reason` and
`remediation` fields when they are present. A profile scaffolded before this change carries no
bullets and preflight prints what it prints today.

## What stays as it is, and why

Three other copies of the endpoint rule exist. None changes here.

- **`scripts/installer/gke_dns_endpoint.sh`** runs on a workstation or in CI, outside any GKE
  VPC, to reach the management cluster. Its header comment names this design as the reason it
  carries no `--internal-ip` branch. The bring-your-own-CI case belongs to the Terraform split
  tracked in the epic that owns this issue.
- **The awk program in `platformagent_manifests.go`** fetches the agent's own cluster's
  credentials at credential-proxy bootstrap. It has the exposure this design describes when the
  agent's own cluster restricts Master Authorized Networks, and every VPC-native cluster reports
  a private endpoint, so switching it changes every install at once. That is a separate change,
  decided on evidence the live validation below gathers.
- **`ClientConfigForIdentity` in `k8s-operator/internal/clusterprofiles/endpoint.go`** serves
  the drift detector from the operator pod. Rule 2 applies to it unchanged, with its own describe
  of the host cluster. Its comment names the gap until a follow-up closes it.

`test_gke_endpoint_parity.py` keeps holding the Python, shell and awk copies to one DNS truth
table. One case is added: with the agent's own network unknown, a same-shape private cluster
yields no flag from all three.

## Tests

- `test_gke_endpoint.py`: the decision matrix. Same network with a private endpoint and a
  restricted authorized list; same network with the public endpoint on; different network;
  same network without a private endpoint; same network with IP endpoints disabled; DNS open
  on a same-network cluster, which still picks DNS; env unset; own-cluster describe failing,
  which is not cached; the remedy text per kind.
- `test_cluster_agent_profile.py`: the four bullets land in `USER.md`; a failing probe logs the
  diagnostic and the scaffold still returns the profile name; a passing probe logs nothing.
- `test_cluster_preflight.py`: check 5's JSON carries the bullets when present and is byte-equal
  to today's output when they are absent.
- `test_gke_endpoint_parity.py`: the added case above.

## Live validation

A throwaway zonal cluster on the `default` VPC of the gkedemos install, private nodes, Master
Authorized Networks restricted to a range that excludes the agent, DNS endpoint closed. The
run observes, in order: the onboarded profile's kubeconfig names the private IP; the scaffold
log names `internal-ip`, the address, the list and the remedy; preflight check 5 carries the
same; after adding the agent cluster's Pod range to the authorized list, `kubectl` from the
profile answers. The cluster is deleted at the end. The same run records whether the gkedemos
pod reaches its own cluster's private endpoint, as the evidence the bootstrap follow-up needs.
