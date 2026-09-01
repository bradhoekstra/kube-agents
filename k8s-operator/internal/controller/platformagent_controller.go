/*
Copyright 2026.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

package controller

import (
	"context"
	"fmt"
	"net"
	"slices"
	"strconv"
	"strings"
	"sync"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	nodev1 "k8s.io/api/node/v1"
	policyv1 "k8s.io/api/policy/v1"
	rbacv1 "k8s.io/api/rbac/v1"
	"k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/discovery"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/builder"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
	"sigs.k8s.io/controller-runtime/pkg/handler"
	logf "sigs.k8s.io/controller-runtime/pkg/log"
	"sigs.k8s.io/controller-runtime/pkg/predicate"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

const (
	platformAgentFinalizer = "kubeagents.x-k8s.io/finalizer"
	minIPv4CIDRPrefix      = 12
	minIPv6CIDRPrefix      = 48
	maxCIDRsPerAnnotation  = 50

	// metadataLinkLocalIP is the address a workload dials for GCP metadata and Workload
	// Identity tokens. It is only ever the pre-DNAT destination.
	metadataLinkLocalIP = "169.254.169.254"
	// metadataDaemonIP is where GKE's node-local metadata daemon actually listens, on
	// TCP 988. On the iptables datapath (Dataplane V1) the node DNATs
	// 169.254.169.254:80 to 169.254.169.252:988 in nat PREROUTING — before NetworkPolicy
	// is evaluated — so a policy that permits only the link-local address drops every
	// token fetch. Dataplane V2 (eBPF) evaluates policy pre-NAT at the socket layer,
	// so the 169.254.169.254/32 on port 80 rule satisfies it directly.
	//
	// Ref:
	// - https://cloud.google.com/kubernetes-engine/docs/how-to/network-policy
	// - https://docs.cilium.io/en/stable/security/policy/layer3/
	// - https://github.com/cilium/cilium/issues/12277 (CIDR rules don't match node IPs without --policy-cidr-match-mode=nodes)
	metadataDaemonIP = "169.254.169.252"

	AnnotationAPIServerCIDR           = "kubeagents.x-k8s.io/apiserver-cidr"
	AnnotationCustomEgressCIDRs       = "kubeagents.x-k8s.io/custom-egress-cidrs"
	AnnotationEnableFQDNNetworkPolicy = "kubeagents.x-k8s.io/enable-fqdn-network-policy"

	// The condition reporting that cluster event ingestion has been switched off
	// on the spec. It is written only in that state — see updateStatusReady.
	eventWatcherConditionType  = "EventWatcher"
	eventWatcherDisabledReason = "DisabledBySpec"
	// Long, because the reader of `kubectl describe` is the person who has to
	// decide whether this is still wanted. It has to say what stopped, that
	// nothing will turn it back on, and how to turn it back on.
	eventWatcherDisabledMessage = "Cluster event ingestion is disabled by spec.harness.eventWatcher.enabled=false. " +
		"The k8s-event-watcher is not started, so no cluster warning reaches the agent and no autonomous triage " +
		"session is created from one; the pod stays Ready regardless. Nothing restores this automatically — set " +
		"spec.harness.eventWatcher.enabled=true (or remove the field) to start watching again."
)

// PlatformAgentReconciler reconciles a PlatformAgent object
type PlatformAgentReconciler struct {
	client.Client
	Scheme          *runtime.Scheme
	DiscoveryClient discovery.DiscoveryInterface

	// APIReader reads straight from the API server, bypassing the manager's cache.
	// Collector discovery looks at Services in namespaces this operator otherwise never
	// touches, and a cached read there would have the manager start — and keep — an
	// informer watching every Service in the cluster, to serve a handful of reads an
	// hour. Nil falls back to the cached client, which is what tests supply.
	APIReader client.Reader

	// clusterImageVolumes caches the cluster-wide ImageVolume capability. Server
	// version cannot change without an API server restart, so resolving it once
	// avoids a discovery round-trip on every reconcile of every agent. Only an
	// authoritative probe sets imageVolumeResolved; a failed probe is retried.
	imageVolumeMu       sync.Mutex
	imageVolumeResolved bool
	clusterImageVolumes bool

	// APIServerIP configures the Kubernetes API server control-plane egress CIDR
	// for generated NetworkPolicy manifests.
	APIServerIP string

	// APIServerCIDROverride configures static CIDR overrides for the Kubernetes API server
	// (e.g. from KUBERNETES_API_SERVER_CIDR).
	APIServerCIDROverride string

	// DNSClusterIPOverride configures static override for the Cluster DNS Service ClusterIP
	// (e.g. from KUBERNETES_DNS_CLUSTER_IP or --kubernetes-dns-cluster-ip).
	DNSClusterIPOverride string

	// MetadataDaemonIPOverride configures static override for the Workload Identity metadata daemon IP
	// (e.g. from KUBERNETES_METADATA_DAEMON_IP or --kubernetes-metadata-daemon-ip).
	MetadataDaemonIPOverride string

	// otelEndpoint caches the discovered OpenTelemetry collector, cluster-wide — there
	// is one collector per cluster, not one per agent. Unlike the ImageVolume
	// capability this expires (otelDiscoveryTTL): a Service can appear or move at any
	// time. See discoveredOTLPEndpoint for the "" / not-determined distinction.
	// otelProbedAt is when a probe was last attempted, successful or not. It exists
	// only to rate-limit retries: an inconclusive probe caches nothing, so without a
	// floor an API outage has every reconcile of every agent re-run the whole sweep.
	otelMu         sync.Mutex
	otelResolved   bool
	otelEndpoint   string
	otelResolvedAt time.Time
	otelProbedAt   time.Time
}

// +kubebuilder:rbac:groups=kubeagents.x-k8s.io,resources=platformagents,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=kubeagents.x-k8s.io,resources=platformagents/status,verbs=get;list;watch;update;patch
// +kubebuilder:rbac:groups=kubeagents.x-k8s.io,resources=platformagents/finalizers,verbs=update
// +kubebuilder:rbac:groups=kubeagents.x-k8s.io,resources=agentplugins,verbs=get;list;watch
// +kubebuilder:rbac:groups=kubeagents.x-k8s.io,resources=agentplugins/status,verbs=get;update;patch
// +kubebuilder:rbac:groups=apps,resources=deployments;statefulsets,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=apps,resources=daemonsets;replicasets,verbs=get;list;watch
// apps/daemonsets is also read by resolveNetpolProfile to discover the gke-metadata-server
// DaemonSet port (issue #747 B4) — a second consumer of a grant that already existed for
// buildMinimalPlatformRole's escalation-prevention requirement.
// +kubebuilder:rbac:groups="",resources=serviceaccounts;persistentvolumeclaims;configmaps;services;pods,verbs=get;list;watch;create;update;patch;delete
// `nodes` is still required: buildMinimalPlatformRole grants it to the agent audit
// ClusterRole, and RBAC escalation-prevention needs the operator to hold it to apply that.
// +kubebuilder:rbac:groups="",resources=namespaces;nodes;events;persistentvolumes;resourcequotas;limitranges;endpoints;pods/log,verbs=get;list;watch
// +kubebuilder:rbac:groups=metrics.k8s.io,resources=nodes;pods,verbs=get;list;watch
// +kubebuilder:rbac:groups=autoscaling,resources=horizontalpodautoscalers,verbs=get;list;watch
// +kubebuilder:rbac:groups=batch,resources=cronjobs;jobs,verbs=get;list;watch
// +kubebuilder:rbac:groups=coordination.k8s.io,resources=leases,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=node.k8s.io,resources=runtimeclasses,verbs=get;list;watch
// +kubebuilder:rbac:groups=networking.k8s.io,resources=networkpolicies,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=networking.k8s.io,resources=ingresses,verbs=get;list;watch
// +kubebuilder:rbac:groups=networking.gke.io,resources=fqdnnetworkpolicies,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=policy,resources=poddisruptionbudgets,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=rbac.authorization.k8s.io,resources=clusterroles;clusterrolebindings;roles;rolebindings,verbs=get;list;watch;create;update;patch;delete
// The split credential broker verifies its callers with a TokenReview. The operator has to
// hold that permission in order to grant it; it confers no read access and cannot mint a token.
// +kubebuilder:rbac:groups=authentication.k8s.io,resources=tokenreviews,verbs=create
// +kubebuilder:rbac:groups=apiextensions.k8s.io,resources=customresourcedefinitions,verbs=get;list;watch

func (r *PlatformAgentReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	log := logf.FromContext(ctx)

	instance := &agentv1alpha1.PlatformAgent{}
	if err := r.Get(ctx, req.NamespacedName, instance); err != nil {
		if errors.IsNotFound(err) {
			// The AgentPlugin watch enqueues spec.agentRef, so this also fires for
			// plugins pointing at an agent that does not exist. Tell them so, rather
			// than leaving a mistyped agentRef silently statusless forever.
			r.markOrphanedPlugins(ctx, req.Namespace, req.Name)
		}
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}

	log.Info("Reconciling PlatformAgent", "name", instance.Name, "namespace", instance.Namespace)

	// projectId became required, but CRs stored before that change still
	// reconcile. Without the full triple the credential proxy bootstrap is
	// skipped and kubectl silently resolves to localhost:8080, so say so
	// loudly rather than letting the agent discover it at runtime.
	if h := instance.Spec.Harness; h == nil || h.ProjectID == "" || h.Location == "" || h.ClusterName == "" {
		log.Info("WARNING: spec.harness needs projectId, location, and clusterName; "+
			"without all three the credential proxy skips its kubeconfig bootstrap and kubectl will not reach any cluster",
			"name", instance.Name, "namespace", instance.Namespace)
	}

	// 1. Intercept Deletion
	if !instance.ObjectMeta.DeletionTimestamp.IsZero() {
		return r.handleDeletion(ctx, instance)
	}

	// 2. Add Finalizer if not present
	if !controllerutil.ContainsFinalizer(instance, platformAgentFinalizer) {
		controllerutil.AddFinalizer(instance, platformAgentFinalizer)
		if err := r.Update(ctx, instance); err != nil {
			return ctrl.Result{}, err
		}
		// Return immediately after update to fetch the fresh ResourceVersion, preventing OptimisticLockErrors
		return ctrl.Result{}, nil
	}

	// 3. Reconcile Service Account (with Workload Identity annotation)
	if err := r.reconcileServiceAccount(ctx, instance); err != nil {
		return ctrl.Result{}, err
	}
	// 3b. Reconcile RBAC (ClusterRole and ClusterRoleBindings)
	if err := r.reconcileRBAC(ctx, instance); err != nil {
		return ctrl.Result{}, err
	}

	// 4. Reconcile PVC for agent persistent data
	if err := r.reconcilePVC(ctx, instance); err != nil {
		return ctrl.Result{}, err
	}

	// 5. Resolve agent plugins
	agentPlugins, err := r.resolveAgentPlugins(ctx, instance)
	if err != nil {
		return ctrl.Result{}, err
	}

	// 6. Reconcile ConfigMap (config.yaml content)
	configMapHash, err := r.reconcileConfigMap(ctx, instance, agentPlugins)
	if err != nil {
		return ctrl.Result{}, err
	}

	// 7. Reconcile Fluent Bit ConfigMap
	fluentBitHash, err := r.reconcileFluentBitConfigMap(ctx, instance)
	if err != nil {
		return ctrl.Result{}, err
	}

	// 8. Reconcile Settings ConfigMap
	settingsHash, err := r.reconcileSettingsConfigMap(ctx, instance)
	if err != nil {
		return ctrl.Result{}, err
	}

	// 9. Reconcile Credential Proxy Policy ConfigMap
	proxyPolicyHash, err := r.reconcileCredentialProxyPolicyConfigMap(ctx, instance)
	if err != nil {
		return ctrl.Result{}, err
	}

	// 9b. Refuse a CR that mounts the broker's own volumes into the agent container.
	if msg := validateExtraVolumeMounts(instance); msg != "" {
		log.Info(msg)
		if statusErr := r.updateStatusDegraded(ctx, instance, "ForbiddenVolumeMount", msg); statusErr != nil {
			return ctrl.Result{}, statusErr
		}
		return ctrl.Result{}, nil
	}

	// 10. Validate RuntimeClass if specified
	if rcName, err := r.validateRuntimeClass(ctx, instance); err != nil {
		if errors.IsNotFound(err) {
			// The name comes back from the check rather than being read off
			// spec.deployment here: the sandbox has a RuntimeClass field of its
			// own, and dereferencing the agent's would panic on a CR that names
			// only the sandbox one.
			msg := fmt.Sprintf("RuntimeClass '%s' is not configured in this cluster. For GKE Standard, enable GKE Sandbox by provisioning a gVisor node pool first. In GKE Autopilot, gVisor is supported automatically.", rcName)
			log.Info(msg)
			if statusErr := r.updateStatusDegraded(ctx, instance, "RuntimeClassNotFound", msg); statusErr != nil {
				return ctrl.Result{}, statusErr
			}
			return ctrl.Result{RequeueAfter: 30 * time.Second}, nil
		}
		return ctrl.Result{}, fmt.Errorf("failed to validate RuntimeClass: %w", err)
	}

	// 10b. Reconcile the shell sandbox before the credential proxy, and both before
	// the workload that connects to them. Neither client blocks on the proxy — the
	// wrapped CLIs report it unavailable and the chat relay retries its poll — but
	// this order narrows the one case where that shows: when the proxy runs in the
	// sandbox pod, reconcileCredentialProxy deletes the standalone Deployment, and
	// doing that before the StatefulSet exists leaves a window with no credential
	// runtime at all.
	if err := r.reconcileShellSandbox(ctx, instance, settingsHash, proxyPolicyHash); err != nil {
		return ctrl.Result{}, err
	}

	// 10b. Grant the broker the one verb it needs to authenticate its callers,
	// before anything that runs it.
	if err := r.reconcileCredentialBrokerTokenReviewRBAC(ctx, instance); err != nil {
		return ctrl.Result{}, err
	}

	// 11. Reconcile the credential proxy: its Service always, its own pod only when
	// the sandbox is not hosting it.
	if err := r.reconcileCredentialProxy(ctx, instance, proxyPolicyHash); err != nil {
		return ctrl.Result{}, err
	}

	// 11b. Refuse a broker split that would strand the event watcher.
	//
	// Before the workload, deliberately: an operator who asked for the broker to
	// leave the agent Pod must not get a running agent whose cluster events have
	// silently stopped. The one refusable configuration is named in
	// validateCredentialBrokerSplit.
	if reason, msg := validateCredentialBrokerSplit(instance); reason != "" {
		log.Info(msg)
		if statusErr := r.updateStatusDegraded(ctx, instance, reason, msg); statusErr != nil {
			return ctrl.Result{}, statusErr
		}
		return ctrl.Result{RequeueAfter: 30 * time.Second}, nil
	}

	// 11c. Reconcile the credential broker's own Pod, if it has one.
	//
	// Before the agent's workload, not after. On the reconcile that first turns
	// the split on, the agent Deployment is re-rendered pointing at the broker
	// Service; creating that Service afterwards leaves the restarted agent
	// failing every proxied command with a connection refused until the next
	// pass. The other direction is safe either way, because turning the split
	// off deletes a broker the re-rendered agent has already stopped using.
	if err := r.reconcileCredentialBroker(ctx, instance, proxyPolicyHash); err != nil {
		return ctrl.Result{}, err
	}

	// 12. Reconcile the Agent Sandbox Pod with its Envoy credential sidecar.
	otlpEndpoint, otlpSource := r.resolveOTLPEndpoint(ctx, instance)
	otlpDisabled := otlpSource == otlpSourceNone
	netpolProf := r.resolveNetpolProfile(ctx, instance)
	if err := r.reconcileWorkload(ctx, instance, configMapHash, fluentBitHash, settingsHash, proxyPolicyHash, agentPlugins, otlpEndpoint, otlpDisabled); err != nil {
		return ctrl.Result{}, err
	}

	// Reconcile Service
	if err := r.reconcileService(ctx, instance); err != nil {
		return ctrl.Result{}, err
	}
	// Reconcile PodDisruptionBudget
	if err := r.reconcilePodDisruptionBudget(ctx, instance); err != nil {
		return ctrl.Result{}, err
	}
	// Reconcile NetworkPolicy
	if err := r.reconcileNetworkPolicy(ctx, instance, netpolProf, otlpEndpoint, otlpDisabled); err != nil {
		return ctrl.Result{}, err
	}
	if err := r.deleteLegacyCredentialIsolationResources(ctx, instance); err != nil {
		return ctrl.Result{}, err
	}

	// 9. Update status phase to Ready
	phase, err := r.updateStatusReady(ctx, instance, otlpEndpoint, otlpSource, netpolProf)
	if err != nil {
		return ctrl.Result{}, err
	}

	// A plugin image that cannot be pulled only surfaces on the pod seconds after the
	// workload is written, and Pods are not watched here. Requeue while the picture is
	// still incomplete so both the failure and the later recovery reach plugin status.
	if pluginStatusNeedsRecheck(agentPlugins, phase == "Ready") {
		return ctrl.Result{RequeueAfter: 30 * time.Second}, nil
	}

	// Default and None are the telemetry outcomes that can improve without anything else
	// changing — someone installs a collector and nothing about this agent is touched.
	// Reconciles are event-driven and can be quiet for hours, so nudge the probe rather
	// than wait for an unrelated event. Every other source is explicit or already found
	// something, and needs no polling. None especially: it is the outcome that leaves the
	// agent exporting nowhere, so it is the one an operator most wants picked up promptly
	// once they install a collector.
	if otlpSource == otlpSourceDefault || otlpSource == otlpSourceNone {
		return ctrl.Result{RequeueAfter: otelRediscoverAfter}, nil
	}
	return ctrl.Result{}, nil
}

// pluginStatusNeedsRecheck reports whether plugin status is still provisional.
//
// While the agent has not reached Ready its pod may yet fail to pull a plugin image, so
// a plugin currently marked Ready cannot be trusted as final. Once a plugin is in
// ImagePullFailed we keep looking so that fixing the image clears the condition. Both
// conditions settle, so this terminates rather than requeueing forever.
func pluginStatusNeedsRecheck(plugins []*agentv1alpha1.AgentPlugin, agentReady bool) bool {
	if len(plugins) == 0 {
		return false
	}
	if !agentReady {
		return true
	}
	for _, plugin := range plugins {
		cond := meta.FindStatusCondition(plugin.Status.Conditions, "Ready")
		if cond == nil || cond.Reason == "ImagePullFailed" {
			return true
		}
	}
	return false
}

func (r *PlatformAgentReconciler) handleDeletion(ctx context.Context, agent *agentv1alpha1.PlatformAgent) (ctrl.Result, error) {
	if controllerutil.ContainsFinalizer(agent, platformAgentFinalizer) {
		// Delete the credential broker's TokenReview grant, if the split ever
		// created one. cleanupAgentRBAC's label-driven pass also reaps it under
		// deleteAll, but only when the grant carries the instance labels — one
		// applied before applyManaged stamped them would be orphaned
		// cluster-scoped RBAC. Named explicitly for that reason.
		tokenReviewName := fmt.Sprintf("kubeagents:tokenreview:%s:%s", agent.Namespace, agent.Name)
		crbTokenReview := &rbacv1.ClusterRoleBinding{ObjectMeta: metav1.ObjectMeta{Name: tokenReviewName}}
		if err := client.IgnoreNotFound(r.Delete(ctx, crbTokenReview)); err != nil {
			return ctrl.Result{}, err
		}
		crTokenReview := &rbacv1.ClusterRole{ObjectMeta: metav1.ObjectMeta{Name: tokenReviewName}}
		if err := client.IgnoreNotFound(r.Delete(ctx, crTokenReview)); err != nil {
			return ctrl.Result{}, err
		}
		if err := r.cleanupAgentRBAC(ctx, agent, true); err != nil {
			return ctrl.Result{}, err
		}

		// Resource is deleted. Safe to remove finalizer and update.
		controllerutil.RemoveFinalizer(agent, platformAgentFinalizer)
		if err := r.Update(ctx, agent); err != nil {
			return ctrl.Result{}, err
		}
	}
	return ctrl.Result{}, nil
}

// applyManaged stamps the recommended labels onto obj and applies it.
//
// Every object this controller writes goes through here, so a newly added
// resource cannot reach the cluster unlabelled. Owner references are still set
// by the caller: the cluster-scoped RBAC objects deliberately have none,
// because a namespaced owner cannot own a cluster-scoped resource.
func (r *PlatformAgentReconciler) applyManaged(ctx context.Context, agent *agentv1alpha1.PlatformAgent, obj client.Object) error {
	withCommonLabels(obj, agent)
	return r.Patch(ctx, obj, client.Apply, client.ForceOwnership, client.FieldOwner(fieldOwner))
}

func (r *PlatformAgentReconciler) reconcileServiceAccount(ctx context.Context, agent *agentv1alpha1.PlatformAgent) error {
	if agent.Spec.Security != nil && agent.Spec.Security.ServiceAccountName != "" && len(agent.Spec.Security.ServiceAccountAnnotations) == 0 {
		return nil
	}

	saName := agent.Name
	var annotations map[string]string
	if agent.Spec.Security != nil {
		if agent.Spec.Security.ServiceAccountName != "" {
			saName = agent.Spec.Security.ServiceAccountName
		}
		annotations = agent.Spec.Security.ServiceAccountAnnotations
	}

	return ReconcileServiceAccount(ctx, r.Client, r.Scheme, agent, saName, agent.Namespace, annotations, commonLabels(agent), fieldOwner)
}

func (r *PlatformAgentReconciler) reconcilePVC(ctx context.Context, agent *agentv1alpha1.PlatformAgent) error {
	pvcs := []*corev1.PersistentVolumeClaim{
		buildPVC(agent),
		buildSystemPVC(agent),
	}
	customPVCs, err := buildCustomPVCs(agent)
	if err != nil {
		return fmt.Errorf("failed to build custom PVCs: %w", err)
	}
	pvcs = append(pvcs, customPVCs...)
	for _, pvc := range pvcs {
		if err := r.reconcilePersistentVolumeClaim(ctx, agent, pvc); err != nil {
			return err
		}
	}
	return nil
}

func (r *PlatformAgentReconciler) reconcilePersistentVolumeClaim(ctx context.Context, agent *agentv1alpha1.PlatformAgent, pvc *corev1.PersistentVolumeClaim) error {
	if err := ctrl.SetControllerReference(agent, pvc, r.Scheme); err != nil {
		return err
	}
	// PVCs are created once and never updated, so this labels new claims only;
	// claims from before this change stay unlabelled until they are recreated.
	withCommonLabels(pvc, agent)

	found := &corev1.PersistentVolumeClaim{}
	err := r.Get(ctx, client.ObjectKey{Name: pvc.Name, Namespace: pvc.Namespace}, found)
	if err != nil {
		if errors.IsNotFound(err) {
			return r.Create(ctx, pvc)
		}
		return err
	}
	return nil
}

func (r *PlatformAgentReconciler) reconcileConfigMap(ctx context.Context, agent *agentv1alpha1.PlatformAgent, agentPlugins []*agentv1alpha1.AgentPlugin) (string, error) {
	cm := buildConfigMap(agent, agentPlugins)
	if err := ctrl.SetControllerReference(agent, cm, r.Scheme); err != nil {
		return "", err
	}

	err := r.applyManaged(ctx, agent, cm)
	if err != nil {
		return "", err
	}

	hash, err := getConfigMapHash(cm)
	if err != nil {
		return "", err
	}
	return hash, nil
}

func (r *PlatformAgentReconciler) reconcileFluentBitConfigMap(ctx context.Context, agent *agentv1alpha1.PlatformAgent) (string, error) {
	cm := buildFluentBitConfigMap(agent)
	if err := ctrl.SetControllerReference(agent, cm, r.Scheme); err != nil {
		return "", err
	}

	err := r.applyManaged(ctx, agent, cm)
	if err != nil {
		return "", err
	}

	hash, err := getConfigMapHash(cm)
	if err != nil {
		return "", err
	}
	return hash, nil
}

func (r *PlatformAgentReconciler) reconcileSettingsConfigMap(ctx context.Context, agent *agentv1alpha1.PlatformAgent) (string, error) {
	cm := buildSettingsConfigMap(agent)
	if err := ctrl.SetControllerReference(agent, cm, r.Scheme); err != nil {
		return "", err
	}

	err := r.applyManaged(ctx, agent, cm)
	if err != nil {
		return "", err
	}

	hash, err := getConfigMapHash(cm)
	if err != nil {
		return "", err
	}
	return hash, nil
}

func (r *PlatformAgentReconciler) reconcileCredentialProxyPolicyConfigMap(ctx context.Context, agent *agentv1alpha1.PlatformAgent) (string, error) {
	cm := buildCredentialProxyPolicyConfigMap(agent)
	if err := ctrl.SetControllerReference(agent, cm, r.Scheme); err != nil {
		return "", err
	}
	if err := r.applyManaged(ctx, agent, cm); err != nil {
		return "", err
	}
	return getConfigMapHash(cm)
}

func (r *PlatformAgentReconciler) reconcileWorkload(ctx context.Context, agent *agentv1alpha1.PlatformAgent, configHash, fluentBitHash, settingsHash, policyHash string, agentPlugins []*agentv1alpha1.AgentPlugin, otlpEndpoint string, otlpDisabled bool) error {
	imageVolumeSupported := r.imageVolumeSupported(agent)
	r.updatePluginStatuses(ctx, agent, agentPlugins, imageVolumeSupported)

	opts := renderOptions{imageVolumeSupported: imageVolumeSupported, otlpEndpoint: otlpEndpoint, otlpDisabled: otlpDisabled}

	// Note: Switching between Deployment and StatefulSet causes a full delete+recreate of the workload.
	// This will incur downtime and potentially stuck pods if RWO volumes take time to unbind.
	// This is an acceptable tradeoff since switching replicas/storage requires an explicit CRD update.
	if useStatefulSet(agent) {
		dep := &appsv1.Deployment{ObjectMeta: metav1.ObjectMeta{Name: agent.Name + "-gateway", Namespace: agent.Namespace}}
		if err := client.IgnoreNotFound(r.Delete(ctx, dep)); err != nil {
			return fmt.Errorf("failed to cleanup legacy Deployment: %w", err)
		}

		sts := buildStatefulSet(agent, configHash, fluentBitHash, settingsHash, policyHash, agentPlugins, opts)
		if err := ctrl.SetControllerReference(agent, sts, r.Scheme); err != nil {
			return err
		}
		return r.applyManaged(ctx, agent, sts)
	}

	sts := &appsv1.StatefulSet{ObjectMeta: metav1.ObjectMeta{Name: agent.Name + "-gateway", Namespace: agent.Namespace}}
	if err := client.IgnoreNotFound(r.Delete(ctx, sts)); err != nil {
		return fmt.Errorf("failed to cleanup legacy StatefulSet: %w", err)
	}

	dep := buildDeployment(agent, configHash, fluentBitHash, settingsHash, policyHash, agentPlugins, opts)
	if err := ctrl.SetControllerReference(agent, dep, r.Scheme); err != nil {
		return err
	}
	return r.applyManaged(ctx, agent, dep)
}

// deleteLegacyCredentialIsolationResources removes the workload objects left
// behind by the two-pod layout that shipped in fb99cd1 and was collapsed back
// into a sidecar in 9b2b7e8. Nothing recreates these names, so leaving them
// running would leave a second, unreconciled copy of the agent alive.
//
// The <name>-credential-proxy Deployment and Service used to be on this list.
// They are not legacy any more — they carry the same names again, and two
// reconcilers own them in both directions: reconcileCredentialProxy when the
// shell sandbox is on, reconcileCredentialBroker when the split is. Leaving
// them here deleted the object the reconcile had just applied, every pass.
// credentialProxySelector reproduces the pre-#368 labels so those objects are
// adopted rather than orphaned.
func (r *PlatformAgentReconciler) deleteLegacyCredentialIsolationResources(ctx context.Context, agent *agentv1alpha1.PlatformAgent) error {
	resources := []client.Object{
		&appsv1.Deployment{ObjectMeta: metav1.ObjectMeta{Name: agent.Name + "-sandbox", Namespace: agent.Namespace}},
		&corev1.ServiceAccount{ObjectMeta: metav1.ObjectMeta{Name: agent.Name + "-sandbox", Namespace: agent.Namespace}},
		&networkingv1.NetworkPolicy{ObjectMeta: metav1.ObjectMeta{Name: agent.Name + "-sandbox-metadata-deny", Namespace: agent.Namespace}},
	}
	for _, resource := range resources {
		if err := r.Get(ctx, client.ObjectKeyFromObject(resource), resource); err != nil {
			if client.IgnoreNotFound(err) != nil {
				return err
			}
			continue
		}
		if !metav1.IsControlledBy(resource, agent) {
			return fmt.Errorf("refusing to delete unowned legacy %T %s/%s", resource, resource.GetNamespace(), resource.GetName())
		}
		if err := client.IgnoreNotFound(r.Delete(ctx, resource)); err != nil {
			return err
		}
	}
	return nil
}

// reconcileShellSandbox creates or removes the agent's shell sandbox — the pod its
// terminal, file and code-execution tools run in when the ssh backend is on. The
// manifests and the reasoning behind them are in shell_sandbox_manifests.go.
//
// The off path deletes rather than leaves the workload behind: a sandbox nothing
// connects to is a pod holding a node's worth of quota for no reason, and the
// namespace quota on the reference install is tight enough that it matters. The
// PersistentVolumeClaim survives, both because a StatefulSet never reclaims its
// own claims and because its retention policy says Retain — so a toggle off and
// back on returns to the same host keys.
//
// Where the credential proxy runs decides what the sandbox is handed here.
// Co-located it is a second container of this StatefulSet and the wrappers post
// to loopback, which is what makes git work; otherwise the proxy has a pod of its
// own and the sandbox gets its Service URL. credentialProxySandboxURL picks, and
// credential_proxy_manifests.go carries the reasoning for both.
func (r *PlatformAgentReconciler) reconcileShellSandbox(ctx context.Context, agent *agentv1alpha1.PlatformAgent, settingsHash, policyHash string) error {
	if !shellSandboxEnabled(agent) {
		return r.deleteShellSandbox(ctx, agent)
	}

	// Before the StatefulSet, because an install that predates agentDataStorageSize
	// has a claim the template can no longer resize.
	r.growShellSandboxDataClaim(ctx, agent)

	sts := buildShellSandboxStatefulSet(agent, shellSandboxAuthorizedKeysSecretName(agent), credentialProxySandboxURL(agent), settingsHash, policyHash)
	objs := []client.Object{
		buildShellSandboxServiceAccount(agent),
		buildShellSandboxService(agent),
		sts,
		buildShellSandboxNetworkPolicy(agent),
	}
	for _, obj := range objs {
		if err := ctrl.SetControllerReference(agent, obj, r.Scheme); err != nil {
			return fmt.Errorf("failed to set controller reference on shell sandbox %T %s/%s: %w", obj, obj.GetNamespace(), obj.GetName(), err)
		}
		apply := r.applyManaged
		if obj == sts {
			apply = r.applyShellSandboxStatefulSet
		}
		if err := apply(ctx, agent, obj); err != nil {
			return fmt.Errorf("failed to apply shell sandbox %T %s/%s: %w", obj, obj.GetNamespace(), obj.GetName(), err)
		}
	}
	return nil
}

// growShellSandboxDataClaim widens the sandbox's data claim to match the agent's.
//
// A StatefulSet's volumeClaimTemplate sizes only the claims it creates, so an
// install from before agentDataStorageSize keeps the 5Gi it was given however the
// template changes — and that claim is the destination sandbox_mirror.py copies
// the agent's working directories into. Expansion is online; the volume stays
// mounted and the shell keeps running.
//
// Best-effort and logged rather than returned. A StorageClass without
// allowVolumeExpansion is how an administrator configured the cluster, and
// failing the reconcile over it would take the whole agent down to fix a volume
// that is merely smaller than we would like. The mirror already refuses to fill
// the volume it is given, so the consequence is a bounded migration, not a broken
// one.
func (r *PlatformAgentReconciler) growShellSandboxDataClaim(ctx context.Context, agent *agentv1alpha1.PlatformAgent) {
	log := logf.FromContext(ctx)
	want := resource.MustParse(agentDataStorageSize)

	name := shellSandboxDataClaimName(agent)
	pvc := &corev1.PersistentVolumeClaim{}
	if err := r.Get(ctx, client.ObjectKey{Name: name, Namespace: agent.Namespace}, pvc); err != nil {
		// Not created yet on a first install: the template sizes it correctly.
		if client.IgnoreNotFound(err) != nil {
			log.Error(err, "could not read the sandbox data claim", "claim", name)
		}
		return
	}

	have := pvc.Spec.Resources.Requests[corev1.ResourceStorage]
	if have.Cmp(want) >= 0 {
		return
	}

	patched := pvc.DeepCopy()
	patched.Spec.Resources.Requests[corev1.ResourceStorage] = want
	if err := r.Patch(ctx, patched, client.MergeFrom(pvc)); err != nil {
		log.Error(err, "could not grow the sandbox data claim; migration into it stays bounded by the space that is there",
			"claim", name, "have", have.String(), "want", want.String())
		return
	}
	log.Info("grew the sandbox data claim to match the agent's",
		"claim", name, "from", have.String(), "to", want.String())
}

// applyShellSandboxStatefulSet applies the StatefulSet, recreating it when the
// API server refuses the update.
//
// Only replicas, ordinals, template, updateStrategy,
// persistentVolumeClaimRetentionPolicy and minReadySeconds are mutable on a
// StatefulSet. Any change to volumeClaimTemplates therefore comes back 422
// Invalid, which without this would error-loop the reconcile on every install
// that already has a sandbox — and take the rest of the agent's reconcile with
// it. Deleting with Orphan propagation leaves the pod and its claims running and
// the replacement adopts the pod by selector, so the shell stays up across the
// swap and the sandbox's disk is never at risk.
func (r *PlatformAgentReconciler) applyShellSandboxStatefulSet(ctx context.Context, agent *agentv1alpha1.PlatformAgent, obj client.Object) error {
	err := r.applyManaged(ctx, agent, obj)
	if !errors.IsInvalid(err) {
		return err
	}

	log := logf.FromContext(ctx)
	log.Info("the sandbox StatefulSet needs an immutable field changed; recreating it with the pod left running",
		"statefulset", obj.GetName(), "reason", err.Error())

	orphan := metav1.DeletePropagationOrphan
	existing := &appsv1.StatefulSet{
		ObjectMeta: metav1.ObjectMeta{Name: obj.GetName(), Namespace: obj.GetNamespace()},
	}
	if delErr := r.Delete(ctx, existing, &client.DeleteOptions{PropagationPolicy: &orphan}); client.IgnoreNotFound(delErr) != nil {
		return fmt.Errorf("failed to delete the sandbox StatefulSet for recreation: %w", delErr)
	}
	return r.applyManaged(ctx, agent, obj)
}

// deleteShellSandbox removes the sandbox for an agent that has it switched off,
// leaving alone anything this controller does not own.
//
// Unlike deleteLegacyCredentialIsolationResources, an unowned object here is logged
// and skipped rather than returned as an error. The names are predictable, so a
// hand-applied StatefulSet called <agent>-shell is a thing an operator can easily
// have left behind — and erroring on it stops the reconcile before the Deployment,
// the Service and the ConfigMap, which takes the whole agent down over a sandbox
// that is switched off. It was observed doing exactly that on a live install.
// Skipping leaves the object running and unmanaged, which is what the person who
// applied it by hand asked for.
func (r *PlatformAgentReconciler) deleteShellSandbox(ctx context.Context, agent *agentv1alpha1.PlatformAgent) error {
	objMeta := metav1.ObjectMeta{Name: shellSandboxName(agent), Namespace: agent.Namespace}
	for _, obj := range []client.Object{
		&appsv1.StatefulSet{ObjectMeta: objMeta},
		&corev1.Service{ObjectMeta: objMeta},
		&networkingv1.NetworkPolicy{ObjectMeta: objMeta},
		// Last. Deleting the identity before the workload that runs under it
		// leaves the pod unable to refresh its projected token while it is
		// still terminating.
		&corev1.ServiceAccount{ObjectMeta: objMeta},
	} {
		if err := r.Get(ctx, client.ObjectKeyFromObject(obj), obj); err != nil {
			if client.IgnoreNotFound(err) != nil {
				return err
			}
			continue
		}
		if !metav1.IsControlledBy(obj, agent) {
			logf.FromContext(ctx).Info("Leaving unowned shell sandbox object in place",
				"kind", fmt.Sprintf("%T", obj), "namespace", obj.GetNamespace(), "name", obj.GetName())
			continue
		}
		if err := client.IgnoreNotFound(r.Delete(ctx, obj)); err != nil {
			return err
		}
	}
	return nil
}

// reconcileCredentialProxy creates the Service the proxy's remote callers reach
// it through, and — unless it lives in the sandbox pod — the Deployment that runs
// it and the NetworkPolicy that narrows who may connect.
//
// The Service is applied in both placements and keeps its name in both, because
// the gateway's chat relay clients dial it and have no business knowing where the
// relays run. buildCredentialProxyService swings its selector; nothing else moves.
//
// Co-located, the workload and its policy belong to the sandbox: the pod is the
// sandbox's StatefulSet, and the NetworkPolicy that governs it is
// buildShellSandboxNetworkPolicy, which has to cover the shell container in the
// same rules anyway. Applying this file's NetworkPolicy as well would leave an
// object whose podSelector matches nothing — harmless until someone reads it as
// the policy in force. credential_proxy_manifests.go carries the placement
// reasoning.
func (r *PlatformAgentReconciler) reconcileCredentialProxy(ctx context.Context, agent *agentv1alpha1.PlatformAgent, policyHash string) error {
	// Only the sandbox's placement is this function's business. With the sandbox
	// off, <name>-credential-proxy belongs to reconcileCredentialBroker, which
	// owns the same names in both directions from spec.security.splitCredentialBrokerPod.
	// Two owners applying and deleting one Deployment on alternate passes would
	// restart the credential runtime forever.
	if !shellSandboxEnabled(agent) {
		return nil
	}
	objs := []client.Object{buildCredentialProxyService(agent)}
	if credentialProxyColocated(agent) {
		if err := r.deleteStandaloneCredentialProxy(ctx, agent); err != nil {
			return err
		}
	} else {
		objs = append(objs,
			buildCredentialProxyDeployment(agent, policyHash),
			buildCredentialProxyNetworkPolicy(agent),
		)
	}
	for _, obj := range objs {
		if err := ctrl.SetControllerReference(agent, obj, r.Scheme); err != nil {
			return fmt.Errorf("failed to set controller reference on credential proxy %T %s/%s: %w", obj, obj.GetNamespace(), obj.GetName(), err)
		}
		if err := r.applyManaged(ctx, agent, obj); err != nil {
			return fmt.Errorf("failed to apply credential proxy %T %s/%s: %w", obj, obj.GetNamespace(), obj.GetName(), err)
		}
	}
	return nil
}

// deleteStandaloneCredentialProxy removes the proxy's own pod once it has moved
// into the sandbox.
//
// Without this, turning federation on leaves two credential runtimes: the old
// Deployment keeps its metadata-server identity and keeps pulling the Google Chat
// subscription, so the Service — whose selector has just swung to the sandbox —
// load-balances nothing to it while it quietly buffers messages nobody fetches.
// That is the failure buildCredentialProxyDeployment's Recreate strategy exists to
// avoid, arriving by a different route.
//
// Unowned objects are left alone and logged rather than erroring, for the reason
// deleteShellSandbox gives: a hand-applied object of the same name must not be
// able to stop the reconcile before the agent's own Deployment.
func (r *PlatformAgentReconciler) deleteStandaloneCredentialProxy(ctx context.Context, agent *agentv1alpha1.PlatformAgent) error {
	objMeta := metav1.ObjectMeta{Name: credentialProxyName(agent), Namespace: agent.Namespace}
	for _, obj := range []client.Object{
		&appsv1.Deployment{ObjectMeta: objMeta},
		&networkingv1.NetworkPolicy{ObjectMeta: objMeta},
	} {
		if err := r.Get(ctx, client.ObjectKeyFromObject(obj), obj); err != nil {
			if client.IgnoreNotFound(err) != nil {
				return err
			}
			continue
		}
		if !metav1.IsControlledBy(obj, agent) {
			logf.FromContext(ctx).Info("Leaving unowned credential proxy object in place",
				"kind", fmt.Sprintf("%T", obj), "namespace", obj.GetNamespace(), "name", obj.GetName())
			continue
		}
		if err := client.IgnoreNotFound(r.Delete(ctx, obj)); err != nil {
			return err
		}
	}
	return nil
}

// reconcileCredentialBrokerTokenReviewRBAC applies, or removes, the one verb the
// broker needs to authenticate the callers it can no longer take on trust.
//
// Keyed on credentialBrokerOffPod rather than on either switch alone, because
// that is the predicate which arms the authentication: off the agent's Pod the
// broker stops treating loopback as the control and reviews every bearer token
// it is handed. Gating the grant on the split alone was how it shipped, and the
// sandbox moves the broker without setting the split — so the runtime asked the
// API server a question it had no permission to ask. The TokenReview came back
// 403, which the authenticator correctly treats as a rejection rather than an
// allow, and every credentialed command in the sandbox failed with a 401 about
// the caller instead of a message about the missing rule.
//
// It lives here rather than in either placement's reconciler for the same
// reason: two owners applying and deleting one cluster-scoped object on
// alternate passes is how the Deployment and Service went wrong first.
func (r *PlatformAgentReconciler) reconcileCredentialBrokerTokenReviewRBAC(ctx context.Context, agent *agentv1alpha1.PlatformAgent) error {
	tokenReviewName := fmt.Sprintf("kubeagents:tokenreview:%s:%s", agent.Namespace, agent.Name)

	if !credentialBrokerOffPod(agent) {
		for _, object := range []client.Object{
			&rbacv1.ClusterRoleBinding{ObjectMeta: metav1.ObjectMeta{Name: tokenReviewName}},
			&rbacv1.ClusterRole{ObjectMeta: metav1.ObjectMeta{Name: tokenReviewName}},
		} {
			if err := r.deleteIfManaged(ctx, object); err != nil {
				return err
			}
		}
		return nil
	}

	// One verb on one virtual resource, which grants no read access to anything.
	role := buildCredentialBrokerTokenReviewRole(agent)
	if err := r.applyManaged(ctx, agent, role); err != nil {
		return fmt.Errorf("failed to reconcile credential broker TokenReview ClusterRole: %w", err)
	}
	binding := buildClusterRoleBinding(agent, tokenReviewName, role.Name)
	if err := r.applyManaged(ctx, agent, binding); err != nil {
		return fmt.Errorf("failed to reconcile credential broker TokenReview ClusterRoleBinding: %w", err)
	}
	return nil
}

// reconcileCredentialBroker renders, or removes, the broker's own Pod.
//
// It owns <name>-credential-proxy in both directions: applied when
// spec.security.splitCredentialBrokerPod is true, deleted when it is false, so
// that turning the gate back off does not leave a second broker running against
// the same workspace. That is cleanup of this controller's own workload, not the
// removal of a guardrail it did not create — see
// deleteLegacyCredentialIsolationResources.
func (r *PlatformAgentReconciler) reconcileCredentialBroker(ctx context.Context, agent *agentv1alpha1.PlatformAgent, policyHash string) error {
	// The sandbox takes precedence, and this function stands down entirely
	// rather than falling through to its delete branch — which would remove the
	// Deployment and Service reconcileCredentialProxy has just applied under the
	// same names. See the note there.
	if shellSandboxEnabled(agent) {
		return nil
	}
	log := logf.FromContext(ctx)

	if !credentialBrokerIsSplit(agent) {
		owned := []client.Object{
			&appsv1.Deployment{ObjectMeta: metav1.ObjectMeta{Name: credentialBrokerName(agent), Namespace: agent.Namespace}},
			&corev1.Service{ObjectMeta: metav1.ObjectMeta{Name: credentialBrokerName(agent), Namespace: agent.Namespace}},
		}
		for _, object := range owned {
			if err := r.deleteIfOwned(ctx, agent, object); err != nil {
				return err
			}
		}
		return nil
	}

	r.warnSplitNeedsSharedFilesystem(ctx, agent)

	homeDir := defaultAgentHome
	if h := agent.Spec.Harness; h != nil && h.Hermes != nil && h.Hermes.AgentHome != "" {
		homeDir = h.Hermes.AgentHome
	}
	deployment := buildCredentialBrokerDeployment(agent, policyHash, homeDir)
	if err := ctrl.SetControllerReference(agent, deployment, r.Scheme); err != nil {
		return err
	}
	if err := r.applyManaged(ctx, agent, deployment); err != nil {
		return fmt.Errorf("failed to reconcile credential broker Deployment: %w", err)
	}

	service := buildCredentialBrokerService(agent)
	if err := ctrl.SetControllerReference(agent, service, r.Scheme); err != nil {
		return err
	}
	if err := r.applyManaged(ctx, agent, service); err != nil {
		return fmt.Errorf("failed to reconcile credential broker Service: %w", err)
	}

	log.Info("credential broker runs in its own Pod",
		"deployment", deployment.Name, "service", service.Name)
	return nil
}

// warnSplitNeedsSharedFilesystem says so, loudly, when the split is enabled
// while the two Pods cannot see the same files.
//
// The broker runs proxied commands with a working directory the agent created
// on this volume, so today both Pods have to mount it read-write at the same
// path. A ReadWriteOnce claim cannot do that across nodes: the broker Pod stays
// Pending with a Multi-Attach error and never becomes a Service endpoint, so
// the agent sees a connection refused on every command. The containment check
// in the broker does not catch it either — it is lexical, both Pods are
// configured with the same workspace root, so the path always looks right and
// what is missing is the data behind it.
//
// The access mode is what this reads, because it is the one signal available.
// It is not a product requirement, and the fix is not to go and buy a
// ReadWriteMany volume — that is one way to satisfy today's design and an
// operator may pick it, but the managed options bill on provisioned capacity
// with a floor far above what a workspace needs. The supported answer is to
// leave the split off until the broker owns the workspace on a volume of its
// own and takes content rather than a directory, a separate change that
// removes the coupling entirely. Co-scheduling both Pods on one node is
// not the answer: it deadlocks the next rolling update on the volume and makes
// the two Pods a single failure domain.
//
// A log line rather than a Degraded status, because unlike the event-watcher
// refusal there is nothing the operator can set to make this correct — the
// access mode of an existing claim cannot be changed in place, and refusing to
// reconcile would not help them.
func (r *PlatformAgentReconciler) warnSplitNeedsSharedFilesystem(ctx context.Context, agent *agentv1alpha1.PlatformAgent) {
	log := logf.FromContext(ctx)
	pvc := &corev1.PersistentVolumeClaim{}
	if err := r.Get(ctx, types.NamespacedName{Namespace: agent.Namespace, Name: agent.Name + "-data"}, pvc); err != nil {
		return
	}
	if slices.Contains(pvc.Spec.AccessModes, corev1.ReadWriteMany) {
		return
	}
	log.Info("WARNING: splitCredentialBrokerPod is enabled, and the agent Pod and the broker Pod cannot see the "+
		"same files: the broker runs proxied commands in a directory the agent created on this claim, and its "+
		"access mode does not let both Pods mount it read-write at once. The broker Pod will stay Pending with a "+
		"Multi-Attach error and every proxied command will report the credential proxy as unavailable. Turn the "+
		"split off. A ReadWriteMany claim also satisfies today's design and is a choice available to you, but it "+
		"is not what this product asks for; the supported path is to wait for the broker to own the workspace and "+
		"take content rather than a directory. Do not co-schedule the two Pods to work around this — it deadlocks "+
		"the next rolling update on the volume.",
		"claim", pvc.Name, "accessModes", pvc.Spec.AccessModes)
}

// deleteIfOwned removes a namespaced object this controller created, refusing
// to touch one it does not own.
func (r *PlatformAgentReconciler) deleteIfOwned(ctx context.Context, agent *agentv1alpha1.PlatformAgent, object client.Object) error {
	if err := r.Get(ctx, client.ObjectKeyFromObject(object), object); err != nil {
		return client.IgnoreNotFound(err)
	}
	if !metav1.IsControlledBy(object, agent) {
		return fmt.Errorf("refusing to delete unowned %T %s/%s", object, object.GetNamespace(), object.GetName())
	}
	return client.IgnoreNotFound(r.Delete(ctx, object))
}

// deleteIfManaged removes a cluster-scoped object this controller created.
// Cluster-scoped objects cannot carry an owner reference to a namespaced agent,
// so the managed-by label is the only evidence of provenance there is.
func (r *PlatformAgentReconciler) deleteIfManaged(ctx context.Context, object client.Object) error {
	if err := r.Get(ctx, client.ObjectKeyFromObject(object), object); err != nil {
		return client.IgnoreNotFound(err)
	}
	if object.GetLabels()[labelManagedBy] != fieldOwner {
		return fmt.Errorf("refusing to delete unmanaged %T %s", object, object.GetName())
	}
	return client.IgnoreNotFound(r.Delete(ctx, object))
}

func (r *PlatformAgentReconciler) reconcileService(ctx context.Context, agent *agentv1alpha1.PlatformAgent) error {
	svc := buildPlatformService(agent)
	if err := ctrl.SetControllerReference(agent, svc, r.Scheme); err != nil {
		return fmt.Errorf("failed to set controller reference on Service %s/%s: %w", svc.Namespace, svc.Name, err)
	}
	if err := r.applyManaged(ctx, agent, svc); err != nil {
		return fmt.Errorf("failed to apply Service %s/%s: %w", svc.Namespace, svc.Name, err)
	}
	return nil
}

func (r *PlatformAgentReconciler) reconcilePodDisruptionBudget(ctx context.Context, agent *agentv1alpha1.PlatformAgent) error {
	pdb := buildPlatformPDB(agent)
	if err := ctrl.SetControllerReference(agent, pdb, r.Scheme); err != nil {
		return fmt.Errorf("failed to set controller reference on PodDisruptionBudget %s/%s: %w", pdb.Namespace, pdb.Name, err)
	}
	if err := r.clearForeignPDBBudgetField(ctx, pdb); err != nil {
		return err
	}
	if err := r.applyManaged(ctx, agent, pdb); err != nil {
		return fmt.Errorf("failed to apply PodDisruptionBudget %s/%s: %w", pdb.Namespace, pdb.Name, err)
	}
	return nil
}

// clearForeignPDBBudgetField removes whichever of minAvailable/maxUnavailable
// the desired budget does not use, when the live object carries it anyway.
//
// Every other object this controller reconciles recovers from hand-edits on its
// own, because a server-side apply with ForceOwnership takes back any field it
// sets. A PodDisruptionBudget does not, and the failure is permanent rather than
// cosmetic. The two budget fields are mutually exclusive, so the apply cannot
// simply overwrite the foreign one: SSA does not remove fields it never owned,
// leaving the merged object with both set, which the API server rejects with
// "minAvailable and maxUnavailable cannot be both set". That error fails the
// whole Reconcile, so every step after this one — the NetworkPolicy included —
// stops running until someone deletes the stray field by hand. An administrator
// tightening the singleton default to minAvailable is all it takes; observed
// while drain-testing this budget.
//
// Nulling the field through a merge patch deletes it from the object, and with
// it the other manager's claim in managedFields, so the apply that follows is
// unambiguous. This runs on the way to a normal apply, not just after damage:
// when the live object already agrees, the switch falls through and nothing is
// patched.
func (r *PlatformAgentReconciler) clearForeignPDBBudgetField(ctx context.Context, desired *policyv1.PodDisruptionBudget) error {
	var live policyv1.PodDisruptionBudget
	if err := r.Get(ctx, client.ObjectKeyFromObject(desired), &live); err != nil {
		if errors.IsNotFound(err) {
			return nil
		}
		return fmt.Errorf("failed to get PodDisruptionBudget %s/%s: %w", desired.Namespace, desired.Name, err)
	}

	var foreign string
	switch {
	case desired.Spec.MaxUnavailable != nil && live.Spec.MinAvailable != nil:
		foreign = "minAvailable"
	case desired.Spec.MinAvailable != nil && live.Spec.MaxUnavailable != nil:
		foreign = "maxUnavailable"
	default:
		return nil
	}

	patch := client.RawPatch(types.MergePatchType, fmt.Appendf(nil, `{"spec":{%q:null}}`, foreign))
	if err := r.Patch(ctx, &live, patch); err != nil {
		return fmt.Errorf("failed to clear %s on PodDisruptionBudget %s/%s: %w", foreign, desired.Namespace, desired.Name, err)
	}
	return nil
}

func (r *PlatformAgentReconciler) reconcileNetworkPolicy(ctx context.Context, agent *agentv1alpha1.PlatformAgent, profile netpolProfile, otlpEndpoint string, otlpDisabled bool) error {
	if !profile.Generated {
		var existingNetpol networkingv1.NetworkPolicy
		if err := r.Get(ctx, types.NamespacedName{Namespace: agent.Namespace, Name: agent.Name + "-gateway-netpol"}, &existingNetpol); err == nil {
			if metav1.IsControlledBy(&existingNetpol, agent) {
				if err := r.Delete(ctx, &existingNetpol); err != nil && !errors.IsNotFound(err) {
					return fmt.Errorf("failed to delete disabled NetworkPolicy %s/%s: %w", existingNetpol.Namespace, existingNetpol.Name, err)
				}
				logf.FromContext(ctx).Info("Deleted owner-referenced NetworkPolicy because spec.networkPolicy.enabled is false", "namespace", existingNetpol.Namespace, "name", existingNetpol.Name)
			}
		} else if !errors.IsNotFound(err) {
			return fmt.Errorf("failed to get NetworkPolicy %s/%s: %w", agent.Namespace, agent.Name+"-gateway-netpol", err)
		}

		// Read before deleting, and check ownership, exactly as the NetworkPolicy
		// above does. The name is agent-prefixed and namespaced, so a collision is
		// unlikely -- but "enabled: false" is a request to stop managing policy, not
		// a licence to delete a policy somebody else created under that name.
		//
		// The FQDN cleanup on the ENABLED path below (fqdnEnabled == false) deletes
		// the same name unguarded, and deliberately still does: an operator old
		// enough to have created that policy without an owner reference would leave
		// it behind here, and FQDN filtering the user just switched off would keep
		// applying. That risk is not worth taking on this path, where the whole
		// point is to stop managing policy at all.
		fqdnNetpol := &unstructured.Unstructured{}
		fqdnNetpol.SetGroupVersionKind(schema.GroupVersionKind{
			Group:   "networking.gke.io",
			Version: "v1alpha1",
			Kind:    "FQDNNetworkPolicy",
		})
		fqdnName := agent.Name + "-fqdn-netpol"
		if err := r.Get(ctx, types.NamespacedName{Namespace: agent.Namespace, Name: fqdnName}, fqdnNetpol); err == nil {
			if metav1.IsControlledBy(fqdnNetpol, agent) {
				if err := r.Delete(ctx, fqdnNetpol); err != nil && !isCRDNotInstalledError(err) {
					return fmt.Errorf("failed to clean up disabled FQDNNetworkPolicy %s/%s: %w", agent.Namespace, fqdnName, err)
				}
				logf.FromContext(ctx).Info("Deleted owner-referenced FQDNNetworkPolicy because spec.networkPolicy.enabled is false", "namespace", agent.Namespace, "name", fqdnName)
			}
		} else if !isCRDNotInstalledError(err) {
			return fmt.Errorf("failed to get FQDNNetworkPolicy %s/%s: %w", agent.Namespace, fqdnName, err)
		}
		return nil
	}

	var apiTargets []string
	if r.APIServerIP != "" {
		apiTargets = append(apiTargets, r.APIServerIP)
	}

	var k8sSvc corev1.Service
	if err := r.Get(ctx, types.NamespacedName{Namespace: "default", Name: "kubernetes"}, &k8sSvc); err == nil {
		if ip := strings.TrimSpace(k8sSvc.Spec.ClusterIP); ip != "" && ip != "None" && net.ParseIP(ip) != nil {
			apiTargets = append(apiTargets, ip)
		}
	} else if !errors.IsNotFound(err) {
		logf.FromContext(ctx).Info("Failed to discover default/kubernetes Service ClusterIP", "error", err)
	}

	// Use APIReader (live non-cached reader) for default/kubernetes Endpoints to avoid
	// starting an unconstrained cluster-wide Endpoints informer / watch cache.
	endpointsReader := client.Reader(r.Client)
	if r.APIReader != nil {
		endpointsReader = r.APIReader
	}

	var k8sEndpoints corev1.Endpoints
	if err := endpointsReader.Get(ctx, types.NamespacedName{Namespace: "default", Name: "kubernetes"}, &k8sEndpoints); err == nil {
		for _, subset := range k8sEndpoints.Subsets {
			for _, addr := range subset.Addresses {
				if addr.IP != "" {
					apiTargets = append(apiTargets, addr.IP)
				}
			}
		}
	} else if !errors.IsNotFound(err) {
		logf.FromContext(ctx).Info("Failed to discover default/kubernetes Endpoints", "error", err)
	}

	parseCIDRTarget := func(annotationName, raw string) {
		raw = strings.TrimSpace(raw)
		if raw == "" {
			return
		}
		// normalizeCIDRTarget, not a local parse: it takes the address family from
		// the address rather than the mask width, so an IPv4-mapped IPv6 block is
		// measured against the IPv4 floor it will actually print as.
		// ::ffff:a00:0/104 used to clear the /48 IPv6 floor here and land in the
		// list as 10.0.0.0/8; it is now rejected, while ::ffff:a00:0/108 still
		// passes because /108 is the IPv4 /12 that is exactly the floor.
		ipNet, ok := normalizeCIDRTarget(raw, true)
		if !ok {
			logf.FromContext(ctx).Info("Ignoring CIDR in annotation: unparseable, or broader than the /12 (IPv4) or /48 (IPv6) floor", "annotation", annotationName, "cidr", raw)
			return
		}
		apiTargets = append(apiTargets, ipNet.String())
	}

	appendCIDRs := func(sourceName, rawList string) {
		if rawList == "" {
			return
		}
		cidrs := strings.Split(rawList, ",")
		if len(cidrs) > maxCIDRsPerAnnotation {
			logf.FromContext(ctx).Info("Truncating CIDR list to max allowed CIDRs", "source", sourceName, "max", maxCIDRsPerAnnotation, "total", len(cidrs))
			cidrs = cidrs[:maxCIDRsPerAnnotation]
		}
		for _, cidr := range cidrs {
			parseCIDRTarget(sourceName, cidr)
		}
	}

	if agent.Annotations != nil {
		appendCIDRs(AnnotationAPIServerCIDR, agent.Annotations[AnnotationAPIServerCIDR])
		appendCIDRs(AnnotationCustomEgressCIDRs, agent.Annotations[AnnotationCustomEgressCIDRs])
	}
	appendCIDRs("KUBERNETES_API_SERVER_CIDR", r.APIServerCIDROverride)

	// 1. Reconcile or clean up companion FQDNNetworkPolicy (networking.gke.io/v1alpha1) on GKE Dataplane V2 clusters
	fqdnEnabled := isFQDNNetworkPolicyEnabled(agent)
	if fqdnEnabled {
		fqdnNetpol := buildFQDNNetworkPolicy(agent)
		if err := ctrl.SetControllerReference(agent, fqdnNetpol, r.Scheme); err != nil {
			return fmt.Errorf("failed to set controller reference on FQDNNetworkPolicy %s/%s: %w", fqdnNetpol.GetNamespace(), fqdnNetpol.GetName(), err)
		}
		if err := r.applyManaged(ctx, agent, fqdnNetpol); err != nil {
			if isCRDNotInstalledError(err) {
				logf.FromContext(ctx).Info("FQDNNetworkPolicy CRD (networking.gke.io/v1alpha1) not present in cluster; keeping blanket external egress rule", "error", err)
				fqdnEnabled = false
			} else {
				return fmt.Errorf("failed to apply FQDNNetworkPolicy %s/%s: %w", fqdnNetpol.GetNamespace(), fqdnNetpol.GetName(), err)
			}
		}
	} else {
		fqdnNetpol := &unstructured.Unstructured{}
		fqdnNetpol.SetGroupVersionKind(schema.GroupVersionKind{
			Group:   "networking.gke.io",
			Version: "v1alpha1",
			Kind:    "FQDNNetworkPolicy",
		})
		fqdnNetpol.SetName(agent.Name + "-fqdn-netpol")
		fqdnNetpol.SetNamespace(agent.Namespace)
		if err := r.Delete(ctx, fqdnNetpol); err != nil && !isCRDNotInstalledError(err) {
			return fmt.Errorf("failed to clean up disabled FQDNNetworkPolicy %s/%s: %w", fqdnNetpol.GetNamespace(), fqdnNetpol.GetName(), err)
		}
	}

	// 2. Build and reconcile standard NetworkPolicy (omits blanket external HTTPS egress only if replacement FQDN policy is active)
	netpol := buildNetworkPolicy(agent, apiTargets, profile, fqdnEnabled, otlpEndpoint, otlpDisabled)
	if err := ctrl.SetControllerReference(agent, netpol, r.Scheme); err != nil {
		return fmt.Errorf("failed to set controller reference on NetworkPolicy %s/%s: %w", netpol.Namespace, netpol.Name, err)
	}
	if err := r.applyManaged(ctx, agent, netpol); err != nil {
		return fmt.Errorf("failed to apply NetworkPolicy %s/%s: %w", netpol.Namespace, netpol.Name, err)
	}

	return nil
}

// cleanupAgentRBAC dynamically purges un-wanted or all RBAC resources for a PlatformAgent.
// When deleteAll is true (called during finalization), all RBAC resources are deleted.
// When deleteAll is false (called during reconcile), active canonical bindings (minimal, local, leader) are preserved.
func (r *PlatformAgentReconciler) cleanupAgentRBAC(ctx context.Context, agent *agentv1alpha1.PlatformAgent, deleteAll bool) error {
	saName := agent.Name
	if agent.Spec.Security != nil && agent.Spec.Security.ServiceAccountName != "" {
		saName = agent.Spec.Security.ServiceAccountName
	}
	minimalBindingName := fmt.Sprintf("kubeagents:minimal:%s:%s", agent.Namespace, agent.Name)
	localBindingName := fmt.Sprintf("kubeagents:local:%s:%s", agent.Namespace, agent.Name)
	leaderBindingName := fmt.Sprintf("kubeagents:leader:%s:%s", agent.Namespace, agent.Name)
	// The split credential broker's TokenReview grant is applied by
	// reconcileCredentialBroker on every reconcile, through applyManaged,
	// which stamps the same instance labels this cleanup selects on. Reaping
	// it here would delete what the same pass just applied — the reconcile
	// would never stabilize. Spared like the minimal binding; deleteAll
	// (the finalizer path) still removes it.
	tokenReviewName := fmt.Sprintf("kubeagents:tokenreview:%s:%s", agent.Namespace, agent.Name)

	// 1. Fast, dynamic cleanup of ClusterRoleBindings using targeted label selectors (current and legacy instance labels)
	var labeledClusterRoleBindings rbacv1.ClusterRoleBindingList
	if err := r.List(ctx, &labeledClusterRoleBindings, client.MatchingLabels{
		"kubeagents.x-k8s.io/agent-name":      agent.Name,
		"kubeagents.x-k8s.io/agent-namespace": agent.Namespace,
	}); err != nil {
		return fmt.Errorf("failed to list labeled ClusterRoleBindings: %w", err)
	}
	for i := range labeledClusterRoleBindings.Items {
		crb := &labeledClusterRoleBindings.Items[i]
		if !deleteAll && (crb.Name == minimalBindingName || crb.Name == tokenReviewName) {
			continue
		}
		if (strings.HasPrefix(crb.Name, "kubeagents:") || strings.HasPrefix(crb.Name, "kubeagents-")) && crb.DeletionTimestamp.IsZero() {
			if err := client.IgnoreNotFound(r.Delete(ctx, crb)); err != nil {
				return fmt.Errorf("failed to clean up legacy ClusterRoleBinding %s: %w", crb.Name, err)
			}
		}
	}

	instLabel := instanceLabel(agent.Namespace, agent.Name)
	var legacyLabeledCRBs rbacv1.ClusterRoleBindingList
	if err := r.List(ctx, &legacyLabeledCRBs, client.MatchingLabels{
		"app.kubernetes.io/instance": instLabel,
		"app.kubernetes.io/part-of":  "kube-agents",
	}); err != nil {
		return fmt.Errorf("failed to list legacy labeled ClusterRoleBindings: %w", err)
	}
	for i := range legacyLabeledCRBs.Items {
		crb := &legacyLabeledCRBs.Items[i]
		if !deleteAll && (crb.Name == minimalBindingName || crb.Name == tokenReviewName) {
			continue
		}
		if (strings.HasPrefix(crb.Name, "kubeagents:") || strings.HasPrefix(crb.Name, "kubeagents-")) && crb.DeletionTimestamp.IsZero() {
			if err := client.IgnoreNotFound(r.Delete(ctx, crb)); err != nil {
				return fmt.Errorf("failed to clean up legacy ClusterRoleBinding %s: %w", crb.Name, err)
			}
		}
	}

	// 2. Dynamic cleanup of ClusterRoles using label selector
	var legacyClusterRoles rbacv1.ClusterRoleList
	if err := r.List(ctx, &legacyClusterRoles, client.MatchingLabels{
		"app.kubernetes.io/instance": instLabel,
		"app.kubernetes.io/part-of":  "kube-agents",
	}); err != nil {
		return fmt.Errorf("failed to list legacy ClusterRoles: %w", err)
	}
	for i := range legacyClusterRoles.Items {
		cr := &legacyClusterRoles.Items[i]
		if !deleteAll && (cr.Name == fmt.Sprintf("kubeagents:minimal:%s:%s", agent.Namespace, agent.Name) || cr.Name == tokenReviewName) {
			continue
		}
		if (strings.HasPrefix(cr.Name, "kubeagents:") || strings.HasPrefix(cr.Name, "kubeagents-")) && cr.DeletionTimestamp.IsZero() {
			if err := client.IgnoreNotFound(r.Delete(ctx, cr)); err != nil {
				return fmt.Errorf("failed to delete legacy ClusterRole %s: %w", cr.Name, err)
			}
		}
	}

	// 4. Dynamically clean up RoleBindings in the agent's namespace (with SA swap protection)
	var existingRoleBindings rbacv1.RoleBindingList
	if err := r.List(ctx, &existingRoleBindings, client.InNamespace(agent.Namespace)); err != nil {
		return fmt.Errorf("failed to list RoleBindings in namespace %s: %w", agent.Namespace, err)
	}
	for i := range existingRoleBindings.Items {
		rb := &existingRoleBindings.Items[i]
		if !deleteAll && (rb.Name == localBindingName || rb.Name == leaderBindingName) {
			continue
		}
		isTargetSA := false
		for _, subj := range rb.Subjects {
			if subj.Kind == "ServiceAccount" &&
				(subj.Namespace == "" || subj.Namespace == agent.Namespace) &&
				(subj.Name == saName || subj.Name == agent.Name) {
				isTargetSA = true
				break
			}
		}
		if isTargetSA && (strings.HasPrefix(rb.Name, "kubeagents:") || strings.HasPrefix(rb.Name, "kubeagents-")) && rb.DeletionTimestamp.IsZero() {
			if err := client.IgnoreNotFound(r.Delete(ctx, rb)); err != nil {
				return fmt.Errorf("failed to clean up legacy RoleBinding %s: %w", rb.Name, err)
			}
		}
	}

	// 5. Clean up local and leader Role/RoleBindings if deleteAll is requested
	if deleteAll {
		rLeader := &rbacv1.Role{ObjectMeta: metav1.ObjectMeta{Name: leaderBindingName, Namespace: agent.Namespace}}
		if err := client.IgnoreNotFound(r.Delete(ctx, rLeader)); err != nil {
			return fmt.Errorf("failed to delete leader Role %s: %w", leaderBindingName, err)
		}

		rbLeader := &rbacv1.RoleBinding{ObjectMeta: metav1.ObjectMeta{Name: leaderBindingName, Namespace: agent.Namespace}}
		if err := client.IgnoreNotFound(r.Delete(ctx, rbLeader)); err != nil {
			return fmt.Errorf("failed to delete leader RoleBinding %s: %w", leaderBindingName, err)
		}

		rLocal := &rbacv1.Role{ObjectMeta: metav1.ObjectMeta{Name: localBindingName, Namespace: agent.Namespace}}
		if err := client.IgnoreNotFound(r.Delete(ctx, rLocal)); err != nil {
			return fmt.Errorf("failed to delete local Role %s: %w", localBindingName, err)
		}

		rbLocal := &rbacv1.RoleBinding{ObjectMeta: metav1.ObjectMeta{Name: localBindingName, Namespace: agent.Namespace}}
		if err := client.IgnoreNotFound(r.Delete(ctx, rbLocal)); err != nil {
			return fmt.Errorf("failed to delete local RoleBinding %s: %w", localBindingName, err)
		}
	}

	return nil
}

func (r *PlatformAgentReconciler) reconcileRBAC(ctx context.Context, agent *agentv1alpha1.PlatformAgent) error {
	minimalBindingName := fmt.Sprintf("kubeagents:minimal:%s:%s", agent.Namespace, agent.Name)
	localBindingName := fmt.Sprintf("kubeagents:local:%s:%s", agent.Namespace, agent.Name)
	leaderBindingName := fmt.Sprintf("kubeagents:leader:%s:%s", agent.Namespace, agent.Name)

	// Reconcile minimal read-only audit ClusterRole and ClusterRoleBinding
	minimalRole := buildMinimalPlatformRole(agent)
	if err := r.applyManaged(ctx, agent, minimalRole); err != nil {
		return fmt.Errorf("failed to reconcile minimal ClusterRole: %w", err)
	}

	crbMinimal := buildClusterRoleBinding(agent, minimalBindingName, minimalRole.Name)
	if err := r.applyManaged(ctx, agent, crbMinimal); err != nil {
		return fmt.Errorf("failed to reconcile minimal ClusterRoleBinding: %w", err)
	}

	// Reconcile namespace-scoped Role and RoleBinding for inspecting PlatformAgent CRs
	localRole := buildPlatformLocalRole(agent)
	if err := ctrl.SetControllerReference(agent, localRole, r.Scheme); err != nil {
		return fmt.Errorf("failed to set controller reference on local Role: %w", err)
	}
	if err := r.applyManaged(ctx, agent, localRole); err != nil {
		return fmt.Errorf("failed to reconcile local Role: %w", err)
	}

	localBinding := buildRoleBinding(agent, localBindingName, localRole.Name)
	if err := ctrl.SetControllerReference(agent, localBinding, r.Scheme); err != nil {
		return fmt.Errorf("failed to set controller reference on local RoleBinding: %w", err)
	}
	if err := r.applyManaged(ctx, agent, localBinding); err != nil {
		return fmt.Errorf("failed to reconcile local RoleBinding: %w", err)
	}

	// Reconcile leader election Role and RoleBinding
	leaderRole := buildPlatformLeaderRole(agent)
	if err := ctrl.SetControllerReference(agent, leaderRole, r.Scheme); err != nil {
		return fmt.Errorf("failed to set controller reference on leader Role: %w", err)
	}
	if err := r.applyManaged(ctx, agent, leaderRole); err != nil {
		return fmt.Errorf("failed to reconcile leader Role: %w", err)
	}

	rbLeader := buildLeaderRoleBinding(agent, leaderBindingName, leaderRole.Name)
	if err := ctrl.SetControllerReference(agent, rbLeader, r.Scheme); err != nil {
		return fmt.Errorf("failed to set controller reference on leader RoleBinding: %w", err)
	}
	if err := r.applyManaged(ctx, agent, rbLeader); err != nil {
		return fmt.Errorf("failed to reconcile leader RoleBinding: %w", err)
	}

	// Clean up legacy or un-canonical RBAC definitions after new roles are applied (Zero-Downtime Upgrade)
	if err := r.cleanupAgentRBAC(ctx, agent, false); err != nil {
		return err
	}

	return nil
}

// updateStatusReady writes the agent's status and returns the phase it settled on, so
// the caller can decide whether the agent is still converging. otlpEndpoint, otlpSource,
// and netpolProfile are the resolved telemetry and network policy wiring; they are reported
// rather than derived because discovery is otherwise invisible to anyone reading the CR.
func (r *PlatformAgentReconciler) updateStatusReady(ctx context.Context, agent *agentv1alpha1.PlatformAgent, otlpEndpoint, otlpSource string, netpolProfile netpolProfile) (string, error) {
	newDeploymentStatusName := ""
	newDeploymentStatusReadyReplicas := int32(0)
	var errWorkload error

	if useStatefulSet(agent) {
		sts := &appsv1.StatefulSet{}
		errWorkload = r.Get(ctx, types.NamespacedName{Namespace: agent.Namespace, Name: agent.Name + "-gateway"}, sts)
		if errWorkload != nil && !errors.IsNotFound(errWorkload) {
			return "", fmt.Errorf("failed to get StatefulSet for status update: %w", errWorkload)
		}
		if errWorkload == nil {
			newDeploymentStatusName = sts.Name
			newDeploymentStatusReadyReplicas = sts.Status.ReadyReplicas
		}
	} else {
		dep := &appsv1.Deployment{}
		errWorkload = r.Get(ctx, types.NamespacedName{Namespace: agent.Namespace, Name: agent.Name + "-gateway"}, dep)
		if errWorkload != nil && !errors.IsNotFound(errWorkload) {
			return "", fmt.Errorf("failed to get Deployment for status update: %w", errWorkload)
		}
		if errWorkload == nil {
			newDeploymentStatusName = dep.Name
			newDeploymentStatusReadyReplicas = dep.Status.ReadyReplicas
		}
	}

	// Fetch actual PVC
	pvc := &corev1.PersistentVolumeClaim{}
	errPVC := r.Get(ctx, types.NamespacedName{Namespace: agent.Namespace, Name: agent.Name + "-data"}, pvc)
	if errPVC != nil && !errors.IsNotFound(errPVC) {
		return "", fmt.Errorf("failed to get PVC for status update: %w", errPVC)
	}
	newStorageStatusBound := false
	if errPVC == nil {
		newStorageStatusBound = (pvc.Status.Phase == corev1.ClaimBound)
	}

	// Fetch actual Service
	svc := &corev1.Service{}
	errSvc := r.Get(ctx, types.NamespacedName{Namespace: agent.Namespace, Name: agent.Name}, svc)
	if errSvc != nil && !errors.IsNotFound(errSvc) {
		return "", fmt.Errorf("failed to get Service for status update: %w", errSvc)
	}
	newServiceStatusEndpoint := ""
	newAddress := ""
	if errSvc == nil {
		newServiceStatusEndpoint = fmt.Sprintf("http://%s.%s.svc.cluster.local:8642", svc.Name, svc.Namespace)
		newAddress = fmt.Sprintf("%s.%s.svc.cluster.local", svc.Name, svc.Namespace)
	}

	// Determine Phase and Condition
	newPhase := "Provisioning"
	condStatus := metav1.ConditionFalse
	condReason := "Provisioning"
	condMsg := "Waiting for deployment replicas to be ready"
	if errWorkload == nil && newDeploymentStatusReadyReplicas > 0 {
		newPhase = "Ready"
		condStatus = metav1.ConditionTrue
		condReason = "Reconciled"
		condMsg = "Agent sandbox and Envoy credential sidecar are fully reconciled"
	} else if errWorkload == nil {
		if phaseOverride, reasonOverride, msgOverride := r.getDeploymentStatusDetails(ctx, agent); reasonOverride != "Provisioning" {
			newPhase = phaseOverride
			condReason = reasonOverride
			condMsg = msgOverride
		}
	}

	gitRepoErr := error(nil)
	if agent.Spec.Integration != nil && agent.Spec.Integration.GitHub != nil {
		gitRepoErr = agentv1alpha1.ValidateGitRepoURL(agent.Spec.Integration.GitHub.GitRepo)
	}

	degradedStatus := metav1.ConditionFalse
	if gitRepoErr != nil {
		newPhase = "Degraded"
		condStatus = metav1.ConditionFalse
		condReason = "InvalidGitRepoURL"
		condMsg = fmt.Sprintf("Invalid gitRepo URL (%s); GitOps disabled in SETTINGS.md", gitRepoErr.Error())
		degradedStatus = metav1.ConditionTrue
	}

	// Cluster event ingestion, reported only while it is switched off. A
	// permanently-present condition would have to read True on every healthy
	// install, and True here could only ever mean "the operator asked for a
	// watcher" — it is not a liveness check, and a watcher that dies leaves the
	// pod Ready with nothing to show for it. Claiming otherwise on every CR is
	// worse than saying nothing, so the condition exists only in the state that
	// is genuinely worth reporting: somebody pressed the emergency stop.
	eventWatcherOn := eventWatcherEnabled(agent)
	existingWatcherCond := meta.FindStatusCondition(agent.Status.Conditions, eventWatcherConditionType)
	// Message is compared alongside Status and Reason, as the Ready and Degraded
	// terms below do. Reason is a constant here, so the only way the text can
	// differ is a release that rewords eventWatcherDisabledMessage — and that
	// message is the recovery instruction a reader gets from `kubectl describe`.
	// Leaving it out would freeze the previous release's wording on every
	// install still holding the stop, since nothing else about them changes.
	eventWatcherUnchanged := (eventWatcherOn && existingWatcherCond == nil) ||
		(!eventWatcherOn && existingWatcherCond != nil && existingWatcherCond.Status == metav1.ConditionFalse &&
			existingWatcherCond.Reason == eventWatcherDisabledReason && existingWatcherCond.Message == eventWatcherDisabledMessage)

	existingCond := meta.FindStatusCondition(agent.Status.Conditions, "Ready")
	existingDegradedCond := meta.FindStatusCondition(agent.Status.Conditions, "Degraded")
	degradedUnchanged := (degradedStatus == metav1.ConditionFalse && existingDegradedCond == nil) ||
		(degradedStatus == metav1.ConditionTrue && existingDegradedCond != nil && existingDegradedCond.Status == metav1.ConditionTrue && existingDegradedCond.Reason == "InvalidGitRepoURL" && existingDegradedCond.Message == condMsg)

	// Check if anything actually changed
	if agent.Status.Phase == newPhase &&
		agent.Status.DeploymentStatus.Name == newDeploymentStatusName &&
		agent.Status.DeploymentStatus.ReadyReplicas == newDeploymentStatusReadyReplicas &&
		agent.Status.StorageStatus.Bound == newStorageStatusBound &&
		agent.Status.ServiceStatus.Endpoint == newServiceStatusEndpoint &&
		agent.Status.Address == newAddress &&
		agent.Status.Telemetry.OTLPEndpoint == otlpEndpoint &&
		agent.Status.Telemetry.OTLPEndpointSource == otlpSource &&
		networkPolicyStatusUnchanged(agent.Status.NetworkPolicy, netpolProfile) &&
		degradedUnchanged &&
		eventWatcherUnchanged &&
		existingCond != nil && existingCond.Status == condStatus && existingCond.Reason == condReason && existingCond.Message == condMsg {
		return newPhase, nil
	}

	// Apply updates
	agent.Status.Phase = newPhase
	agent.Status.DeploymentStatus.Name = newDeploymentStatusName
	agent.Status.DeploymentStatus.ReadyReplicas = newDeploymentStatusReadyReplicas
	agent.Status.StorageStatus.Bound = newStorageStatusBound
	agent.Status.ServiceStatus.Endpoint = newServiceStatusEndpoint
	agent.Status.Address = newAddress
	agent.Status.Telemetry.OTLPEndpoint = otlpEndpoint
	agent.Status.Telemetry.OTLPEndpointSource = otlpSource
	agent.Status.NetworkPolicy.Generated = netpolProfile.Generated
	agent.Status.NetworkPolicy.DNSClusterIPs = append([]string(nil), netpolProfile.DNSClusterIPs...)
	agent.Status.NetworkPolicy.DNSClusterIPsSource = netpolProfile.DNSSource
	agent.Status.NetworkPolicy.MetadataDaemonIP = netpolProfile.MetadataDaemonIP
	agent.Status.NetworkPolicy.MetadataDaemonPort = netpolProfile.MetadataDaemonPort
	agent.Status.NetworkPolicy.MetadataDaemonIPSource = netpolProfile.MetadataDaemonSource

	now := metav1.Now()
	agent.Status.LastReconcileTime = &now

	condition := metav1.Condition{
		Type:               "Ready",
		Status:             condStatus,
		Reason:             condReason,
		Message:            condMsg,
		LastTransitionTime: now,
	}
	meta.SetStatusCondition(&agent.Status.Conditions, condition)

	if degradedStatus == metav1.ConditionTrue {
		degradedCond := metav1.Condition{
			Type:               "Degraded",
			Status:             metav1.ConditionTrue,
			Reason:             "InvalidGitRepoURL",
			Message:            condMsg,
			LastTransitionTime: now,
		}
		meta.SetStatusCondition(&agent.Status.Conditions, degradedCond)
	} else {
		meta.RemoveStatusCondition(&agent.Status.Conditions, "Degraded")
	}

	if eventWatcherOn {
		meta.RemoveStatusCondition(&agent.Status.Conditions, eventWatcherConditionType)
	} else {
		meta.SetStatusCondition(&agent.Status.Conditions, metav1.Condition{
			Type:               eventWatcherConditionType,
			Status:             metav1.ConditionFalse,
			Reason:             eventWatcherDisabledReason,
			Message:            eventWatcherDisabledMessage,
			LastTransitionTime: now,
		})
	}

	return newPhase, r.Status().Update(ctx, agent)
}

func networkPolicyStatusUnchanged(status agentv1alpha1.NetworkPolicyStatus, profile netpolProfile) bool {
	if status.Generated != profile.Generated {
		return false
	}
	if status.DNSClusterIPsSource != profile.DNSSource {
		return false
	}
	if status.MetadataDaemonIP != profile.MetadataDaemonIP {
		return false
	}
	if status.MetadataDaemonPort != profile.MetadataDaemonPort {
		return false
	}
	if status.MetadataDaemonIPSource != profile.MetadataDaemonSource {
		return false
	}
	if len(status.DNSClusterIPs) != len(profile.DNSClusterIPs) {
		return false
	}
	for i := range status.DNSClusterIPs {
		if status.DNSClusterIPs[i] != profile.DNSClusterIPs[i] {
			return false
		}
	}
	return true
}

func (r *PlatformAgentReconciler) getDeploymentStatusDetails(ctx context.Context, agent *agentv1alpha1.PlatformAgent) (phase string, reason string, message string) {
	phase = "Provisioning"
	reason = "Provisioning"
	message = "Waiting for deployment replicas to be ready"

	podList := &corev1.PodList{}
	err := r.List(ctx, podList, client.InNamespace(agent.Namespace), client.MatchingLabels{"app": agent.Name + "-gateway"})
	if err != nil || len(podList.Items) == 0 {
		return phase, reason, message
	}

	for _, pod := range podList.Items {
		// 1. Check container waiting states (CrashLoopBackOff, ImagePullBackOff, ErrImagePull, etc.)
		//
		// Init statuses first, and PodInitializing filtered out with
		// ContainerCreating. Both were measured on a cluster rather than reasoned
		// about, because the obvious theory is wrong: while a pod is stuck in Init
		// the kubelet does populate ContainerStatuses -- every app container sits
		// there waiting with reason PodInitializing. So the old code did find a
		// waiting container and did report Degraded. What it reported was
		// "PodInitializing", which names no fault and points at a container that is
		// only waiting its turn, while the init container that actually failed to
		// pull went unmentioned. The credential proxy is a native sidecar now, so
		// the container that strands the pod is usually in the init list.
		//
		// Scanning init first and skipping the two placeholder reasons gets the
		// reason an operator can act on: ImagePullBackOff on the container that has
		// it, rather than PodInitializing on one that does not.
		initThenApp := make([]corev1.ContainerStatus, 0, len(pod.Status.InitContainerStatuses)+len(pod.Status.ContainerStatuses))
		initThenApp = append(initThenApp, pod.Status.InitContainerStatuses...)
		initThenApp = append(initThenApp, pod.Status.ContainerStatuses...)
		for _, cs := range initThenApp {
			if cs.State.Waiting != nil && cs.State.Waiting.Reason != "" &&
				cs.State.Waiting.Reason != "ContainerCreating" && cs.State.Waiting.Reason != "PodInitializing" {
				phase = "Degraded"
				reason = cs.State.Waiting.Reason
				message = fmt.Sprintf("Container '%s' in pod %s is waiting: %s - %s", cs.Name, pod.Name, cs.State.Waiting.Reason, cs.State.Waiting.Message)
				return phase, reason, message
			}
		}

		// 2. Check pod scheduling conditions (Unschedulable due to node selector/affinity/gVisor)
		for _, cond := range pod.Status.Conditions {
			if cond.Type == corev1.PodScheduled && cond.Status == corev1.ConditionFalse && cond.Reason == "Unschedulable" {
				phase = "Degraded"
				reason = "PodUnschedulable"
				if requested := requestedRuntimeClasses(agent); len(requested) > 0 {
					// Plural only when the CR really does name two, which takes
					// the agent pod and the sandbox having deliberately been
					// given different runtimes. Every other install reads the
					// sentence this condition has always produced.
					noun := "RuntimeClass"
					if len(requested) > 1 {
						noun = "RuntimeClasses"
					}
					quoted := make([]string, 0, len(requested))
					for _, name := range requested {
						quoted = append(quoted, fmt.Sprintf("'%s'", name))
					}
					message = fmt.Sprintf("Pod %s is waiting to be scheduled because no nodes in the cluster match the requested %s %s. For GKE Standard, enable GKE Sandbox by provisioning a gVisor node pool.", pod.Name, noun, strings.Join(quoted, ", "))
				} else {
					cleanMsg := strings.TrimSuffix(strings.TrimSpace(cond.Message), ".")
					message = fmt.Sprintf("Pod %s cannot be scheduled onto any available node: %s.", pod.Name, cleanMsg)
				}
				return phase, reason, message
			}
		}
	}

	return phase, reason, message
}

// validateRuntimeClass returns the name it could not resolve alongside the
// error, because the caller's Degraded message names it and the error alone
// does not carry it back.
func (r *PlatformAgentReconciler) validateRuntimeClass(ctx context.Context, agent *agentv1alpha1.PlatformAgent) (string, error) {
	for _, rcName := range requestedRuntimeClasses(agent) {
		rc := &nodev1.RuntimeClass{}
		if err := r.Get(ctx, types.NamespacedName{Name: rcName}, rc); err != nil {
			return rcName, err
		}
	}
	return "", nil
}

// requestedRuntimeClasses is every RuntimeClass this CR asks for, deduplicated.
//
// Two pods can name one now — the agent's, and the sandbox's, which is a
// separate field because the two workloads do not want the same runtime. Both
// are checked here rather than each at its own builder because the failure is
// the same failure and the operator already has one message for it: a
// RuntimeClass that does not exist leaves the pod Pending with nothing in the CR
// that explains why, and that is worth catching before either object is applied.
func requestedRuntimeClasses(agent *agentv1alpha1.PlatformAgent) []string {
	var names []string
	add := func(name *string) {
		if name == nil || *name == "" {
			return
		}
		if slices.Contains(names, *name) {
			return
		}
		names = append(names, *name)
	}
	if agent.Spec.Deployment != nil && agent.Spec.Deployment.Availability != nil {
		add(agent.Spec.Deployment.Availability.RuntimeClassName)
	}
	if shellSandboxEnabled(agent) {
		add(shellSandboxRuntimeClassName(agent))
	}
	return names
}

func (r *PlatformAgentReconciler) updateStatusDegraded(ctx context.Context, agent *agentv1alpha1.PlatformAgent, reason, message string) error {
	agent.Status.Phase = "Degraded"
	now := metav1.Now()
	agent.Status.LastReconcileTime = &now

	condition := metav1.Condition{
		Type:               "Ready",
		Status:             metav1.ConditionFalse,
		Reason:             reason,
		Message:            message,
		LastTransitionTime: now,
	}
	meta.SetStatusCondition(&agent.Status.Conditions, condition)
	return r.Status().Update(ctx, agent)
}

// SetupWithManager sets up the controller with the Manager.
func (r *PlatformAgentReconciler) SetupWithManager(mgr ctrl.Manager) error {
	if r.DiscoveryClient == nil && mgr != nil && mgr.GetConfig() != nil {
		dc, err := discovery.NewDiscoveryClientForConfig(mgr.GetConfig())
		if err != nil {
			// Not fatal — the operator still reconciles agents without plugins. But the
			// ImageVolume probe fails closed, so without this client every AgentPlugin
			// goes Degraded; say why rather than leaving it to be inferred.
			logf.Log.WithName("platformagent-controller").Error(err,
				"Failed to build discovery client; ImageVolume support cannot be detected and "+
					"AgentPlugins will be reported as Degraded unless the "+
					"kubeagents.x-k8s.io/enable-image-volumes annotation is set")
		} else {
			r.DiscoveryClient = dc
		}
	}

	if r.APIReader == nil && mgr != nil {
		r.APIReader = mgr.GetAPIReader()
	}

	bld := ctrl.NewControllerManagedBy(mgr).
		For(&agentv1alpha1.PlatformAgent{}).
		Owns(&appsv1.Deployment{}).
		Owns(&appsv1.StatefulSet{}).
		Owns(&corev1.ServiceAccount{}).
		Owns(&corev1.PersistentVolumeClaim{}).
		Owns(&corev1.ConfigMap{}).
		Owns(&corev1.Service{}).
		Owns(&networkingv1.NetworkPolicy{}).
		Owns(&policyv1.PodDisruptionBudget{})

	// Only register AgentPlugin watch if CRD exists in cluster RESTMapper
	gvk := agentv1alpha1.GroupVersion.WithKind("AgentPlugin")
	if mgr != nil && mgr.GetRESTMapper() != nil {
		if _, err := mgr.GetRESTMapper().RESTMapping(gvk.GroupKind(), gvk.Version); err == nil {
			bld = bld.Watches(
				&agentv1alpha1.AgentPlugin{},
				handler.EnqueueRequestsFromMapFunc(func(ctx context.Context, obj client.Object) []reconcile.Request {
					ext, ok := obj.(*agentv1alpha1.AgentPlugin)
					if !ok {
						return nil
					}
					if ext.Spec.AgentRef != "" {
						return []reconcile.Request{
							{NamespacedName: types.NamespacedName{Namespace: ext.Namespace, Name: ext.Spec.AgentRef}},
						}
					}
					var list agentv1alpha1.PlatformAgentList
					if err := mgr.GetClient().List(ctx, &list, client.InNamespace(ext.Namespace)); err != nil {
						return nil
					}
					var reqs []reconcile.Request
					for _, agent := range list.Items {
						reqs = append(reqs, reconcile.Request{
							NamespacedName: types.NamespacedName{Namespace: agent.Namespace, Name: agent.Name},
						})
					}
					return reqs
				}),
				// Status writes on AgentPlugin come from this controller. Without a
				// generation filter each of those writes would re-enqueue the agent that
				// produced it.
				builder.WithPredicates(predicate.GenerationChangedPredicate{}),
			)
		} else {
			logf.Log.WithName("platformagent-controller").Info(
				"AgentPlugin CRD is not installed on cluster; skipping AgentPlugin watch. " +
					"Restart the operator after installing the CRD to enable plugin reconciliation.")
		}
	}

	return bld.
		Watches(
			&rbacv1.ClusterRoleBinding{},
			handler.EnqueueRequestsFromMapFunc(func(ctx context.Context, obj client.Object) []reconcile.Request {
				parts := strings.Split(obj.GetName(), ":") // format: kubeagents:<role>:<namespace>:<name>
				if len(parts) == 4 && parts[0] == "kubeagents" {
					return []reconcile.Request{{NamespacedName: types.NamespacedName{Namespace: parts[2], Name: parts[3]}}}
				}
				return nil
			}),
		).
		Watches(
			&rbacv1.ClusterRole{},
			handler.EnqueueRequestsFromMapFunc(func(ctx context.Context, obj client.Object) []reconcile.Request {
				parts := strings.Split(obj.GetName(), ":") // format: kubeagents:<role>:<namespace>:<name>
				if len(parts) == 4 && parts[0] == "kubeagents" {
					return []reconcile.Request{{NamespacedName: types.NamespacedName{Namespace: parts[2], Name: parts[3]}}}
				}
				return nil
			}),
		).
		Watches(
			&rbacv1.RoleBinding{},
			handler.EnqueueRequestsFromMapFunc(func(ctx context.Context, obj client.Object) []reconcile.Request {
				parts := strings.Split(obj.GetName(), ":") // format: kubeagents:<role>:<namespace>:<name>
				if len(parts) == 4 && parts[0] == "kubeagents" {
					return []reconcile.Request{{NamespacedName: types.NamespacedName{Namespace: parts[2], Name: parts[3]}}}
				}
				return nil
			}),
		).
		Watches(
			&rbacv1.Role{},
			handler.EnqueueRequestsFromMapFunc(func(ctx context.Context, obj client.Object) []reconcile.Request {
				parts := strings.Split(obj.GetName(), ":") // format: kubeagents:<role>:<namespace>:<name>
				if len(parts) == 4 && parts[0] == "kubeagents" {
					return []reconcile.Request{{NamespacedName: types.NamespacedName{Namespace: parts[2], Name: parts[3]}}}
				}
				return nil
			}),
		).
		Named("platformagent").
		Complete(r)
}

func isCRDNotInstalledError(err error) bool {
	if err == nil {
		return false
	}
	if meta.IsNoMatchError(err) || errors.IsNotFound(err) {
		return true
	}
	msg := err.Error()
	return strings.Contains(msg, "no matches for kind") ||
		strings.Contains(msg, "could not find the requested resource") ||
		strings.Contains(msg, "failed to get restmapping")
}

func (r *PlatformAgentReconciler) resolveAgentPlugins(ctx context.Context, agent *agentv1alpha1.PlatformAgent) ([]*agentv1alpha1.AgentPlugin, error) {
	var extList agentv1alpha1.AgentPluginList
	if err := r.List(ctx, &extList, client.InNamespace(agent.Namespace)); err != nil {
		if isCRDNotInstalledError(err) {
			logf.Log.WithName("platformagent-controller").Info("AgentPlugin CRD is not installed on cluster; skipping plugin resolution", "namespace", agent.Namespace)
			return nil, nil
		}
		return nil, err
	}

	var matching []*agentv1alpha1.AgentPlugin
	for i := range extList.Items {
		ext := &extList.Items[i]
		if ext.Spec.AgentRef == agent.Name {
			matching = append(matching, ext)
		}
	}

	slices.SortFunc(matching, func(a, b *agentv1alpha1.AgentPlugin) int {
		return strings.Compare(a.Name, b.Name)
	})

	return matching, nil
}

// isImageVolumeSupported reports whether OCI image volumes may be attached for the
// given agent.
//
// The check fails closed: if the cluster capability cannot be established, image
// volumes are treated as unsupported. Mounting an unsupported ImageVolume makes the
// API server reject the entire Deployment, which would take the agent down rather
// than merely leaving its plugins unloaded — so an unknown answer must mean "no".
// The enable-image-volumes annotation is an explicit operator override and wins over
// discovery in both directions, which is how 1.33/1.34 clusters that have the feature
// gate turned on manually opt back in.
func isImageVolumeSupported(dc discovery.DiscoveryInterface, agent *agentv1alpha1.PlatformAgent) bool {
	if agent != nil && agent.Annotations != nil {
		if val, ok := agent.Annotations["kubeagents.x-k8s.io/enable-image-volumes"]; ok {
			return strings.EqualFold(strings.TrimSpace(val), "true")
		}
	}
	supported, _ := clusterImageVolumeSupport(dc)
	return supported
}

// clusterImageVolumeSupport probes the API server for ImageVolume support.
//
// determined reports whether the answer is authoritative. When the capability cannot be
// established — no discovery client, an unreachable API server, an unparseable version —
// supported is false and determined is false: the caller must fail closed for this pass
// but must not remember the answer, because the next probe may succeed.
func clusterImageVolumeSupport(dc discovery.DiscoveryInterface) (supported bool, determined bool) {
	log := logf.Log.WithName("platformagent-controller")
	const override = "Set the kubeagents.x-k8s.io/enable-image-volumes annotation to override."

	if dc == nil {
		log.Info("No discovery client available to verify ImageVolume support; assuming unsupported. " + override)
		return false, false
	}
	ver, err := dc.ServerVersion()
	if err != nil {
		log.Error(err, "Failed to query server version to verify ImageVolume support; assuming unsupported. "+override)
		return false, false
	}

	major, errMajor := strconv.Atoi(strings.TrimRight(ver.Major, "+"))
	minorStr := strings.Split(strings.TrimRight(ver.Minor, "+"), ".")[0]
	minor, errMinor := strconv.Atoi(minorStr)
	if errMajor != nil || errMinor != nil {
		log.Info("Could not parse server version to verify ImageVolume support; assuming unsupported. "+override,
			"major", ver.Major, "minor", ver.Minor)
		return false, false
	}

	if major > 1 {
		return true, true
	}
	return major == 1 && minor >= 35, true
}

// imageVolumeSupported resolves the cluster ImageVolume capability and reuses it for
// subsequent reconciles. Only an authoritative answer is cached: a transient discovery
// failure must not pin every plugin to Degraded until the operator restarts. Per-agent
// annotation overrides are evaluated on every call, since those change without a restart.
func (r *PlatformAgentReconciler) imageVolumeSupported(agent *agentv1alpha1.PlatformAgent) bool {
	if agent != nil && agent.Annotations != nil {
		if val, ok := agent.Annotations["kubeagents.x-k8s.io/enable-image-volumes"]; ok {
			return strings.EqualFold(strings.TrimSpace(val), "true")
		}
	}

	r.imageVolumeMu.Lock()
	defer r.imageVolumeMu.Unlock()
	if r.imageVolumeResolved {
		return r.clusterImageVolumes
	}
	supported, determined := clusterImageVolumeSupport(r.DiscoveryClient)
	if determined {
		r.clusterImageVolumes = supported
		r.imageVolumeResolved = true
	}
	return supported
}

// evaluatePluginReadiness decides a plugin's Ready condition. imageFailure is the
// kubelet's message when the agent pod cannot pull this plugin's image, empty otherwise.
func evaluatePluginReadiness(
	agent *agentv1alpha1.PlatformAgent,
	plugin *agentv1alpha1.AgentPlugin,
	imageVolumeSupported bool,
	duplicate bool,
	imageFailure string,
) (phase string, condition metav1.Condition) {
	degraded := func(reason, message string) (string, metav1.Condition) {
		return "Degraded", metav1.Condition{
			Type: "Ready", Status: metav1.ConditionFalse, Reason: reason, Message: message,
		}
	}

	switch {
	case !isValidPluginName(plugin.Name):
		return degraded("InvalidPluginName", fmt.Sprintf(
			"Plugin name '%s' must start with a lowercase letter and contain only lowercase letters and digits (max 56 characters).",
			plugin.Name))
	case duplicate:
		return degraded("DuplicatePluginName", fmt.Sprintf(
			"Plugin name '%s' collides with built-in or already registered plugin.", plugin.Name))
	case !imageVolumeSupported:
		return degraded("ImageVolumeUnsupported", fmt.Sprintf(
			"Kubernetes version does not support ImageVolumeSource (requires 1.35+). OCI volume for agent %s was not mounted.",
			agent.Name))
	case imageFailure != "":
		// The image volume is part of the agent's pod spec, so an unpullable plugin
		// image keeps the whole agent pod from starting. Reporting Ready here would
		// point whoever is debugging the outage away from its actual cause.
		return degraded("ImagePullFailed", fmt.Sprintf(
			"Plugin image '%s' could not be pulled, which is blocking agent %s from starting: %s",
			plugin.Spec.Image, agent.Name, imageFailure))
	}

	message := fmt.Sprintf("Plugin successfully applied to agent %s.", agent.Name)
	if issues := pluginConfigIssues(plugin); len(issues) > 0 {
		message = fmt.Sprintf("%s %s", message, strings.Join(issues, " "))
	}
	return "Ready", metav1.Condition{
		Type: "Ready", Status: metav1.ConditionTrue, Reason: "Applied", Message: message,
	}
}

func (r *PlatformAgentReconciler) updatePluginStatuses(ctx context.Context, agent *agentv1alpha1.PlatformAgent, plugins []*agentv1alpha1.AgentPlugin, imageVolumeSupported bool) {
	now := metav1.Now()
	seenNames := make(map[string]bool)
	imageFailures := r.detectPluginImageFailures(ctx, agent, plugins)

	for _, plugin := range plugins {
		original := plugin.DeepCopy()
		patch := client.MergeFrom(original)
		if !slices.Contains(plugin.Status.TargetAgents, agent.Name) {
			plugin.Status.TargetAgents = append(plugin.Status.TargetAgents, agent.Name)
		}
		plugin.Status.ObservedGeneration = plugin.Generation

		normName := normalizePluginName(plugin.Name)
		duplicate := IsBuiltInPlugin(plugin.Name) || seenNames[normName]
		seenNames[normName] = true

		phase, condition := evaluatePluginReadiness(agent, plugin, imageVolumeSupported, duplicate, imageFailures[plugin.Name])
		condition.LastTransitionTime = now
		plugin.Status.Phase = phase
		meta.SetStatusCondition(&plugin.Status.Conditions, condition)

		// Only write when something other than the timestamp actually moved. Stamping
		// LastUpdated on every pass would make each reconcile issue a PATCH, and each
		// PATCH re-enqueue the agent through the AgentPlugin watch. Logging is gated on
		// the same check: a standing misconfiguration is already reported in status, so
		// repeating it every reconcile is noise, not signal.
		if pluginStatusEqual(&original.Status, &plugin.Status) {
			continue
		}
		plugin.Status.LastUpdated = &now
		logPluginCondition(plugin, condition)

		if err := r.Status().Patch(ctx, plugin, patch); err != nil {
			logf.Log.WithName("platformagent-controller").Error(err, "Failed to update AgentPlugin status", "plugin", plugin.Name)
		}
	}
}

// logPluginCondition emits one log line per genuine status transition.
func logPluginCondition(plugin *agentv1alpha1.AgentPlugin, condition metav1.Condition) {
	log := logf.Log.WithName("platformagent-controller")
	if condition.Status == metav1.ConditionTrue {
		// Surfaced as an error so operators notice keys silently dropped from config.yaml.
		for _, issue := range pluginConfigIssues(plugin) {
			log.Error(fmt.Errorf("%s", issue), "ignoring plugin config key outside allowed subtrees",
				"plugin", plugin.Name)
		}
		log.Info("AgentPlugin ready", "plugin", plugin.Name, "message", condition.Message)
		return
	}
	log.Error(fmt.Errorf("%s", condition.Message), "AgentPlugin degraded",
		"plugin", plugin.Name, "reason", condition.Reason)
}

// detectPluginImageFailures maps plugin name to the kubelet's message when the agent's
// pod cannot pull that plugin's image. The failure surfaces on the platform-agent
// container's waiting state, carrying the offending reference in its message, so a
// plugin is only blamed when its own image is named.
func (r *PlatformAgentReconciler) detectPluginImageFailures(ctx context.Context, agent *agentv1alpha1.PlatformAgent, plugins []*agentv1alpha1.AgentPlugin) map[string]string {
	failures := map[string]string{}
	if len(plugins) == 0 {
		return failures
	}

	podList := &corev1.PodList{}
	if err := r.List(ctx, podList, client.InNamespace(agent.Namespace),
		client.MatchingLabels{"app": agent.Name + "-gateway"}); err != nil {
		return failures
	}

	for _, pod := range podList.Items {
		for _, cs := range pod.Status.ContainerStatuses {
			w := cs.State.Waiting
			if w == nil || (w.Reason != "ImagePullBackOff" && w.Reason != "ErrImagePull") {
				continue
			}
			for _, plugin := range plugins {
				if imageReferencedIn(w.Message, plugin.Spec.Image) {
					failures[plugin.Name] = w.Message
				}
			}
		}
	}
	return failures
}

// isImageRefChar reports whether b could be part of an image reference, and so whether a
// match ending or starting next to it is really a match of some longer reference.
func isImageRefChar(b byte) bool {
	switch {
	case b >= 'a' && b <= 'z', b >= 'A' && b <= 'Z', b >= '0' && b <= '9':
		return true
	case b == '.' || b == '-' || b == '_' || b == '/' || b == ':' || b == '@':
		return true
	}
	return false
}

// imageReferencedIn reports whether message names exactly this image.
//
// A plain substring test is wrong here: "repo/x:v1" occurs inside "repo/x:v10", so a
// failure on one tag would be blamed on a sibling plugin using another. Requiring a
// non-reference character on both sides — kubelet quotes the reference — keeps the match
// to whole references without depending on one exact message format.
func imageReferencedIn(message, image string) bool {
	if image == "" {
		return false
	}
	for offset := 0; offset <= len(message)-len(image); {
		idx := strings.Index(message[offset:], image)
		if idx < 0 {
			return false
		}
		start := offset + idx
		end := start + len(image)
		startOK := start == 0 || !isImageRefChar(message[start-1])
		endOK := end == len(message) || !isImageRefChar(message[end])
		if startOK && endOK {
			return true
		}
		offset = start + 1
	}
	return false
}

// markOrphanedPlugins reports plugins whose agentRef names a PlatformAgent that does not
// exist. Nothing else reconciles them — the resolver only ever sees plugins that match an
// existing agent — so without this a typo in agentRef leaves the plugin permanently
// statusless, with no indication that it will never be applied.
func (r *PlatformAgentReconciler) markOrphanedPlugins(ctx context.Context, namespace, agentName string) {
	var list agentv1alpha1.AgentPluginList
	if err := r.List(ctx, &list, client.InNamespace(namespace)); err != nil {
		if !isCRDNotInstalledError(err) {
			logf.Log.WithName("platformagent-controller").Error(err,
				"Failed to list AgentPlugins while checking for orphans", "namespace", namespace)
		}
		return
	}

	now := metav1.Now()
	for i := range list.Items {
		plugin := &list.Items[i]
		if plugin.Spec.AgentRef != agentName {
			continue
		}

		original := plugin.DeepCopy()
		patch := client.MergeFrom(original)
		plugin.Status.ObservedGeneration = plugin.Generation
		plugin.Status.TargetAgents = nil
		plugin.Status.Phase = "Degraded"
		condition := metav1.Condition{
			Type:               "Ready",
			Status:             metav1.ConditionFalse,
			Reason:             "AgentNotFound",
			Message:            fmt.Sprintf("No PlatformAgent named '%s' exists in namespace '%s'; this plugin is not applied to any agent.", agentName, namespace),
			LastTransitionTime: now,
		}
		meta.SetStatusCondition(&plugin.Status.Conditions, condition)

		if pluginStatusEqual(&original.Status, &plugin.Status) {
			continue
		}
		plugin.Status.LastUpdated = &now
		logPluginCondition(plugin, condition)

		if err := r.Status().Patch(ctx, plugin, patch); err != nil {
			logf.Log.WithName("platformagent-controller").Error(err,
				"Failed to update orphaned AgentPlugin status", "plugin", plugin.Name)
		}
	}
}

// pluginStatusEqual compares two AgentPlugin statuses while ignoring LastUpdated and
// condition timestamps, so that a re-reconcile that reaches the same conclusion is not
// mistaken for a change.
func pluginStatusEqual(a, b *agentv1alpha1.AgentPluginStatus) bool {
	if a.Phase != b.Phase ||
		a.ObservedGeneration != b.ObservedGeneration ||
		!slices.Equal(a.TargetAgents, b.TargetAgents) ||
		len(a.Conditions) != len(b.Conditions) {
		return false
	}
	for i := range a.Conditions {
		ac, bc := a.Conditions[i], b.Conditions[i]
		if ac.Type != bc.Type || ac.Status != bc.Status ||
			ac.Reason != bc.Reason || ac.Message != bc.Message ||
			ac.ObservedGeneration != bc.ObservedGeneration {
			return false
		}
	}
	return true
}
