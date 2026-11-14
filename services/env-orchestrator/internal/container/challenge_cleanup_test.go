package container

import (
	"context"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"reflect"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	dockertypes "github.com/docker/docker/api/types"
	"github.com/docker/docker/api/types/container"
	dockerclient "github.com/docker/docker/client"
	"github.com/docker/go-connections/nat"
)

const cleanupTestJobID = "11223344-5566-7788-99aa-bbccddeeff00"
const cleanupTestChallengeID = 42

type cleanupDockerFixture struct {
	mu           sync.Mutex
	containers   map[string]dockertypes.ContainerJSON
	removed      []string
	removeMode   string
	onRemove     func()
	imagePresent bool
	imageOwned   bool
	imageRemoves []string
	imageMode    string
	requests     int
}

type cleanupDockerSnapshot struct {
	removed      []string
	imageRemoves []string
	containers   map[string]bool
	imagePresent bool
}

func (f *cleanupDockerFixture) snapshot() cleanupDockerSnapshot {
	f.mu.Lock()
	defer f.mu.Unlock()
	result := cleanupDockerSnapshot{removed: append([]string(nil), f.removed...), imageRemoves: append([]string(nil), f.imageRemoves...), containers: map[string]bool{}, imagePresent: f.imagePresent}
	for id := range f.containers {
		result.containers[id] = true
	}
	return result
}

func newCleanupDockerFixture(t *testing.T) (*dockerclient.Client, *cleanupDockerFixture) {
	t.Helper()
	f := &cleanupDockerFixture{containers: make(map[string]dockertypes.ContainerJSON)}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		f.mu.Lock()
		defer f.mu.Unlock()
		f.requests++
		w.Header().Set("Content-Type", "application/json")
		path := strings.TrimPrefix(r.URL.Path, "/v1.44")
		switch {
		case r.Method == http.MethodGet && path == "/containers/json":
			if r.URL.Query().Get("all") != "1" && r.URL.Query().Get("all") != "true" {
				t.Error("cleanup did not include stopped containers")
			}
			var filters map[string]json.RawMessage
			if err := json.Unmarshal([]byte(r.URL.Query().Get("filters")), &filters); err != nil {
				t.Error("cleanup omitted Docker label filters")
			}
			var labels []string
			if err := json.Unmarshal(filters["label"], &labels); err != nil {
				var flags map[string]bool
				if json.Unmarshal(filters["label"], &flags) != nil {
					t.Error("cleanup label filters have an invalid shape")
				}
				for label, enabled := range flags {
					if enabled {
						labels = append(labels, label)
					}
				}
			}
			items := []dockertypes.Container{}
			for _, inspection := range f.containers {
				matches := true
				for _, label := range labels {
					parts := strings.SplitN(label, "=", 2)
					if len(parts) != 2 || inspection.Config.Labels[parts[0]] != parts[1] {
						matches = false
					}
				}
				if !matches {
					continue
				}
				item := dockertypes.Container{ID: inspection.ID, Names: []string{inspection.Name}, Labels: inspection.Config.Labels, State: "running"}
				for _, bindings := range inspection.HostConfig.PortBindings {
					for _, binding := range bindings {
						port, _ := strconv.Atoi(binding.HostPort)
						item.Ports = append(item.Ports, dockertypes.Port{PublicPort: uint16(port), PrivatePort: 8080, Type: "tcp"})
					}
				}
				items = append(items, item)
			}
			json.NewEncoder(w).Encode(items)
		case r.Method == http.MethodGet && strings.HasPrefix(path, "/containers/") && strings.HasSuffix(path, "/json"):
			id := strings.TrimSuffix(strings.TrimPrefix(path, "/containers/"), "/json")
			if inspection, exists := f.containers[id]; exists {
				json.NewEncoder(w).Encode(inspection)
			} else {
				w.WriteHeader(http.StatusNotFound)
				json.NewEncoder(w).Encode(map[string]string{"message": "owned fixture absent"})
			}
		case r.Method == http.MethodDelete && strings.HasPrefix(path, "/containers/"):
			id := strings.TrimPrefix(path, "/containers/")
			if f.removeMode == "failure" {
				w.WriteHeader(http.StatusInternalServerError)
				json.NewEncoder(w).Encode(map[string]string{"message": "owned removal failure"})
				return
			}
			if _, exists := f.containers[id]; !exists {
				w.WriteHeader(http.StatusNotFound)
				json.NewEncoder(w).Encode(map[string]string{"message": "owned fixture absent"})
				return
			}
			f.removed = append(f.removed, id)
			delete(f.containers, id)
			if f.onRemove != nil {
				f.onRemove()
			}
			if f.removeMode == "lost_response" {
				connection, _, err := w.(http.Hijacker).Hijack()
				if err != nil {
					t.Error("owned response-loss fixture could not close the response")
					return
				}
				connection.Close()
				return
			}
			w.WriteHeader(http.StatusNoContent)
		case r.Method == http.MethodGet && strings.HasPrefix(path, "/images/") && strings.HasSuffix(path, "/json"):
			if !f.imagePresent {
				w.WriteHeader(http.StatusNotFound)
				json.NewEncoder(w).Encode(map[string]string{"message": "owned image absent"})
				return
			}
			labels := map[string]string{}
			if f.imageOwned {
				labels["ctf-platform-built"], labels["ctf-platform-challenge-id"] = "true", "42"
			}
			json.NewEncoder(w).Encode(map[string]interface{}{"Id": "sha256:owned-fixture", "RepoTags": []string{"ctf-challenge-42:latest", "unrelated:keep"}, "Config": map[string]interface{}{"Labels": labels}})
		case r.Method == http.MethodDelete && strings.HasPrefix(path, "/images/"):
			image := strings.TrimPrefix(path, "/images/")
			if image != "ctf-challenge-42:latest" || r.URL.Query().Get("force") == "1" || r.URL.Query().Get("force") == "true" ||
				(r.URL.Query().Get("noprune") != "1" && r.URL.Query().Get("noprune") != "true") {
				t.Error("cleanup attempted a wildcard, shared image ID, force or child prune")
			}
			if f.imageMode == "conflict" {
				w.WriteHeader(http.StatusConflict)
				json.NewEncoder(w).Encode(map[string]string{"message": "owned image conflict"})
				return
			}
			f.imageRemoves = append(f.imageRemoves, image)
			f.imagePresent = false
			json.NewEncoder(w).Encode([]map[string]string{{"Untagged": image}})
		default:
			t.Errorf("unexpected cleanup Docker request: %s %s", r.Method, path)
			w.WriteHeader(http.StatusBadRequest)
			json.NewEncoder(w).Encode(map[string]string{"message": "unexpected owned request"})
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
	return docker, f
}

func cleanupFixtureContainer(challengeID int, instanceID, userID string, port int) dockertypes.ContainerJSON {
	labels := instanceContainerLabels(CreateRequest{ChallengeID: challengeID, UserID: userID}, instanceID)
	bindings := nat.PortMap{}
	if strings.HasPrefix(instanceID, "verify_") {
		labels = verificationContainerLabels(challengeID, instanceID)
	} else {
		bindings[nat.Port("8080/tcp")] = []nat.PortBinding{{HostIP: "127.0.0.1", HostPort: strconv.Itoa(port)}}
	}
	return dockertypes.ContainerJSON{
		ContainerJSONBase: &dockertypes.ContainerJSONBase{ID: cleanupFixtureContainerID(instanceID), Name: "/ctf-" + instanceID, HostConfig: &container.HostConfig{PortBindings: bindings}},
		Config:            &container.Config{Labels: labels},
	}
}

func cleanupFixtureContainerID(instanceID string) string {
	return fmt.Sprintf("%x", sha256.Sum256([]byte("owned-cleanup-"+instanceID)))
}

func cleanupProofServer(t *testing.T, status int, proof challengeCleanupProof, errorCodes ...string) *httptest.Server {
	t.Helper()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodPost && r.URL.Path == dynamicFlagRevokePath && r.Header.Get("X-Internal-Key") == "owned-cleanup-key" {
			w.WriteHeader(http.StatusNoContent)
			return
		}
		if r.Method != http.MethodGet || r.URL.Path != "/internal/challenges/42/cleanup-target" || r.URL.Query().Get("job_id") != cleanupTestJobID || r.Header.Get("X-Internal-Key") != "owned-cleanup-key" {
			t.Error("cleanup proof was not bound to the exact internal job")
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(status)
		if status == http.StatusConflict {
			code := "cleanup_challenge_exists"
			if len(errorCodes) > 0 && errorCodes[0] != "" {
				code = errorCodes[0]
			}
			json.NewEncoder(w).Encode(map[string]interface{}{"detail": map[string]string{"code": code, "message": "owned fixture rejection"}})
			return
		}
		json.NewEncoder(w).Encode(proof)
	}))
	t.Cleanup(server.Close)
	return server
}

func newCleanupTestManager(t *testing.T, f *ownedRedisFixture, imageReference string) (*Manager, *cleanupDockerFixture) {
	t.Helper()
	docker, fixture := newCleanupDockerFixture(t)
	proof := cleanupProofServer(t, 200, challengeCleanupProof{ChallengeID: 42, JobID: cleanupTestJobID, Deleted: true, ImageReference: imageReference})
	manager := &Manager{rdb: f.rdb, docker: docker, portAllocator: NewPortAllocator(f.rdb, 36017, 36025), internalAPIKey: "owned-cleanup-key", learnURL: proof.URL, httpClient: proof.Client()}
	manager.SetDynamicFlagRuntimeDir(t.TempDir())
	f.keys["challenge_cleanup:"+cleanupTestJobID+":instances"] = true
	return manager, fixture
}

func seedCleanupTestInstance(t *testing.T, f *ownedRedisFixture, challengeID, port int, owner string) map[string]string {
	t.Helper()
	data := f.seedInstance(t, port, owner)
	data["challenge_id"] = strconv.Itoa(challengeID)
	data["container_id"] = cleanupFixtureContainerID(owner)
	if err := f.rdb.HSet(f.ctx, "instance:"+owner, "challenge_id", data["challenge_id"], "container_id", data["container_id"]).Err(); err != nil {
		t.Fatal("owned cleanup challenge binding failed")
	}
	return data
}

func TestDeletedChallengeCleanupRejectsUnprovenTarget(t *testing.T) {
	for _, tc := range []struct {
		name   string
		status int
		proof  challengeCleanupProof
		want   error
		code   string
	}{
		{"current_challenge", 409, challengeCleanupProof{}, ErrCleanupChallengeExists, "cleanup_challenge_exists"},
		{"rejected_target", 409, challengeCleanupProof{}, ErrCleanupTargetRejected, "cleanup_target_rejected"},
		{"unknown_conflict", 409, challengeCleanupProof{}, ErrCleanupTargetRejected, "not_allowlisted"},
		{"unknown_job", 404, challengeCleanupProof{}, ErrCleanupTargetRejected, ""},
		{"wrong_job", 200, challengeCleanupProof{ChallengeID: 42, JobID: "00000000-0000-0000-0000-000000000000", Deleted: true}, ErrCleanupTargetRejected, ""},
		{"wrong_challenge", 200, challengeCleanupProof{ChallengeID: 43, JobID: cleanupTestJobID, Deleted: true}, ErrCleanupTargetRejected, ""},
		{"not_deleted", 200, challengeCleanupProof{ChallengeID: 42, JobID: cleanupTestJobID}, ErrCleanupTargetRejected, ""},
		{"external_image", 200, challengeCleanupProof{ChallengeID: 42, JobID: cleanupTestJobID, Deleted: true, ImageReference: "another-owner:latest"}, ErrCleanupTargetRejected, ""},
	} {
		t.Run(tc.name, func(t *testing.T) {
			proof := cleanupProofServer(t, tc.status, tc.proof, tc.code)
			docker, fixture := newCleanupDockerFixture(t)
			manager := &Manager{docker: docker, internalAPIKey: "owned-cleanup-key", learnURL: proof.URL, httpClient: proof.Client()}
			if _, err := manager.CleanupDeletedChallengeResources(context.Background(), 42, cleanupTestJobID); !errors.Is(err, tc.want) {
				t.Fatalf("unproven target result = %v, want %v", err, tc.want)
			}
			if fixture.requests != 0 {
				t.Fatal("unproven deletion touched Docker")
			}
		})
	}
}

func TestDeletedChallengeCleanupOwnedResourcesIntegration(t *testing.T) {
	f := newOwnedRedisFixture(t)
	manager, docker := newCleanupTestManager(t, f, "ctf-challenge-42:latest")
	owner := "inst_" + f.prefix
	data := seedCleanupTestInstance(t, f, 42, 36017, owner)
	other := "inst_" + strings.Repeat("a", 32)
	otherData := seedCleanupTestInstance(t, f, 43, 36018, other)
	owned := cleanupFixtureContainer(42, owner, owner, 36017)
	protected := cleanupFixtureContainer(43, other, other, 36018)
	external := cleanupFixtureContainer(99, "inst_"+strings.Repeat("b", 32), "unrelated-user", 36019)
	external.Config.Labels["ctf-challenge"] = "false"
	external.Config.Labels["com.docker.compose.project"] = "another-project"
	docker.containers[owned.ID], docker.containers[protected.ID], docker.containers[external.ID] = owned, protected, external
	docker.imagePresent = true
	if _, err := manager.writeCompatibilityFlagFile(owner); err != nil {
		t.Fatal(err)
	}
	result, err := manager.CleanupDeletedChallengeResources(f.ctx, 42, cleanupTestJobID)
	if err != nil || result.Status != "removed" || result.ChallengeID != 42 || result.JobID != cleanupTestJobID || result.ImageReference != "ctf-challenge-42:latest" {
		t.Fatalf("owned cleanup result = %+v, error = %v", result, err)
	}
	state := docker.snapshot()
	if !reflect.DeepEqual(state.removed, []string{data["container_id"]}) || !reflect.DeepEqual(state.imageRemoves, []string{"ctf-challenge-42:latest"}) {
		t.Fatal("cleanup changed a non-target container or image tag")
	}
	if len(state.containers) != 2 || !state.containers[protected.ID] || !state.containers[external.ID] {
		t.Fatal("protected shared-host containers were changed")
	}
	if got, err := f.rdb.HGetAll(f.ctx, "instance:"+other).Result(); err != nil || !reflect.DeepEqual(got, otherData) {
		t.Fatal("another challenge metadata changed")
	}
	assertCleanupMetadataGone(t, f, owner)
	if _, err := f.rdb.ZScore(f.ctx, allocatedPortCooldownsKey, "36017|"+owner).Result(); err != nil {
		t.Fatal("confirmed cleanup did not retain the existing port cooldown")
	}
	if err := manager.confirmCleanupRuntimeAbsent(owner); err != nil {
		t.Fatal("instance runtime file remained")
	}
	result, err = manager.CleanupDeletedChallengeResources(f.ctx, 42, cleanupTestJobID)
	state = docker.snapshot()
	if err != nil || result.Status != "absent" || len(state.removed) != 1 || len(state.imageRemoves) != 1 {
		t.Fatalf("completed cleanup retry was not idempotent: %+v %v", result, err)
	}
}

func assertCleanupMetadataGone(t *testing.T, f *ownedRedisFixture, owner string) {
	t.Helper()
	if exists, err := f.rdb.Exists(f.ctx, "instance:"+owner, "challenge_cleanup:"+cleanupTestJobID+":instances").Result(); err != nil || exists != 0 {
		t.Fatal("completed cleanup left instance or journal metadata")
	}
	if member, err := f.rdb.SIsMember(f.ctx, "user_instances:"+owner, owner).Result(); err != nil || member {
		t.Fatal("completed cleanup left the user reference")
	}
}

func TestDeletedChallengeCleanupFailureAndRetryIntegration(t *testing.T) {
	for _, mode := range []string{"failure", "lost_response", "redis_after_remove", "orphan_redis_after_remove", "image_conflict"} {
		t.Run(mode, func(t *testing.T) {
			f := newOwnedRedisFixture(t)
			image := ""
			if mode == "image_conflict" {
				image = "ctf-challenge-42:latest"
			}
			manager, docker := newCleanupTestManager(t, f, image)
			owner := "inst_" + f.prefix
			data := seedCleanupTestInstance(t, f, 42, 36020, owner)
			var runtimePaths []string
			if mode == "failure" {
				data["dynamic_flag_enabled"] = "true"
				if err := f.rdb.HSet(f.ctx, "instance:"+owner, "dynamic_flag_enabled", "true").Err(); err != nil {
					t.Fatal(err)
				}
				for _, write := range []func() (string, error){
					func() (string, error) { return manager.writeDynamicFlagFile(owner, "owned-dynamic-runtime") },
					func() (string, error) { return manager.writeCompatibilityFlagFile(owner) },
				} {
					path, err := write()
					if err != nil {
						t.Fatal(err)
					}
					runtimePaths = append(runtimePaths, path)
				}
			}
			owned := cleanupFixtureContainer(42, owner, owner, 36020)
			docker.containers[owned.ID] = owned
			if mode == "orphan_redis_after_remove" {
				if err := f.rdb.Del(f.ctx, "instance:"+owner).Err(); err != nil {
					t.Fatal(err)
				}
			}
			if mode == "redis_after_remove" || mode == "orphan_redis_after_remove" {
				docker.onRemove = func() {
					if err := f.rdb.Set(f.ctx, allocatedPortCooldownsKey, "owned-invalid-type", 0).Err(); err != nil {
						t.Error("owned Redis failure injection failed")
					}
				}
			} else if mode == "image_conflict" {
				docker.imagePresent, docker.imageMode = true, "conflict"
			} else {
				docker.removeMode = mode
			}
			first, err := manager.CleanupDeletedChallengeResources(f.ctx, 42, cleanupTestJobID)
			if mode == "lost_response" {
				if err != nil || first.Status != "removed" {
					t.Fatalf("confirmed response loss did not recover: %+v %v", first, err)
				}
				assertCleanupMetadataGone(t, f, owner)
				return
			}
			if err == nil {
				t.Fatal("incomplete cleanup reported success")
			}
			if exists, err := f.rdb.Exists(f.ctx, "challenge_cleanup:"+cleanupTestJobID+":instances").Result(); err != nil || exists != 1 {
				t.Fatal("failed cleanup lost the durable recovery journal")
			}
			if mode == "failure" {
				if got, err := f.rdb.HGetAll(f.ctx, "instance:"+owner).Result(); err != nil || !reflect.DeepEqual(got, data) {
					t.Fatal("failed Docker removal changed recovery metadata")
				}
				if count, err := f.rdb.ZCard(f.ctx, allocatedPortCooldownsKey).Result(); err != nil || count != 0 {
					t.Fatal("failed Docker removal released a port")
				}
				for _, path := range runtimePaths {
					if _, err := os.Lstat(path); err != nil {
						t.Fatal("unconfirmed Docker removal changed the running instance runtime")
					}
				}
			}
			if mode == "redis_after_remove" || mode == "orphan_redis_after_remove" {
				if err := f.rdb.Del(f.ctx, allocatedPortCooldownsKey).Err(); err != nil {
					t.Fatal(err)
				}
			}
			docker.mu.Lock()
			docker.removeMode, docker.imageMode, docker.onRemove = "", "", nil
			docker.mu.Unlock()
			result, err := manager.CleanupDeletedChallengeResources(f.ctx, 42, cleanupTestJobID)
			if err != nil || result.Status != "removed" {
				t.Fatalf("exact cleanup retry did not recover: %+v %v", result, err)
			}
			assertCleanupMetadataGone(t, f, owner)
			if len(docker.snapshot().removed) != 1 {
				t.Fatal("retry removed something other than the one owned container")
			}
			if _, err := f.rdb.ZScore(f.ctx, allocatedPortCooldownsKey, "36020|"+owner).Result(); err != nil {
				t.Fatal("recovered cleanup lost the owner-bound cooldown")
			}
			if err := manager.confirmCleanupRuntimeAbsent(owner); err != nil {
				t.Fatal("confirmed removal did not clean its instance runtime")
			}
		})
	}
}

func TestDeletedChallengeCleanupRejectsContainerAliases(t *testing.T) {
	instanceID := "inst_" + strings.Repeat("a", 32)
	for _, reference := range []string{"ctf-" + instanceID, "abcdef123456", strings.Repeat("b", 63), strings.Repeat("C", 64), ""} {
		t.Run(fmt.Sprintf("reference_%d", len(reference)), func(t *testing.T) {
			data := map[string]string{"challenge_id": "42", "container_id": reference, "user_id": "owned-user", "port": "36022"}
			entry := challengeCleanupInstance{ChallengeID: 42, InstanceID: instanceID, ContainerID: reference, UserID: "owned-user", Data: data}
			if validateCleanupMetadata(42, instanceID, data) == nil || validateCleanupJournal(42, instanceID, entry) == nil {
				t.Fatal("container alias was accepted as a durable exact identity")
			}
			docker, fixture := newCleanupDockerFixture(t)
			manager := &Manager{docker: docker}
			if _, err := manager.confirmCleanupContainer(context.Background(), entry); !errors.Is(err, ErrCleanupTargetRejected) {
				t.Fatal("container alias reached the Docker mutation boundary")
			}
			if fixture.requests != 0 {
				t.Fatal("container alias triggered a Docker lookup")
			}
		})
	}
}

func TestDeletedChallengeCleanupRejectsShortListedIDIntegration(t *testing.T) {
	f := newOwnedRedisFixture(t)
	manager, docker := newCleanupTestManager(t, f, "")
	owner := "inst_" + f.prefix
	seedCleanupTestInstance(t, f, 42, 36022, owner)
	listed := cleanupFixtureContainer(42, owner, owner, 36022)
	listed.ID = "not-a-full-container-id"
	docker.containers[listed.ID] = listed
	if _, err := manager.CleanupDeletedChallengeResources(f.ctx, 42, cleanupTestJobID); !errors.Is(err, ErrCleanupTargetRejected) {
		t.Fatal("short listed container ID was accepted")
	}
	if state := docker.snapshot(); len(state.removed) != 0 || len(state.containers) != 1 {
		t.Fatal("invalid identity changed a container")
	}
}

func TestDeletedChallengeCleanupRejectsChangedMetadataIntegration(t *testing.T) {
	f := newOwnedRedisFixture(t)
	manager, docker := newCleanupTestManager(t, f, "")
	owner := "inst_" + f.prefix
	seedCleanupTestInstance(t, f, 43, 36021, owner)
	owned := cleanupFixtureContainer(42, owner, owner, 36021)
	docker.containers[owned.ID] = owned
	if _, err := manager.CleanupDeletedChallengeResources(f.ctx, 42, cleanupTestJobID); !errors.Is(err, ErrCleanupTargetRejected) {
		t.Fatal("changed Redis challenge ownership was accepted")
	}
	state := docker.snapshot()
	if len(state.removed) != 0 || len(state.containers) != 1 {
		t.Fatal("ownership conflict removed a container")
	}
}

func TestDeletedChallengeCleanupVerificationAndExternalImageIntegration(t *testing.T) {
	f := newOwnedRedisFixture(t)
	manager, docker := newCleanupTestManager(t, f, "")
	verificationID := "verify_" + f.prefix
	owned := cleanupFixtureContainer(42, verificationID, "", 0)
	docker.containers[owned.ID] = owned
	docker.imagePresent = true
	path, err := manager.writeDynamicFlagFile(verificationID, "owned-verification-placeholder")
	if err != nil {
		t.Fatal(err)
	}
	result, err := manager.CleanupDeletedChallengeResources(f.ctx, 42, cleanupTestJobID)
	state := docker.snapshot()
	if err != nil || result.Status != "removed" || len(state.removed) != 1 {
		t.Fatalf("verification cleanup failed: %+v %v", result, err)
	}
	if !state.imagePresent || len(state.imageRemoves) != 0 {
		t.Fatal("resources-only job changed an external or shared image")
	}
	if _, err := os.Lstat(path); !os.IsNotExist(err) {
		t.Fatal("verification runtime file remained")
	}
}

func TestDeletedChallengeCleanupLateOwnedBuildIntegration(t *testing.T) {
	f := newOwnedRedisFixture(t)
	manager, docker := newCleanupTestManager(t, f, "")
	docker.imagePresent, docker.imageOwned = true, true
	result, err := manager.CleanupDeletedChallengeResources(f.ctx, 42, cleanupTestJobID)
	state := docker.snapshot()
	if err != nil || result.Status != "removed" || result.ImageReference != "" || state.imagePresent || len(state.imageRemoves) != 1 {
		t.Fatalf("late owned build from an empty-image deletion was not cleaned: %+v %v", result, err)
	}
}

func TestChallengeOperationGatePreservesSharedWorkAndCancellation(t *testing.T) {
	ctx := context.Background()
	first, err := acquireChallengeOperation(ctx, 88001, false)
	if err != nil {
		t.Fatal(err)
	}
	defer first()
	second, err := acquireChallengeOperation(ctx, 88001, false)
	if err != nil {
		t.Fatal(err)
	}
	second()
	waitCtx, cancel := context.WithTimeout(ctx, 25*time.Millisecond)
	defer cancel()
	if _, err := acquireChallengeOperation(waitCtx, 88001, true); !errors.Is(err, context.DeadlineExceeded) {
		t.Fatal("cleanup did not wait for shared build/provision work")
	}
	other, err := acquireChallengeOperation(ctx, 88002, true)
	if err != nil {
		t.Fatal("another challenge was blocked by unrelated shared work")
	}
	other()
	first()
	last, err := acquireChallengeOperation(ctx, 88001, true)
	if err != nil {
		t.Fatal(err)
	}
	last()
	challengeOperationGates.Lock()
	remaining := challengeOperationGates.entries[88001]
	challengeOperationGates.Unlock()
	if remaining != nil {
		t.Fatal("completed challenge gate leaked registry state")
	}
}

func TestChallengeOperationGateWiresBuildCreateAndVerification(t *testing.T) {
	for index, operation := range []func(context.Context) error{
		func(ctx context.Context) error { _, err := (&Builder{}).BuildImage(ctx, 88003); return err },
		func(ctx context.Context) error {
			_, err := (&Manager{}).CreateInstance(ctx, CreateRequest{ChallengeID: 88003})
			return err
		},
		func(ctx context.Context) error {
			_, err := (&Manager{}).VerifyChallengeEnvironment(ctx, 88003, "owned-admin")
			return err
		},
	} {
		t.Run(fmt.Sprintf("entry_%d", index), func(t *testing.T) {
			end, err := acquireChallengeOperation(context.Background(), 88003, true)
			if err != nil {
				t.Fatal(err)
			}
			defer end()
			ctx, cancel := context.WithTimeout(context.Background(), 25*time.Millisecond)
			defer cancel()
			if err := operation(ctx); !errors.Is(err, context.DeadlineExceeded) {
				t.Fatal("entry point bypassed the deletion gate or ignored cancellation")
			}
		})
	}
}
