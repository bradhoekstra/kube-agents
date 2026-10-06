// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//	http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.
package main

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

// writeScopeProfile writes a Cluster Agent profile the way cluster_agent_profile.py
// does, carrying only what the scope reads: the cluster_identity block.
func writeScopeProfile(t *testing.T, dir, profile string, identity clusterIdentity) {
	t.Helper()
	home := filepath.Join(dir, profile)
	if err := os.MkdirAll(home, 0o700); err != nil {
		t.Fatal(err)
	}
	config := "model:\n  provider: custom\ncluster_identity:\n" +
		"  project: " + identity.Project + "\n" +
		"  cluster: " + identity.Cluster + "\n" +
		"  location: " + identity.Location + "\n"
	if err := os.WriteFile(filepath.Join(home, "config.yaml"), []byte(config), 0o600); err != nil {
		t.Fatal(err)
	}
}

func testProfileScope(dir string, now *time.Time) *profileScope {
	return &profileScope{dir: dir, rescanAfter: profileScopeRescanInterval, now: func() time.Time { return *now }}
}

func TestProfileScopeAnswersFromTheProfilesDirectory(t *testing.T) {
	dir := t.TempDir()
	prodA := clusterIdentity{Project: "p1", Location: "us-central1", Cluster: "prod-a"}
	prodB := clusterIdentity{Project: "p1", Location: "us-central1", Cluster: "prod-b"}
	writeScopeProfile(t, dir, "prod-a", prodA)
	now := time.Now()
	s := testProfileScope(dir, &now)

	if profiled, known := s.Profiled(prodA); !profiled || !known {
		t.Errorf("Profiled(prod-a) = (%v, %v), want (true, true): a profile names it", profiled, known)
	}
	if profiled, known := s.Profiled(prodB); profiled || !known {
		t.Errorf("Profiled(prod-b) = (%v, %v), want (false, true): the directory was read and no profile names it", profiled, known)
	}
}

// A profile written after startup counts as soon as the scope is stale: the
// reconcile onboards clusters while the detector runs, and a record from one
// must not be held because discovery ran before the profile existed.
func TestProfileScopeRereadsTheDirectoryAfterTheInterval(t *testing.T) {
	dir := t.TempDir()
	prodB := clusterIdentity{Project: "p1", Location: "us-central1", Cluster: "prod-b"}
	now := time.Now()
	s := testProfileScope(dir, &now)

	if profiled, known := s.Profiled(prodB); profiled || !known {
		t.Fatalf("Profiled(prod-b) = (%v, %v) on an empty directory, want (false, true): a readable empty scope is known, and is the fresh-install state that holds every record", profiled, known)
	}
	writeScopeProfile(t, dir, "prod-b", prodB)
	if profiled, _ := s.Profiled(prodB); profiled {
		t.Errorf("Profiled(prod-b) = true inside the rescan interval, want the cached answer (the interval is the whole cost bound)")
	}
	now = now.Add(profileScopeRescanInterval)
	if profiled, known := s.Profiled(prodB); !profiled || !known {
		t.Errorf("Profiled(prod-b) = (%v, %v) after the interval, want (true, true): the directory was re-read", profiled, known)
	}
}

// An unreadable directory is an unknown scope, not an empty one, and the
// answer says so rather than reporting every cluster as outside it.
func TestProfileScopeReportsAnUnreadableDirectoryAsUnknown(t *testing.T) {
	now := time.Now()
	s := testProfileScope(filepath.Join(t.TempDir(), "absent"), &now)
	if profiled, known := s.Profiled(clusterIdentity{Project: "p1", Location: "us-central1", Cluster: "prod-a"}); profiled || known {
		t.Errorf("Profiled = (%v, %v) on a missing directory, want (false, false)", profiled, known)
	}
}

func TestNewProfileScopeIsNilWithoutADirectory(t *testing.T) {
	if s := newProfileScope(""); s != nil {
		t.Errorf("newProfileScope(\"\") = %#v, want a nil scopeIndex: no --profiles-dir means the scope was never declared", s)
	}
	if s := newProfileScope(t.TempDir()); s == nil {
		t.Error("newProfileScope(dir) = nil, want a scope")
	}
}
