package container

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strconv"
	"testing"
	"time"

	dockerclient "github.com/docker/docker/client"
)

func TestExtendedInstanceExpiryPreservesRemainingTime(t *testing.T) {
	now := time.Date(2026, 10, 29, 0, 0, 0, 0, time.UTC)
	for _, remaining := range []time.Duration{10 * time.Minute, 59 * time.Minute, 2 * time.Hour} {
		t.Run(remaining.String(), func(t *testing.T) {
			previous := now.Add(remaining)
			got, err := extendedInstanceExpiry(strconv.FormatInt(previous.Unix(), 10), now)
			if err != nil || !got.Equal(previous.Add(30*time.Minute)) {
				t.Fatalf("remaining time was not preserved: expiry=%v err=%v", got, err)
			}
		})
	}
}

func TestExtendInstanceAddsThirtyMinutesAndPersistsIntegration(t *testing.T) {
	f := newOwnedRedisFixture(t)
	owner := f.owner("extension")
	data := f.seedInstance(t, 36016, owner)
	image := "sha256:extension-test-image"
	previous := time.Now().UTC().Add(59 * time.Minute).Truncate(time.Second)
	if err := f.rdb.HSet(f.ctx, "instance:"+owner, "challenge_id", "1", "environment_revision", "1", "environment_verified_image_id", image, "expires_at", previous.Unix()).Err(); err != nil {
		t.Fatal(err)
	}
	learn := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet || r.URL.Path != "/internal/challenges/1/env" || r.URL.Query().Get("purpose") != "instance" {
			t.Error("unexpected Learn request")
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		json.NewEncoder(w).Encode(challengeEnvResponse{EnvironmentReady: true, EnvironmentRevision: 1, EnvironmentVerifiedImageID: image})
	}))
	t.Cleanup(learn.Close)
	dockerServer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet || r.URL.Path != "/v1.44/containers/"+data["container_id"]+"/json" {
			t.Error("unexpected Docker request")
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]string{"Image": image, "Id": data["container_id"]})
	}))
	t.Cleanup(dockerServer.Close)
	docker, err := dockerclient.NewClientWithOpts(dockerclient.WithHost("tcp://"+dockerServer.Listener.Addr().String()), dockerclient.WithVersion("1.44"), dockerclient.WithHTTPClient(dockerServer.Client()))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { docker.Close() })
	manager := &Manager{rdb: f.rdb, docker: docker, learnURL: learn.URL, httpClient: learn.Client(), maxExtendCount: 3}
	if _, err := manager.ExtendInstance(f.ctx, owner, "another-user"); err == nil {
		t.Fatal("another user extended the instance")
	}
	for count := 1; count <= 3; count++ {
		result, err := manager.ExtendInstance(f.ctx, owner, owner)
		want := previous.Add(time.Duration(count) * 30 * time.Minute)
		if err != nil || result == nil || !result.ExpiresAt.Equal(want) || result.ExtendCount != count || result.MaxExtendCount != 3 {
			t.Fatalf("extension %d did not add thirty minutes: result=%+v err=%v", count, result, err)
		}
		stored, err := f.rdb.HGetAll(f.ctx, "instance:"+owner).Result()
		if err != nil || stored["expires_at"] != strconv.FormatInt(want.Unix(), 10) || stored["extend_count"] != strconv.Itoa(count) {
			t.Fatal("extension result differs from persisted metadata")
		}
		ttl, err := f.rdb.TTL(f.ctx, "instance:"+owner).Result()
		if err != nil || ttl < time.Until(want) || ttl > time.Until(want.Add(instanceMetadataRetention))+time.Second {
			t.Fatal("extension did not preserve the metadata retention window")
		}
	}
	if _, err := manager.ExtendInstance(f.ctx, owner, owner); err == nil {
		t.Fatal("extension count limit was bypassed")
	}
}

func TestExtendedInstanceExpiryRejectsExpiredOrInvalidMetadata(t *testing.T) {
	now := time.Date(2026, 10, 29, 0, 0, 0, 0, time.UTC)
	for _, value := range []string{"", "invalid", "0", "-1", strconv.FormatInt(now.Unix(), 10), strconv.FormatInt(now.Add(-time.Second).Unix(), 10)} {
		t.Run(value, func(t *testing.T) {
			if _, err := extendedInstanceExpiry(value, now); err == nil {
				t.Fatal("expired or malformed metadata was extended")
			}
		})
	}
}
