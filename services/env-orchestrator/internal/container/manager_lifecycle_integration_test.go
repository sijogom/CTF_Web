package container

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"reflect"
	"strconv"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	dockerclient "github.com/docker/docker/client"
)

func ownedManagerDocker(t *testing.T, containerID string, stopStatus, removeStatus int) (*dockerclient.Client, *atomic.Int32) {
	t.Helper()
	removes := &atomic.Int32{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		status := http.StatusBadRequest
		switch {
		case r.Method == http.MethodPost && r.URL.Path == "/v1.44/containers/"+containerID+"/stop":
			status = stopStatus
		case r.Method == http.MethodDelete && r.URL.Path == "/v1.44/containers/"+containerID:
			removes.Add(1)
			status = removeStatus
		default:
			t.Errorf("unexpected loopback Docker method/path: %s %s", r.Method, r.URL.Path)
		}
		w.WriteHeader(status)
		if status != http.StatusNoContent {
			json.NewEncoder(w).Encode(map[string]string{"message": "owned Docker fixture response"})
		}
	}))
	t.Cleanup(server.Close)
	client := server.Client()
	client.Timeout = time.Second
	docker, err := dockerclient.NewClientWithOpts(dockerclient.WithHost("tcp://"+server.Listener.Addr().String()), dockerclient.WithVersion("1.44"), dockerclient.WithHTTPClient(client))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { docker.Close() })
	return docker, removes
}

func TestManagerStopRemovalContractsIntegration(t *testing.T) {
	for _, tc := range []struct {
		name         string
		stop, remove int
		success      bool
	}{
		{"remove_failure", 500, 500, false},
		{"remove_success", 204, 204, true},
		{"already_missing", 404, 404, true},
		{"stop_failure_remove_success", 500, 204, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			f := newOwnedRedisFixture(t)
			owner := f.owner(tc.name)
			data := f.seedInstance(t, 36007, owner)
			other := f.owner("unaffected")
			otherData := f.seedInstance(t, 36008, other)
			docker, removes := ownedManagerDocker(t, data["container_id"], tc.stop, tc.remove)
			manager := &Manager{rdb: f.rdb, docker: docker, portAllocator: NewPortAllocator(f.rdb, 36007, 36008)}
			manager.SetDynamicFlagRuntimeDir(t.TempDir())
			err := manager.StopInstance(f.ctx, owner, owner)
			if (err == nil) != tc.success || removes.Load() != 1 {
				t.Fatalf("stop result disagrees with confirmed removal: success=%t removes=%d", err == nil, removes.Load())
			}
			exists, err := f.rdb.Exists(f.ctx, "instance:"+owner).Result()
			if err != nil || (exists == 0) != tc.success {
				t.Fatal("instance metadata did not follow confirmed removal")
			}
			member, err := f.rdb.SIsMember(f.ctx, "user_instances:"+owner, owner).Result()
			if err != nil || member == tc.success {
				t.Fatal("owner membership did not follow confirmed removal")
			}
			if reserved, err := f.rdb.SIsMember(f.ctx, allocatedPortsKey, 36007).Result(); err != nil || !reserved {
				t.Fatal("stop immediately freed a port")
			}
			cooldowns, err := f.rdb.ZCard(f.ctx, allocatedPortCooldownsKey).Result()
			if err != nil || (cooldowns == 1) != tc.success {
				t.Fatal("cooldown was not conditional on confirmed removal")
			}
			if got, err := f.rdb.HGetAll(f.ctx, "instance:"+other).Result(); err != nil || !reflect.DeepEqual(got, otherData) {
				t.Fatal("another user's metadata changed")
			}
			if retained, err := f.rdb.SIsMember(f.ctx, "user_instances:"+other, other).Result(); err != nil || !retained {
				t.Fatal("another user's membership changed")
			}
			if retained, err := f.rdb.SIsMember(f.ctx, allocatedPortsKey, 36008).Result(); err != nil || !retained {
				t.Fatal("another user's reservation changed")
			}
		})
	}
}

func TestManagerFailedRemovalRevokesRuntimeAndKeepsCountIntegration(t *testing.T) {
	f := newOwnedRedisFixture(t)
	owner := f.owner("runtime")
	data := f.seedInstance(t, 36009, owner)
	docker, _ := ownedManagerDocker(t, data["container_id"], 500, 500)
	var revocations atomic.Int32
	learn := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost || r.URL.Path != dynamicFlagRevokePath {
			t.Error("unexpected loopback runtime cleanup request")
			w.WriteHeader(400)
			return
		}
		revocations.Add(1)
		w.WriteHeader(http.StatusNoContent)
	}))
	t.Cleanup(learn.Close)
	manager := &Manager{rdb: f.rdb, docker: docker, portAllocator: NewPortAllocator(f.rdb, 36009, 36009), internalAPIKey: "owned-test-key", learnURL: learn.URL, httpClient: learn.Client()}
	manager.SetDynamicFlagRuntimeDir(t.TempDir())
	flagPath, err := manager.writeDynamicFlagFile(owner, "owned-test-flag")
	if err != nil {
		t.Fatal(err)
	}
	compatPath, err := manager.writeCompatibilityFlagFile(owner)
	if err != nil {
		t.Fatal(err)
	}
	if err := f.rdb.HSet(f.ctx, "instance:"+owner, "dynamic_flag_enabled", "true", "expires_at", strconv.FormatInt(time.Now().Add(-time.Minute).Unix(), 10)).Err(); err != nil {
		t.Fatal(err)
	}
	if err := manager.StopInstance(f.ctx, owner, owner); err == nil {
		t.Fatal("failed removal reported successful stop")
	}
	if _, err := os.Stat(flagPath); !os.IsNotExist(err) {
		t.Fatal("failed Docker removal retained the secret runtime file")
	}
	if _, err := os.Stat(compatPath); !os.IsNotExist(err) {
		t.Fatal("failed Docker removal retained the compatibility runtime file")
	}
	if revocations.Load() != 1 {
		t.Fatal("failed removal skipped independent issuance revocation")
	}
	if count, err := manager.cleanAndCountInstances(f.ctx, "user_instances:"+owner); err != nil || count != 1 {
		t.Fatal("unremoved instance was excluded from the user capacity count")
	}
}

func TestManagerCooldownFailurePreservesRecoveryMetadataIntegration(t *testing.T) {
	f := newOwnedRedisFixture(t)
	owner := f.owner("cooldown_failure")
	data := f.seedInstance(t, 36010, owner)
	docker, _ := ownedManagerDocker(t, data["container_id"], 204, 204)
	manager := &Manager{rdb: f.rdb, docker: docker, portAllocator: NewPortAllocator(f.rdb, 36010, 36010)}
	manager.SetDynamicFlagRuntimeDir(t.TempDir())
	if err := f.rdb.Set(f.ctx, allocatedPortCooldownsKey, "owned-invalid-type", 0).Err(); err != nil {
		t.Fatal(err)
	}
	f.wrongCooldown = true
	if err := manager.StopInstance(f.ctx, owner, owner); err == nil {
		t.Fatal("unpersisted cooldown reported successful stop")
	}
	if got, err := f.rdb.HGetAll(f.ctx, "instance:"+owner).Result(); err != nil || !reflect.DeepEqual(got, data) {
		t.Fatal("failed cooldown write lost recovery metadata")
	}
	if member, err := f.rdb.SIsMember(f.ctx, "user_instances:"+owner, owner).Result(); err != nil || !member {
		t.Fatal("failed cooldown write lost ownership")
	}
	if reserved, err := f.rdb.SIsMember(f.ctx, allocatedPortsKey, 36010).Result(); err != nil || !reserved {
		t.Fatal("failed cooldown write freed capacity")
	}
	if err := f.rdb.Del(f.ctx, allocatedPortCooldownsKey).Err(); err != nil {
		t.Fatal(err)
	}
}

func TestStartedContainerCleanupUsesConfirmedRemovalIntegration(t *testing.T) {
	f := newOwnedRedisFixture(t)
	owner := f.owner("unrecorded")
	f.ports[36011] = true
	pa := NewPortAllocator(f.rdb, 36011, 36011)
	if _, err := pa.AllocateWithinLimitForOwner(f.ctx, 1, owner); err != nil {
		t.Fatal(err)
	}
	docker, _ := ownedManagerDocker(t, "ctf-"+owner, 204, 500)
	manager := &Manager{rdb: f.rdb, docker: docker, portAllocator: pa}
	manager.SetDynamicFlagRuntimeDir(t.TempDir())
	manager.cleanupStartedContainer(f.ctx, "ctf-"+owner, 36011, owner, false, "container_start_failed")
	if count, err := f.rdb.ZCard(f.ctx, allocatedPortCooldownsKey).Result(); err != nil || count != 0 {
		t.Fatal("unconfirmed create cleanup scheduled release")
	}
	if reserved, err := f.rdb.SIsMember(f.ctx, allocatedPortsKey, 36011).Result(); err != nil || !reserved {
		t.Fatal("unconfirmed create cleanup freed capacity")
	}
}

func TestPortOwnerSeparatorValidation(t *testing.T) {
	for _, value := range []string{"a|b", "a\nb", "a\rb", "a\x00b"} {
		if ValidPortOwner(value) {
			t.Fatalf("invalid reservation identity accepted: %q", strings.ReplaceAll(value, "\x00", "NUL"))
		}
	}
}

func TestUnreadInstanceDoesNotBypassUserCapacityIntegration(t *testing.T) {
	f := newOwnedRedisFixture(t)
	owner := f.owner("unread")
	f.seedInstance(t, 36012, owner)
	if err := f.rdb.Del(f.ctx, "instance:"+owner).Err(); err != nil {
		t.Fatal(err)
	}
	if err := f.rdb.Set(f.ctx, "instance:"+owner, "owned-unreadable-metadata", 0).Err(); err != nil {
		t.Fatal(err)
	}
	manager := &Manager{rdb: f.rdb}
	if _, err := manager.cleanAndCountInstances(f.ctx, "user_instances:"+owner); err == nil {
		t.Fatal("unread instance was treated as an empty user capacity")
	}
	if retained, err := f.rdb.SIsMember(f.ctx, "user_instances:"+owner, owner).Result(); err != nil || !retained {
		t.Fatal("unread instance lost owner membership")
	}
	if retained, err := f.rdb.SIsMember(f.ctx, allocatedPortsKey, 36012).Result(); err != nil || !retained {
		t.Fatal("unread instance lost its reservation")
	}
}

func TestInstanceCleanupRedisFailurePreservesBothReferencesIntegration(t *testing.T) {
	f := newOwnedRedisFixture(t)
	owner := f.owner("metadata_failure")
	data := f.seedInstance(t, 36013, owner)
	if err := f.rdb.Del(f.ctx, "user_instances:"+owner).Err(); err != nil {
		t.Fatal(err)
	}
	if err := f.rdb.Set(f.ctx, "user_instances:"+owner, "owned-invalid-user-type", 0).Err(); err != nil {
		t.Fatal(err)
	}
	if err := DeleteInstanceRecord(f.ctx, f.rdb, owner, data); err == nil {
		t.Fatal("invalid user reference type was accepted")
	}
	if got, err := f.rdb.HGetAll(f.ctx, "instance:"+owner).Result(); err != nil || !reflect.DeepEqual(got, data) {
		t.Fatal("failed metadata transaction partially deleted the instance")
	}
	if got, err := f.rdb.Get(f.ctx, "user_instances:"+owner).Result(); err != nil || got != "owned-invalid-user-type" {
		t.Fatal("failed metadata transaction changed the owner reference")
	}
}

func TestStopConfirmedOldMetadataAfterPortReuseIntegration(t *testing.T) {
	f := newOwnedRedisFixture(t)
	old, current := f.owner("old_metadata"), f.owner("new_reservation")
	data := f.seedInstance(t, 36015, old)
	// The actual 30-second test proves this post-reuse state is reachable.
	if err := f.rdb.HSet(f.ctx, allocatedPortOwnersKey, "36015", current).Err(); err != nil {
		t.Fatal(err)
	}
	pa := NewPortAllocator(f.rdb, 36015, 36015)
	if err := pa.ReleaseOwned(f.ctx, 36015, current); err != nil {
		t.Fatal(err)
	}
	member := "36015|" + current
	deadline, err := f.rdb.ZScore(f.ctx, allocatedPortCooldownsKey, member).Result()
	if err != nil {
		t.Fatal(err)
	}
	docker, _ := ownedManagerDocker(t, data["container_id"], 404, 404)
	manager := &Manager{rdb: f.rdb, docker: docker, portAllocator: pa}
	manager.SetDynamicFlagRuntimeDir(t.TempDir())
	if err := manager.StopInstance(f.ctx, old, old); err != nil {
		t.Fatal("confirmed old metadata could not be cleaned after reservation reuse")
	}
	if exists, err := f.rdb.Exists(f.ctx, "instance:"+old).Result(); err != nil || exists != 0 {
		t.Fatal("old recovery metadata still occupies capacity")
	}
	if retained, err := f.rdb.SIsMember(f.ctx, "user_instances:"+old, old).Result(); err != nil || retained {
		t.Fatal("old owner membership still occupies capacity")
	}
	if owner, err := f.rdb.HGet(f.ctx, allocatedPortOwnersKey, "36015").Result(); err != nil || owner != current {
		t.Fatal("old stop retry changed the new owner")
	}
	if score, err := f.rdb.ZScore(f.ctx, allocatedPortCooldownsKey, member).Result(); err != nil || score != deadline {
		t.Fatal("old stop retry changed the new owner's first deadline")
	}
}
