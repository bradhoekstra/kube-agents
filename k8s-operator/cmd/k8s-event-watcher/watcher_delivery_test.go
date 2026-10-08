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

package main

import (
	"context"
	"fmt"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/watch"
	"k8s.io/client-go/kubernetes/fake"
	k8stesting "k8s.io/client-go/testing"
)

// stallingDispatcher blocks every Dispatch until release is closed, and
// counts the calls that have entered.
type stallingDispatcher struct {
	entered atomic.Int64
	release chan struct{}
}

func (d *stallingDispatcher) Dispatch(ctx context.Context, _ TriageEvent) {
	d.entered.Add(1)
	select {
	case <-d.release:
	case <-ctx.Done():
	}
}

func (d *stallingDispatcher) RecordScaleUpMark(TriageEvent) bool { return false }

// listOf builds a fake Event list of n distinct pods.
func listOf(n int) *corev1.EventList {
	list := &corev1.EventList{ListMeta: metav1.ListMeta{ResourceVersion: "10"}}
	for i := 0; i < n; i++ {
		list.Items = append(list.Items, listedEvent(fmt.Sprintf("pod-%d", i), "BackOff", fmt.Sprintf("api-%d.1", i)))
	}
	return list
}

// TestRun_SyncDoesNotWaitOnDelivery pins that the initial sync — and so
// cluster_up and the no-cluster-synced exit in main.go — depends on the API
// server's list alone, not on how fast the daemon takes what the list
// carries. The delivery channel is bounded, so a daemon that stalls on every
// accepted event holds the pop once the channel is full; the sync has to be
// reported anyway, as it was when the shared informer buffered without bound,
// or a single-cluster install with a wedged daemon restarts the watcher every
// two minutes and relists into the same state.
func TestRun_SyncDoesNotWaitOnDelivery(t *testing.T) {
	captureLog(t)
	client := fake.NewClientset()
	allowPreflight(client)
	client.PrependReactor("list", "events", func(k8stesting.Action) (bool, runtime.Object, error) {
		return true, listOf(deliveryQueueDepth + 2), nil
	})
	client.PrependWatchReactor("events", func(k8stesting.Action) (bool, watch.Interface, error) {
		return true, watch.NewFakeWithChanSize(1, false), nil
	})
	rec := &stallingDispatcher{release: make(chan struct{})}
	w := newWatcher(client, rec, targetCluster{Name: "stalled-daemon"})

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	synced := make(chan struct{})
	done := make(chan error, 1)
	go func() {
		done <- w.Run(ctx, func(watching bool) {
			if watching {
				close(synced)
			}
		})
	}()
	select {
	case <-synced:
	case <-time.After(10 * time.Second):
		t.Fatalf("the sync was not reported within 10s while the daemon stalled; %d dispatch(es) entered, %d events listed, channel depth %d",
			rec.entered.Load(), deliveryQueueDepth+2, deliveryQueueDepth)
	}
	if got := rec.entered.Load(); got != 1 {
		t.Errorf("%d dispatches entered while the first was stalled; want 1 (one delivery goroutine per cluster)", got)
	}

	close(rec.release)
	cancel()
	if err := <-done; err != nil {
		t.Errorf("Run returned %v; want nil on shutdown", err)
	}
}

// TestRun_DeletedEventsAreNotDispatched pins that an Event expiring is not
// an observation: a watch Deleted carries the Event's last state, and
// converting and dispatching it would feed the dedup window — or open a card
// — for an incident the API server merely stopped remembering.
//
// With no store of known objects the DeltaFIFO drops a Deleted for a key it
// has already popped, so the one shape of Deleted that reaches process is a
// deletion queued behind an Add of the same key that has not been popped yet.
// The test builds exactly that: it stalls delivery with a list wider than
// the channel so pops stop, sends an Add and then a Delete for one new Event,
// releases delivery, and checks that Event was dispatched once, for the Add.
func TestRun_DeletedEventsAreNotDispatched(t *testing.T) {
	captureLog(t)
	client := fake.NewClientset()
	allowPreflight(client)
	client.PrependReactor("list", "events", func(k8stesting.Action) (bool, runtime.Object, error) {
		return true, listOf(deliveryQueueDepth + 2), nil
	})
	var openWatch atomic.Pointer[watch.FakeWatcher]
	client.PrependWatchReactor("events", func(k8stesting.Action) (bool, watch.Interface, error) {
		fw := watch.NewFakeWithChanSize(2, false)
		openWatch.Store(fw)
		return true, fw, nil
	})
	rec := &recordingStallDispatcher{release: make(chan struct{})}
	w := newWatcher(client, rec, targetCluster{Name: "expiring"})

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	synced := make(chan struct{})
	go func() {
		done <- w.Run(ctx, func(watching bool) {
			if watching {
				close(synced)
			}
		})
	}()
	select {
	case <-synced:
	case <-time.After(10 * time.Second):
		t.Fatal("the reflector never synced")
	}
	eventually(t, 10*time.Second, func() bool { return openWatch.Load() != nil }, "the reflector to open its watch")
	// Delivery is stalled on the first listed event and the channel is full
	// behind it, so these two queue up under one key and are popped together.
	live := listedEvent("pod-live", "Unhealthy", "api-live.1")
	live.ResourceVersion = "11"
	openWatch.Load().Add(&live)
	expired := live
	expired.ResourceVersion = "12"
	openWatch.Load().Delete(&expired)
	eventually(t, 10*time.Second, func() bool { return rec.entered.Load() >= 1 }, "delivery to begin")

	close(rec.release)
	eventually(t, 10*time.Second, func() bool { return rec.count("Unhealthy") >= 1 }, "the live event to be dispatched")
	// Everything queued ahead of it has been delivered by now; give a second
	// dispatch of the same Event, if there were going to be one, time to show.
	time.Sleep(200 * time.Millisecond)
	if got := rec.count("Unhealthy"); got != 1 {
		t.Errorf("the Event was dispatched %d time(s); want 1 (its deletion must not be dispatched)", got)
	}

	cancel()
	if err := <-done; err != nil {
		t.Errorf("Run returned %v; want nil on shutdown", err)
	}
}

// recordingStallDispatcher stalls like stallingDispatcher until released,
// and once released counts the reasons it is handed.
type recordingStallDispatcher struct {
	entered atomic.Int64
	release chan struct{}
	mu      sync.Mutex
	reasons map[string]int
}

func (d *recordingStallDispatcher) Dispatch(ctx context.Context, ev TriageEvent) {
	d.entered.Add(1)
	select {
	case <-d.release:
	case <-ctx.Done():
		return
	}
	d.mu.Lock()
	defer d.mu.Unlock()
	if d.reasons == nil {
		d.reasons = map[string]int{}
	}
	d.reasons[ev.Key.Reason]++
}

func (d *recordingStallDispatcher) RecordScaleUpMark(TriageEvent) bool { return false }

func (d *recordingStallDispatcher) count(reason string) int {
	d.mu.Lock()
	defer d.mu.Unlock()
	return d.reasons[reason]
}
