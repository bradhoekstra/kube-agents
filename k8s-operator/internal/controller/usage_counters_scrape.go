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
	"bufio"
	"context"
	"errors"
	"fmt"
	"io"
	"math"
	"net"
	"net/http"
	"strings"
	"time"

	dto "github.com/prometheus/client_model/go"
	"github.com/prometheus/common/expfmt"
	"github.com/prometheus/common/model"
)

// The scrape behind status.usage's counters: one GET of a pod's metrics
// listener over the pod network, read line by line and kept nowhere. The port
// is held by a listener in a pod that runs other containers, so what answers
// is input, not the operator's own data; every bound here is sized on that.

const (
	// usageScrapeTimeout bounds one scrape from connect to the last byte.
	usageScrapeTimeout = 15 * time.Second
	// usageScrapeMaxLineBytes is the one bound on a body: a line past it is a
	// failed scrape. Far above any line either listener writes, a name, five
	// short labels and a number, and the only memory a scrape holds, since
	// the body is folded as it is read. A bound on the number of lines would
	// be a bound on the watcher family's cardinality, which grows with the
	// fleet for the life of the process and cannot be sized.
	usageScrapeMaxLineBytes = 64 * 1024
	// usageScrapeLineBuffer is the scanner's initial buffer, grown up to the
	// line bound as needed.
	usageScrapeLineBuffer = 4 * 1024
	usageMetricsPath      = "/metrics"

	// The series the poller reads, and the gauge both listeners export so that
	// a restart is seen whatever the sample did.
	toolInvocationsSeries  = "kubeagents_tool_invocations_total"
	eventsInjectedSeries   = "k8s_event_watcher_events_injected_total"
	processStartTimeSeries = "process_start_time_seconds"
	// toolInvocationsStatusLabel is the broker's outcome label. The outcomes
	// that mean a command ran to an exit are in toolInvocationsCountedStatuses:
	// blocked and busy never ran, and abandoned cannot say whether the
	// command had started.
	toolInvocationsStatusLabel = "status"

	// The kinds a failed scrape is logged as. Never the body.
	usageScrapeKindConnect = "connect"
	usageScrapeKindStatus  = "status"
	usageScrapeKindRead    = "read"
	usageScrapeKindLine    = "line too long"
	usageScrapeKindParse   = "unparsable line"
	usageScrapeKindSample  = "sample out of range"
)

var toolInvocationsCountedStatuses = map[string]bool{"success": true, "error": true}

// usageSeriesFor is the family each counter is read from and, for the broker,
// the status values summed; nil statuses sums every label set.
func usageSeriesFor(counter string) (family string, statuses map[string]bool) {
	if counter == usageCounterEventsIngested {
		return eventsInjectedSeries, nil
	}
	return toolInvocationsSeries, toolInvocationsCountedStatuses
}

// usageReading is what one scrape yields: the counter summed over its label
// sets, and the start time the body carried, nil when it carried none.
type usageReading struct {
	Sample    int64
	StartTime *float64
}

// usageScrapeError is a scrape that produced no body to count, with a kind the
// log can name. Err carries detail only for the kinds that cannot contain the
// body: the connection and the status line.
type usageScrapeError struct {
	Kind string
	Err  error
}

func (e *usageScrapeError) Error() string {
	if e.Err != nil {
		return e.Kind + ": " + e.Err.Error()
	}
	return e.Kind
}

func (e *usageScrapeError) Unwrap() error { return e.Err }

// usageSource reads a listener. The pod scraper is its one implementation; a
// test supplies a stub, and a deployment that cannot admit operator-to-pod
// traffic could gain another without changing the accumulation or the writer.
type usageSource interface {
	Scrape(ctx context.Context, addr, counter string) (usageReading, error)
}

// podUsageSource reads /metrics at a pod IP and port over the pod network.
type podUsageSource struct {
	client *http.Client
}

func newPodUsageSource() *podUsageSource {
	transport := &http.Transport{
		// No proxy: a pod-network scrape never has one, and the default
		// transport would send the GET to an HTTP_PROXY the operator's
		// environment sets.
		Proxy:             nil,
		DialContext:       (&net.Dialer{Timeout: usageScrapeTimeout}).DialContext,
		DisableKeepAlives: true,
	}
	return &podUsageSource{client: &http.Client{
		Timeout:   usageScrapeTimeout,
		Transport: transport,
		// No redirect: a body on the port cannot send the operator's GET,
		// made from a network position the pod's own egress policy does not
		// have, anywhere else.
		CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
	}}
}

// Scrape GETs the listener at addr and folds the body for counter. Any status
// other than 200 is a failed scrape.
func (s *podUsageSource) Scrape(ctx context.Context, addr, counter string) (usageReading, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, "http://"+addr+usageMetricsPath, nil)
	if err != nil {
		return usageReading{}, &usageScrapeError{Kind: usageScrapeKindConnect, Err: err}
	}
	resp, err := s.client.Do(req)
	if err != nil {
		return usageReading{}, &usageScrapeError{Kind: usageScrapeKindConnect, Err: err}
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return usageReading{}, &usageScrapeError{Kind: usageScrapeKindStatus, Err: fmt.Errorf("HTTP %d", resp.StatusCode)}
	}
	return foldUsageBody(resp.Body, counter)
}

// foldUsageBody scans body line by line and keeps none of it: a sample line of
// counter's family, or of the start-time gauge, is parsed on its own with
// expfmt and folded into the running sum as it is read; every other line is
// skipped unread. A line past the bound, a wanted line that does not parse, or
// a sample that is negative or not finite is a failed scrape.
func foldUsageBody(body io.Reader, counter string) (usageReading, error) {
	family, statuses := usageSeriesFor(counter)
	parser := expfmt.NewTextParser(model.UTF8Validation)
	var sum float64
	var start *float64
	scanner := bufio.NewScanner(body)
	scanner.Buffer(make([]byte, 0, usageScrapeLineBuffer), usageScrapeMaxLineBytes)
	for scanner.Scan() {
		line := scanner.Text()
		name := usageLineName(line)
		if name != family && name != processStartTimeSeries {
			continue
		}
		families, err := parser.TextToMetricFamilies(strings.NewReader(line + "\n"))
		if err != nil || families[name] == nil {
			return usageReading{}, &usageScrapeError{Kind: usageScrapeKindParse}
		}
		for _, metric := range families[name].GetMetric() {
			value, ok := usageSampleValue(metric)
			if !ok || math.IsNaN(value) || math.IsInf(value, 0) || value < 0 {
				return usageReading{}, &usageScrapeError{Kind: usageScrapeKindSample}
			}
			if name == processStartTimeSeries {
				captured := value
				start = &captured
				continue
			}
			if statuses != nil && !statuses[usageLabelValue(metric, toolInvocationsStatusLabel)] {
				continue
			}
			sum += value
		}
	}
	if err := scanner.Err(); err != nil {
		if errors.Is(err, bufio.ErrTooLong) {
			return usageReading{}, &usageScrapeError{Kind: usageScrapeKindLine}
		}
		return usageReading{}, &usageScrapeError{Kind: usageScrapeKindRead, Err: err}
	}
	if sum >= float64(math.MaxInt64) {
		return usageReading{}, &usageScrapeError{Kind: usageScrapeKindSample}
	}
	return usageReading{Sample: int64(sum), StartTime: start}, nil
}

// usageLineName is the metric name a sample line starts with, or "" for a
// comment, a blank line, or a line with no name.
func usageLineName(line string) string {
	if line == "" || line[0] == '#' {
		return ""
	}
	end := strings.IndexAny(line, "{ \t")
	if end < 0 {
		return ""
	}
	return line[:end]
}

// usageSampleValue is the sample of a metric parsed from a single line, which
// expfmt types as untyped; the typed forms are read too in case a body
// carries the TYPE line beside it.
func usageSampleValue(metric *dto.Metric) (float64, bool) {
	switch {
	case metric.Untyped != nil:
		return metric.Untyped.GetValue(), true
	case metric.Counter != nil:
		return metric.Counter.GetValue(), true
	case metric.Gauge != nil:
		return metric.Gauge.GetValue(), true
	}
	return 0, false
}

func usageLabelValue(metric *dto.Metric, name string) string {
	for _, pair := range metric.GetLabel() {
		if pair.GetName() == name {
			return pair.GetValue()
		}
	}
	return ""
}

// usageErrorKind is the kind a failed scrape is logged under.
func usageErrorKind(err error) string {
	var scrape *usageScrapeError
	if errors.As(err, &scrape) {
		return scrape.Kind
	}
	return "error"
}
