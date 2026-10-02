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
	"context"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// scrapeTestFleetClusters and scrapeTestFleetNamespaces size the fleet-sized
// body: clusters times namespaces times reasons, per family, is the product
// the design says cannot be bounded, and the body is folded within a memory
// bound of one line whatever its size.
const (
	scrapeTestFleetClusters   = 40
	scrapeTestFleetNamespaces = 30
	scrapeTestStartTime       = 1759400000.25
)

var scrapeTestReasons = []string{"BackOff", "Failed", "Unhealthy", "FailedScheduling", "OOMKilling"}

// fleetBody renders a watcher exposition the size of a fleet: every family
// the watcher exports over the label product, with the injected family
// summing to a known value and the observed family far larger.
func fleetBody(t *testing.T) (body string, injected int64) {
	t.Helper()
	var b strings.Builder
	b.WriteString("# HELP k8s_event_watcher_events_seen_total Total k8s events observed by the informer, before filter.\n# TYPE k8s_event_watcher_events_seen_total counter\n")
	for c := 0; c < scrapeTestFleetClusters; c++ {
		for _, reason := range scrapeTestReasons {
			fmt.Fprintf(&b, "k8s_event_watcher_events_seen_total{cluster=\"c%d\",location=\"us-central1\",project=\"p\",reason=%q} %d\n", c, reason, 100000+c)
		}
	}
	for _, family := range []string{"k8s_event_watcher_events_deduped_total", "k8s_event_watcher_events_policy_filtered_total", "k8s_event_watcher_events_injected_total"} {
		fmt.Fprintf(&b, "# TYPE %s counter\n", family)
		for c := 0; c < scrapeTestFleetClusters; c++ {
			for n := 0; n < scrapeTestFleetNamespaces; n++ {
				for _, reason := range scrapeTestReasons {
					value := int64(c + n + 1)
					fmt.Fprintf(&b, "%s{cluster=\"c%d\",location=\"us-central1\",namespace=\"ns%d\",project=\"p\",reason=%q} %d\n", family, c, n, reason, value)
					if family == eventsInjectedSeries {
						injected += value
					}
				}
			}
		}
	}
	fmt.Fprintf(&b, "# TYPE %s gauge\n%s %v\n", processStartTimeSeries, processStartTimeSeries, scrapeTestStartTime)
	return b.String(), injected
}

func serveBody(t *testing.T, status int, body string) string {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(status)
		_, _ = w.Write([]byte(body))
	}))
	t.Cleanup(srv.Close)
	return strings.TrimPrefix(srv.URL, "http://")
}

func scrapeKind(t *testing.T, err error) string {
	t.Helper()
	var scrape *usageScrapeError
	if !errors.As(err, &scrape) {
		t.Fatalf("error %v is not a usageScrapeError", err)
	}
	return scrape.Kind
}

// A fleet-sized body, of the wanted family and of other families alike, is
// summed correctly: the injected series over every label set and not the
// observed one, with the start time read off the gauge.
func TestPodUsageSource_FoldsAFleetSizedBody(t *testing.T) {
	body, injected := fleetBody(t)
	addr := serveBody(t, http.StatusOK, body)
	reading, err := newPodUsageSource().Scrape(context.Background(), addr, usageCounterEventsIngested)
	if err != nil {
		t.Fatalf("Scrape: %v", err)
	}
	if reading.Sample != injected {
		t.Errorf("sample = %d, want the injected sum %d", reading.Sample, injected)
	}
	if reading.StartTime == nil || *reading.StartTime != scrapeTestStartTime {
		t.Errorf("start time = %v, want %v", reading.StartTime, scrapeTestStartTime)
	}
}

// The broker's series: status values success and error summed over tool and
// subcommand; blocked, busy and abandoned excluded.
func TestPodUsageSource_SumsTheBrokersCountedStatuses(t *testing.T) {
	body := strings.Join([]string{
		"# HELP kubeagents_tool_invocations_total CLI tool executions brokered, by tool, subcommand and outcome.",
		"# TYPE kubeagents_tool_invocations_total counter",
		`kubeagents_tool_invocations_total{tool="kubectl",subcommand="get",status="success"} 40`,
		`kubeagents_tool_invocations_total{tool="kubectl",subcommand="get",status="error"} 2`,
		`kubeagents_tool_invocations_total{tool="kubectl",subcommand="delete",status="blocked"} 7`,
		`kubeagents_tool_invocations_total{tool="gcloud",subcommand="other",status="busy"} 3`,
		`kubeagents_tool_invocations_total{tool="gcloud",subcommand="other",status="abandoned"} 1`,
		`kubeagents_tool_invocations_total{tool="gcloud",subcommand="other",status="success"} 5`,
		"# TYPE kubeagents_tool_execution_duration_seconds histogram",
		`kubeagents_tool_execution_duration_seconds_bucket{tool="kubectl",le="+Inf"} 42`,
		`kubeagents_tool_execution_duration_seconds_sum{tool="kubectl"} 12.5`,
		`kubeagents_tool_execution_duration_seconds_count{tool="kubectl"} 42`,
		`kubeagents_credential_proxy_requests_total{endpoint="/v1/exec",status_code="200"} 42`,
		"",
	}, "\n")
	addr := serveBody(t, http.StatusOK, body)
	reading, err := newPodUsageSource().Scrape(context.Background(), addr, usageCounterToolExecutions)
	if err != nil {
		t.Fatalf("Scrape: %v", err)
	}
	if reading.Sample != 47 {
		t.Errorf("sample = %d, want 47 (success and error only)", reading.Sample)
	}
	if reading.StartTime != nil {
		t.Errorf("a body without the gauge yielded a start time %v", *reading.StartTime)
	}
}

// Each scrape that produces no body to count: a connection that failed, a
// 3xx answer (not followed), a line past its bound, a wanted line expfmt
// cannot parse, a sample that is negative or not finite.
func TestPodUsageSource_FailedScrapes(t *testing.T) {
	longLine := eventsInjectedSeries + `{cluster="c",namespace="` + strings.Repeat("n", usageScrapeMaxLineBytes) + `"} 1` + "\n"
	cases := []struct {
		name   string
		status int
		body   string
		kind   string
	}{
		{"a redirect", http.StatusFound, "", usageScrapeKindStatus},
		{"a server error", http.StatusInternalServerError, "", usageScrapeKindStatus},
		{"a line past the bound", http.StatusOK, longLine, usageScrapeKindLine},
		{"a wanted line that does not parse", http.StatusOK, eventsInjectedSeries + `{cluster="c"} not-a-number` + "\n", usageScrapeKindParse},
		{"a negative sample", http.StatusOK, eventsInjectedSeries + `{cluster="c"} -3` + "\n", usageScrapeKindSample},
		{"a NaN sample", http.StatusOK, eventsInjectedSeries + `{cluster="c"} NaN` + "\n", usageScrapeKindSample},
		{"an infinite sample", http.StatusOK, eventsInjectedSeries + `{cluster="c"} +Inf` + "\n", usageScrapeKindSample},
		{"a negative start time", http.StatusOK, processStartTimeSeries + " -1\n", usageScrapeKindSample},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			addr := serveBody(t, tc.status, tc.body)
			_, err := newPodUsageSource().Scrape(context.Background(), addr, usageCounterEventsIngested)
			if err == nil {
				t.Fatal("Scrape succeeded")
			}
			if got := scrapeKind(t, err); got != tc.kind {
				t.Errorf("kind = %q, want %q (%v)", got, tc.kind, err)
			}
		})
	}
	t.Run("a connection that failed", func(t *testing.T) {
		// Port 1 has no listener, so the connection is refused.
		_, err := newPodUsageSource().Scrape(context.Background(), "127.0.0.1:1", usageCounterEventsIngested)
		if err == nil || scrapeKind(t, err) != usageScrapeKindConnect {
			t.Errorf("a refused connection: %v, want kind %q", err, usageScrapeKindConnect)
		}
	})
}

// A redirect is not followed: the target that would answer 200 with a huge
// sample is never reached.
func TestPodUsageSource_DoesNotFollowRedirects(t *testing.T) {
	reached := false
	target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		reached = true
		_, _ = fmt.Fprintf(w, "%s 1000000\n", eventsInjectedSeries)
	}))
	t.Cleanup(target.Close)
	redirecting := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, target.URL+usageMetricsPath, http.StatusFound)
	}))
	t.Cleanup(redirecting.Close)
	_, err := newPodUsageSource().Scrape(context.Background(), strings.TrimPrefix(redirecting.URL, "http://"), usageCounterEventsIngested)
	if err == nil || scrapeKind(t, err) != usageScrapeKindStatus {
		t.Fatalf("redirect: err=%v, want kind %q", err, usageScrapeKindStatus)
	}
	if reached {
		t.Error("the redirect target was reached")
	}
}

// Other lines are skipped unread: a line of another family that would not
// parse is not a failed scrape, and only the wanted families are folded.
func TestPodUsageSource_SkipsOtherLinesUnread(t *testing.T) {
	body := strings.Join([]string{
		"garbage line that is not a sample",
		`k8s_event_watcher_events_seen_total{cluster="c"} not-a-number`,
		`k8s_event_watcher_events_injected_total_created{cluster="c"} 1700000000`,
		eventsInjectedSeries + `{cluster="c",namespace="a"} 3`,
		eventsInjectedSeries + "\t4",
		eventsInjectedSeries + " 5",
		"",
	}, "\n")
	addr := serveBody(t, http.StatusOK, body)
	reading, err := newPodUsageSource().Scrape(context.Background(), addr, usageCounterEventsIngested)
	if err != nil {
		t.Fatalf("Scrape: %v", err)
	}
	if reading.Sample != 12 {
		t.Errorf("sample = %d, want 12", reading.Sample)
	}
}

// The client runs on a transport with no proxy: an HTTP_PROXY in the
// operator's environment does not take the GET.
func TestPodUsageSource_IgnoresAProxyEnvironment(t *testing.T) {
	t.Setenv("HTTP_PROXY", "http://127.0.0.1:1")
	t.Setenv("http_proxy", "http://127.0.0.1:1")
	addr := serveBody(t, http.StatusOK, eventsInjectedSeries+" 9\n")
	reading, err := newPodUsageSource().Scrape(context.Background(), addr, usageCounterEventsIngested)
	if err != nil {
		t.Fatalf("Scrape under HTTP_PROXY: %v", err)
	}
	if reading.Sample != 9 {
		t.Errorf("sample = %d, want 9", reading.Sample)
	}
}

func TestUsageLineName(t *testing.T) {
	cases := map[string]string{
		"":                   "",
		"# HELP x y":         "",
		`name{a="b"} 1`:      "name",
		"name 1":             "name",
		"name\t1":            "name",
		"name_only_no_value": "",
		`kubeagents_tool_invocations_total{tool="kubectl"} 2`: "kubeagents_tool_invocations_total",
	}
	for line, want := range cases {
		if got := usageLineName(line); got != want {
			t.Errorf("usageLineName(%q) = %q, want %q", line, got, want)
		}
	}
}
