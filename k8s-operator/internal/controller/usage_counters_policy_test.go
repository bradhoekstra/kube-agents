// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

package controller

import (
	"testing"

	networkingv1 "k8s.io/api/networking/v1"
	"k8s.io/apimachinery/pkg/api/equality"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

const policyTestOperatorNamespace = "operator-ns"

// policyTestMalformedNamespace is not a valid label value: the API server would
// reject a NetworkPolicy that carried it in a selector.
const policyTestMalformedNamespace = "Not A Valid NS!"

// policyTestNonDNS1123Namespace is a valid label value but not a DNS-1123 label
// (uppercase). The selector would carry it, but the API server only ever sets
// kubernetes.io/metadata.name to a namespace's own DNS-1123 name, so it matches
// no namespace on any cluster.
const policyTestNonDNS1123Namespace = "Kubeagents-System"

// operatorPeerRules is every ingress rule of np whose peer is the operator's
// pods, keyed by the ports it opens.
func operatorPeerRules(np *networkingv1.NetworkPolicy) map[int32]networkingv1.NetworkPolicyPeer {
	found := map[int32]networkingv1.NetworkPolicyPeer{}
	for _, rule := range np.Spec.Ingress {
		for _, peer := range rule.From {
			if peer.PodSelector != nil && peer.PodSelector.MatchLabels[operatorPodNameLabel] == operatorPodNameValue {
				for _, port := range rule.Ports {
					found[port.Port.IntVal] = peer
				}
			}
		}
	}
	return found
}

// operatorRules is every ingress rule of np that admits a peer carrying the
// operator's pod label -- the whole rule, so a test can see what else the rule
// opens beside that peer (a second peer, an empty or wider Ports list), which
// operatorPeerRules drops.
func operatorRules(np *networkingv1.NetworkPolicy) []networkingv1.NetworkPolicyIngressRule {
	var rules []networkingv1.NetworkPolicyIngressRule
	for _, rule := range np.Spec.Ingress {
		for _, peer := range rule.From {
			if peer.PodSelector != nil && peer.PodSelector.MatchLabels[operatorPodNameLabel] == operatorPodNameValue {
				rules = append(rules, rule)
				break
			}
		}
	}
	return rules
}

// With the operator's namespace known, the gateway and broker policies each
// gain one rule admitting the operator's pods, selected by namespace and pod
// label, on the metrics port and no other; without it, neither does.
func TestThePoliciesAdmitTheOperatorOnTheMetricsPortsOnly(t *testing.T) {
	agent := &agentv1alpha1.PlatformAgent{ObjectMeta: metav1.ObjectMeta{Name: "agent", Namespace: "agents"}}
	profile := defaultTestNetpolProfile()
	profile.OperatorNamespace = policyTestOperatorNamespace

	gateway := operatorPeerRules(buildNetworkPolicy(agent, nil, profile, false, "", false))
	broker := operatorPeerRules(credentialProxyNetworkPolicyWithOperatorPeer(agent, policyTestOperatorNamespace))
	if len(gateway) != 1 || len(broker) != 1 {
		t.Fatalf("operator peer rules: gateway on ports %v, broker on ports %v; want one each", gateway, broker)
	}
	for name, got := range map[string]map[int32]networkingv1.NetworkPolicyPeer{"gateway": gateway, "broker": broker} {
		port := eventWatcherMetricsPort
		if name == "broker" {
			port = credentialProxyMetricsPort
		}
		peer, ok := got[port]
		if !ok {
			t.Fatalf("%s policy: the operator rule is not on the metrics port %d: %v", name, port, got)
		}
		if peer.NamespaceSelector == nil || peer.NamespaceSelector.MatchLabels[labelMetadataName] != policyTestOperatorNamespace {
			t.Errorf("%s policy: the operator peer is not narrowed to the operator's namespace: %+v", name, peer)
		}
	}

	if n := len(operatorPeerRules(buildNetworkPolicy(agent, nil, defaultTestNetpolProfile(), false, "", false))); n != 0 {
		t.Errorf("the gateway policy renders %d operator rule(s) with no namespace, want 0", n)
	}
	if n := len(operatorPeerRules(credentialProxyNetworkPolicyWithOperatorPeer(agent, ""))); n != 0 {
		t.Errorf("the broker policy renders %d operator rule(s) with no namespace, want 0", n)
	}
	// A malformed namespace is treated like an unknown one: written into a
	// selector it would have the API server reject the whole policy, so neither
	// policy renders the operator rule.
	malformed := defaultTestNetpolProfile()
	malformed.OperatorNamespace = policyTestMalformedNamespace
	if n := len(operatorPeerRules(buildNetworkPolicy(agent, nil, malformed, false, "", false))); n != 0 {
		t.Errorf("the gateway policy renders %d operator rule(s) with a malformed namespace, want 0", n)
	}
	if n := len(operatorPeerRules(credentialProxyNetworkPolicyWithOperatorPeer(agent, policyTestMalformedNamespace))); n != 0 {
		t.Errorf("the broker policy renders %d operator rule(s) with a malformed namespace, want 0", n)
	}
	// A namespace that is a valid label value but not a DNS-1123 label is treated
	// the same: the selector would carry it, but it matches no namespace, so
	// neither policy renders the operator rule.
	nonDNS := defaultTestNetpolProfile()
	nonDNS.OperatorNamespace = policyTestNonDNS1123Namespace
	if n := len(operatorPeerRules(buildNetworkPolicy(agent, nil, nonDNS, false, "", false))); n != 0 {
		t.Errorf("the gateway policy renders %d operator rule(s) with a non-DNS-1123 namespace, want 0", n)
	}
	if n := len(operatorPeerRules(credentialProxyNetworkPolicyWithOperatorPeer(agent, policyTestNonDNS1123Namespace))); n != 0 {
		t.Errorf("the broker policy renders %d operator rule(s) with a non-DNS-1123 namespace, want 0", n)
	}
	// The operator rule admits nothing besides the operator peer on the metrics
	// port: exactly one rule, equal to operatorMetricsIngressRule's output. A
	// second peer in the rule's From, or an empty or wider Ports list, passes the
	// port-and-selector checks above (operatorPeerRules keeps only the labelled
	// peer and nothing when Ports is empty) but is caught here.
	for name, np := range map[string]*networkingv1.NetworkPolicy{
		"gateway": buildNetworkPolicy(agent, nil, profile, false, "", false),
		"broker":  credentialProxyNetworkPolicyWithOperatorPeer(agent, policyTestOperatorNamespace),
	} {
		port := eventWatcherMetricsPort
		if name == "broker" {
			port = credentialProxyMetricsPort
		}
		want, ok := operatorMetricsIngressRule(policyTestOperatorNamespace, port)
		if !ok {
			t.Fatalf("operatorMetricsIngressRule returned no rule for a valid namespace")
		}
		rules := operatorRules(np)
		if len(rules) != 1 {
			t.Fatalf("%s policy: %d rules admit the operator peer, want exactly 1: %+v", name, len(rules), rules)
		}
		if !equality.Semantic.DeepEqual(rules[0], want) {
			t.Errorf("%s policy: the operator rule admits more than the operator peer on the metrics port:\n got %+v\nwant %+v", name, rules[0], want)
		}
	}
	// The builder itself is unchanged: no operator rule, whatever the caller knows.
	if n := len(operatorPeerRules(buildCredentialProxyNetworkPolicy(agent))); n != 0 {
		t.Errorf("buildCredentialProxyNetworkPolicy renders %d operator rule(s), want 0", n)
	}
	// What the reconcile applies is the builder's policy plus exactly that one
	// rule, so the tests that guard the broker's boundary through the builder
	// still describe the applied policy up to the operator peer.
	built := buildCredentialProxyNetworkPolicy(agent)
	applied := credentialProxyNetworkPolicyWithOperatorPeer(agent, policyTestOperatorNamespace)
	if len(applied.Spec.Ingress) != len(built.Spec.Ingress)+1 {
		t.Fatalf("the applied broker policy has %d ingress rules, the builder's %d; want exactly one more", len(applied.Spec.Ingress), len(built.Spec.Ingress))
	}
	for i := range built.Spec.Ingress {
		if !equality.Semantic.DeepEqual(built.Spec.Ingress[i], applied.Spec.Ingress[i]) {
			t.Errorf("ingress rule %d differs between the builder's policy and the applied one", i)
		}
	}
	if !equality.Semantic.DeepEqual(built.Spec.PodSelector, applied.Spec.PodSelector) || !equality.Semantic.DeepEqual(built.Spec.PolicyTypes, applied.Spec.PolicyTypes) {
		t.Error("the applied broker policy differs from the builder's beyond the appended rule")
	}
}
