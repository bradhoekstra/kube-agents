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
	"log"
	"sync"
	"time"

	"github.com/gke-labs/kube-agents/k8s-operator/internal/clusterprofiles"
)

const (
	// profileScopeRescanInterval bounds how stale the scope may be: an
	// unreachable record arriving this long after the last read of the
	// profiles directory reads it again before being judged. One directory
	// listing and one small file per profile, so a minute is cheap at any
	// fleet size the join itself can carry -- and it is the interval that
	// makes the hold safe on a long-lived pod, since cluster-agent-reconcile
	// writes profiles while the detector runs and discovery (the
	// reachability pass) runs once at startup.
	profileScopeRescanInterval = time.Minute
)

// scopeIndex answers whether the install's scope names a cluster, which is a
// different question from whether the join can read it. Every cluster the
// Platform Agent's reconcile onboards gets a Cluster Agent profile, so "a
// readable profile names it" is the detector's view of the scope; a cluster
// the join could not reach despite a profile (a 403 at discovery, a profile
// written after startup) is inside it, and a cluster in the project with no
// profile at all -- excluded, or never onboarded -- is outside.
type scopeIndex interface {
	// Profiled reports whether a readable Cluster Agent profile names
	// identity. known is false when there is no answer: the profiles
	// directory could not be read, so the scope is unknown rather than empty,
	// and the caller holds nothing.
	Profiled(identity clusterIdentity) (profiled, known bool)
}

// profileScope is the scopeIndex over a profiles directory. It re-reads the
// directory lazily, when asked and at most once per profileScopeRescanInterval,
// and never addresses a cluster: clusterprofiles.ReadIdentities reads config
// files only.
//
// A profile the read drops -- its config unreadable, unparsable, or naming no
// complete identity on this read -- is logged by name once per streak, and
// keeps the identity it last carried for as long as its directory exists. The
// second half is what closes the scaffold window: cluster_agent_profile.py
// writes a profile's config in stages and stamps the identity last, in place,
// so a read landing mid-write sees a cluster profile that names nothing; a
// first scaffold was unprofiled an instant earlier and loses nothing, but a
// re-scaffold of an existing profile would otherwise hold an in-scope cluster
// for up to an interval. The identity is dropped only with the directory, which
// is how the reconcile offboards a cluster.
//
// Safe for concurrent use. The joiner that asks it is single-threaded today,
// but the lock costs nothing and the rescan writes state, so the type does not
// inherit a constraint the joiner documents for a different reason.
type profileScope struct {
	dir string
	now func() time.Time

	mu        sync.Mutex
	scannedAt time.Time
	// byProfile is each cluster profile's identity by directory name, the
	// last one read for it; profiled is the same set keyed for the answer.
	byProfile map[string]clusterIdentity
	profiled  map[clusterIdentity]struct{}
	readable  bool
	// unreadableLogged keeps a directory that stays unreadable from logging
	// once per record; it resets on the next successful read so a directory
	// that breaks again is reported again.
	unreadableLogged bool
	// skipped is the profiles the last read dropped, by directory name, so
	// each is logged once per streak rather than once per rescan: the hold
	// line alone would send the operator to write a profile that exists.
	skipped map[string]struct{}
}

// newProfileScope returns the scope over dir, or nil when dir is empty: a
// detector started without --profiles-dir was never told the install's scope,
// and a nil scopeIndex is how the joiner learns that.
func newProfileScope(dir string) scopeIndex {
	if dir == "" {
		return nil
	}
	return &profileScope{dir: dir, now: time.Now}
}

// Profiled implements scopeIndex.
func (s *profileScope) Profiled(identity clusterIdentity) (bool, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.scannedAt.IsZero() || s.now().Sub(s.scannedAt) >= profileScopeRescanInterval {
		s.rescan()
	}
	if !s.readable {
		return false, false
	}
	_, ok := s.profiled[identity]
	return ok, true
}

// rescan replaces the profiled set from the directory. Caller holds mu.
func (s *profileScope) rescan() {
	s.scannedAt = s.now()
	byProfile := map[string]clusterIdentity{}
	skipped := map[string]struct{}{}
	ids, err := clusterprofiles.ReadIdentities(s.dir, func(profile string, err error) {
		skipped[profile] = struct{}{}
		kept, known := s.byProfile[profile]
		if known {
			byProfile[profile] = kept
		}
		if _, logged := s.skipped[profile]; logged {
			return
		}
		if known {
			log.Printf("%s: profile %s names no cluster on this read (%v); keeping %s inside the install's scope while the profile directory exists", commandName, profile, err, kept)
			return
		}
		log.Printf("%s: profile %s names no readable cluster for the install's scope (%v); records from its cluster are held out of the inject as outside the scope until it does", commandName, profile, err)
	})
	if err != nil {
		s.readable = false
		if !s.unreadableLogged {
			log.Printf("%s: cannot read %s to tell which clusters are inside the install's scope, so no record is held out of the inject until it can be read: %v", commandName, s.dir, err)
			s.unreadableLogged = true
		}
		return
	}
	for _, p := range ids {
		byProfile[p.Profile] = identityFromProfile(p.Identity)
	}
	set := make(map[clusterIdentity]struct{}, len(byProfile))
	for _, id := range byProfile {
		set[id] = struct{}{}
	}
	s.readable = true
	s.unreadableLogged = false
	s.skipped = skipped
	s.byProfile = byProfile
	s.profiled = set
}
