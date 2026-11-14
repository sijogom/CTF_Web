package lifecycle

import (
	"context"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"reflect"
	"strconv"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	dockertypes "github.com/docker/docker/api/types"
	dockerclient "github.com/docker/docker/client"
	"github.com/redis/go-redis/v9"

	ctfcontainer "github.com/ctf-platform/env-orchestrator/internal/container"
)

type ownedLifecycleFixture struct {
	rdb           *redis.Client
	ctx           context.Context
	owner         string
	port          int
	instanceKey   string
	userKey       string
	data          map[string]string
	wrongCooldown bool
	owners        map[string]bool
}

// The permanent lifecycle tests use the same explicit ownership boundary as
// the allocator tests, independently of private verification fixtures.
func newOwnedLifecycleFixture(t *testing.T) *ownedLifecycleFixture {
	t.Helper()
	mode := os.Getenv("CTF_TEST_REDIS_INTEGRATION")
	if mode == "" {
		t.Skip("owned Redis integration was not explicitly enabled")
	}
	if mode != "owned" {
		t.Fatal("invalid Redis integration opt-in")
	}
	address := os.Getenv("CTF_TEST_REDIS_ADDR")
	host, port, err := net.SplitHostPort(address)
	nonce, pid, runID := os.Getenv("CTF_TEST_REDIS_NONCE"), os.Getenv("CTF_TEST_REDIS_PROCESS_ID"), os.Getenv("CTF_TEST_REDIS_RUN_ID")
	processID, pidErr := strconv.Atoi(pid)
	if err != nil || host != "127.0.0.1" || port == "" || os.Getenv("CTF_TEST_REDIS_DB") != "15" || len(nonce) != 36 || strings.ContainsAny(nonce, "|\r\n\x00") || pidErr != nil || processID <= 0 || len(runID) != 40 || os.Getenv("CTF_GO_LIFECYCLE_REDIS_PASSWORD") == "" {
		t.Fatal("owned loopback DB15 process binding missing")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	rdb := redis.NewClient(&redis.Options{Addr: address, DB: 15, Password: os.Getenv("CTF_GO_LIFECYCLE_REDIS_PASSWORD"), PoolSize: 1, MaxRetries: -1, DialTimeout: time.Second, ReadTimeout: time.Second, WriteTimeout: time.Second})
	t.Cleanup(func() { cancel(); rdb.Close() })
	info, err := rdb.Info(ctx, "server").Result()
	if err != nil {
		t.Fatal("cannot inspect identified integration Redis")
	}
	fields := map[string]string{}
	for _, line := range strings.Split(info, "\n") {
		pair := strings.SplitN(strings.TrimSpace(line), ":", 2)
		if len(pair) == 2 {
			fields[pair[0]] = pair[1]
		}
	}
	if fields["process_id"] != pid || fields["run_id"] != runID {
		t.Fatal("integration Redis process identity differs")
	}
	if size, err := rdb.DBSize(ctx).Result(); err != nil || size != 0 {
		t.Fatal("DB15 is not empty; no writes performed")
	}
	clientID, err := rdb.Do(ctx, "CLIENT", "ID").Int64()
	if err != nil {
		t.Fatal("cannot bind integration client")
	}
	clients, err := rdb.ClientList(ctx).Result()
	if err != nil {
		t.Fatal("cannot inspect exclusive DB15 clients")
	}
	for _, line := range strings.Split(strings.TrimSpace(clients), "\n") {
		values := map[string]string{}
		for _, item := range strings.Fields(line) {
			pair := strings.SplitN(item, "=", 2)
			if len(pair) == 2 {
				values[pair[0]] = pair[1]
			}
		}
		if values["db"] == "15" && values["id"] != strconv.FormatInt(clientID, 10) {
			t.Fatal("another DB15 client exists; no writes performed")
		}
	}
	owner := "inst_" + strings.ReplaceAll(nonce, "-", "") + "_lifecycle"
	f := &ownedLifecycleFixture{rdb: rdb, ctx: ctx, owner: owner, port: 36101, instanceKey: "instance:" + owner, userKey: "user_instances:" + owner}
	f.owners = map[string]bool{"": true, owner: true}
	f.data = map[string]string{"container_id": "docker-" + owner, "user_id": owner, "port": strconv.Itoa(f.port), "expires_at": strconv.FormatInt(time.Now().Add(time.Hour).Unix(), 10), "dynamic_flag_enabled": "false"}
	t.Cleanup(func() { f.cleanup(t) })
	return f
}

func (f *ownedLifecycleFixture) seed(t *testing.T) {
	t.Helper()
	if err := f.seedData(); err != nil {
		t.Fatal(err)
	}
}

func (f *ownedLifecycleFixture) seedData() error {
	if err := f.rdb.HSet(f.ctx, f.instanceKey, f.data).Err(); err != nil {
		return err
	}
	if err := f.rdb.SAdd(f.ctx, f.userKey, f.owner).Err(); err != nil {
		return err
	}
	return f.rdb.SAdd(f.ctx, "allocated_ports", f.port).Err()
}

func (f *ownedLifecycleFixture) cleanup(t *testing.T) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	keys, err := f.rdb.Keys(ctx, "*").Result()
	if err != nil {
		t.Error("owned lifecycle cleanup inspection failed")
		return
	}
	for _, key := range keys {
		if key != f.instanceKey && key != f.userKey && key != "allocated_ports" && key != "allocated_port_owners" && key != "allocated_port_cooldowns" {
			t.Error("unknown lifecycle key preserved")
			return
		}
	}
	ports, err := f.rdb.SMembers(ctx, "allocated_ports").Result()
	if err != nil {
		t.Error("lifecycle port cleanup inspection failed")
		return
	}
	for _, port := range ports {
		if port != strconv.Itoa(f.port) {
			t.Error("unknown lifecycle port preserved")
			return
		}
	}
	owners, err := f.rdb.HGetAll(ctx, "allocated_port_owners").Result()
	if err != nil {
		t.Error("lifecycle owner cleanup inspection failed")
		return
	}
	for port, owner := range owners {
		if port != strconv.Itoa(f.port) || !f.owners[owner] {
			t.Error("unknown lifecycle owner preserved")
			return
		}
	}
	if kind, err := f.rdb.Type(ctx, "allocated_port_cooldowns").Result(); err != nil {
		t.Error("lifecycle cooldown type inspection failed")
		return
	} else if kind == "string" && f.wrongCooldown {
		value, err := f.rdb.Get(ctx, "allocated_port_cooldowns").Result()
		if err != nil || value != "owned-invalid-type" {
			t.Error("unknown wrong-type lifecycle cooldown preserved")
			return
		}
		if err := f.rdb.Del(ctx, "allocated_port_cooldowns").Err(); err != nil {
			t.Error("owned wrong-type lifecycle cooldown cleanup failed")
			return
		}
	}
	members, err := f.rdb.ZRange(ctx, "allocated_port_cooldowns", 0, -1).Result()
	if err != nil {
		t.Error("lifecycle cooldown cleanup inspection failed")
		return
	}
	for _, member := range members {
		parts := strings.SplitN(member, "|", 2)
		if len(parts) != 2 || parts[0] != strconv.Itoa(f.port) || !f.owners[parts[1]] {
			t.Error("unknown lifecycle cooldown preserved")
			return
		}
	}
	if err := f.rdb.Del(ctx, f.instanceKey, f.userKey).Err(); err != nil {
		t.Error("owned lifecycle metadata cleanup failed")
	}
	if err := f.rdb.SRem(ctx, "allocated_ports", f.port).Err(); err != nil {
		t.Error("owned lifecycle port cleanup failed")
	}
	if err := f.rdb.HDel(ctx, "allocated_port_owners", strconv.Itoa(f.port)).Err(); err != nil {
		t.Error("owned lifecycle owner cleanup failed")
	}
	for _, member := range members {
		if err := f.rdb.ZRem(ctx, "allocated_port_cooldowns", member).Err(); err != nil {
			t.Error("owned lifecycle cooldown cleanup failed")
		}
	}
	if size, err := f.rdb.DBSize(ctx).Result(); err != nil || size != 0 {
		t.Error("DB15 did not return to its empty baseline")
	}
}

func (f *ownedLifecycleFixture) assertPreserved(t *testing.T) {
	t.Helper()
	if data, err := f.rdb.HGetAll(f.ctx, f.instanceKey).Result(); err != nil || !reflect.DeepEqual(data, f.data) {
		t.Fatal("recovery metadata changed")
	}
	if member, err := f.rdb.SIsMember(f.ctx, f.userKey, f.owner).Result(); err != nil || !member {
		t.Fatal("recovery ownership changed")
	}
	if reserved, err := f.rdb.SIsMember(f.ctx, "allocated_ports", f.port).Result(); err != nil || !reserved {
		t.Fatal("occupied port reservation changed")
	}
}

type lifecycleDockerPlan struct {
	list            []dockertypes.Container
	listStatus      int
	stopStatus      int
	removeStatus    int
	inspectStatus   int
	onList          func() error
	onInspect       func() error
	removes         atomic.Int32
	inspectOverride atomic.Int32
	omitList        atomic.Bool
}

func newLifecycleDocker(t *testing.T, f *ownedLifecycleFixture, plan *lifecycleDockerPlan) *dockerclient.Client {
	t.Helper()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		status := http.StatusBadRequest
		switch {
		case r.Method == http.MethodGet && r.URL.Path == "/v1.44/containers/json":
			if plan.onList != nil {
				if err := plan.onList(); err != nil {
					t.Error("owned list callback failed")
					w.WriteHeader(500)
					return
				}
			}
			if plan.listStatus == 0 || plan.listStatus == 200 {
				if plan.omitList.Load() {
					json.NewEncoder(w).Encode([]dockertypes.Container{})
					return
				}
				json.NewEncoder(w).Encode(plan.list)
				return
			}
			status = plan.listStatus
		case r.Method == http.MethodGet && strings.HasPrefix(r.URL.Path, "/v1.44/containers/") && strings.HasSuffix(r.URL.Path, "/json"):
			target := strings.TrimSuffix(strings.TrimPrefix(r.URL.Path, "/v1.44/containers/"), "/json")
			if target != f.data["container_id"] && !(strings.HasPrefix(target, "ctf-") && f.owners[strings.TrimPrefix(target, "ctf-")]) {
				t.Error("unknown lifecycle inspect identity")
				w.WriteHeader(400)
				return
			}
			if plan.onInspect != nil {
				if err := plan.onInspect(); err != nil {
					t.Error("owned inspect callback failed")
					w.WriteHeader(500)
					return
				}
			}
			status = plan.inspectStatus
			if override := plan.inspectOverride.Load(); override > 0 {
				status = int(override)
			}
			if status == 200 {
				json.NewEncoder(w).Encode(map[string]interface{}{"Id": f.data["container_id"], "State": map[string]bool{"Running": true}})
				return
			}
		case r.Method == http.MethodPost && r.URL.Path == "/v1.44/containers/"+f.data["container_id"]+"/stop":
			status = plan.stopStatus
		case r.Method == http.MethodDelete && r.URL.Path == "/v1.44/containers/"+f.data["container_id"]:
			plan.removes.Add(1)
			status = plan.removeStatus
		default:
			t.Errorf("unexpected loopback lifecycle Docker request: %s %s", r.Method, r.URL.Path)
		}
		if status == 0 {
			status = http.StatusBadRequest
		}
		w.WriteHeader(status)
		if status != http.StatusNoContent {
			json.NewEncoder(w).Encode(map[string]string{"message": "owned lifecycle Docker response"})
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
	return docker
}

func (f *ownedLifecycleFixture) row() dockertypes.Container {
	return dockertypes.Container{ID: f.data["container_id"], Created: time.Now().Add(-time.Hour).Unix(), State: "running", Labels: map[string]string{"ctf-challenge": "true", "instance-id": f.owner}, Ports: []dockertypes.Port{{PrivatePort: 8080, PublicPort: uint16(f.port), Type: "tcp"}}}
}

func TestTTLRemovalContractsIntegration(t *testing.T) {
	for _, tc := range []struct {
		name         string
		stop, remove int
		success      bool
	}{
		{"remove_failure", 500, 500, false}, {"remove_success", 204, 204, true}, {"not_found", 404, 404, true}, {"stop_failure_remove_success", 500, 204, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			f := newOwnedLifecycleFixture(t)
			f.seed(t)
			plan := &lifecycleDockerPlan{stopStatus: tc.stop, removeStatus: tc.remove}
			docker := newLifecycleDocker(t, f, plan)
			var cleanups atomic.Int32
			cleanup := func(id string, data map[string]string, reason string) {
				if id != f.owner || reason != "instance_expired" || !reflect.DeepEqual(data, f.data) {
					t.Error("runtime cleanup scope changed")
				}
				cleanups.Add(1)
			}
			watcher := NewTTLWatcher(f.rdb, docker, ctfcontainer.NewPortAllocator(f.rdb, f.port, f.port), cleanup, time.Second)
			watcher.cleanupInstance(f.ctx, f.instanceKey)
			if plan.removes.Load() != 1 || cleanups.Load() != 1 {
				t.Fatal("TTL skipped removal or independent runtime cleanup")
			}
			if !tc.success {
				f.assertPreserved(t)
			} else {
				if exists, err := f.rdb.Exists(f.ctx, f.instanceKey).Result(); err != nil || exists != 0 {
					t.Fatal("confirmed TTL removal retained metadata")
				}
				if member, err := f.rdb.SIsMember(f.ctx, f.userKey, f.owner).Result(); err != nil || member {
					t.Fatal("confirmed TTL removal retained owner reference")
				}
			}
			if port, err := f.rdb.SIsMember(f.ctx, "allocated_ports", f.port).Result(); err != nil || !port {
				t.Fatal("TTL immediately released a port")
			}
			if count, err := f.rdb.ZCard(f.ctx, "allocated_port_cooldowns").Result(); err != nil || (count == 1) != tc.success {
				t.Fatal("TTL cooldown does not follow confirmed removal")
			}
		})
	}
}

func TestReconcilerOccupiedPortsIntegration(t *testing.T) {
	for _, mode := range []string{"remove_failure", "missing_label", "redis_lookup_failure", "docker_list_failure"} {
		t.Run(mode, func(t *testing.T) {
			f := newOwnedLifecycleFixture(t)
			f.data["expires_at"] = strconv.FormatInt(time.Now().Add(-time.Minute).Unix(), 10)
			f.seed(t)
			row := f.row()
			plan := &lifecycleDockerPlan{list: []dockertypes.Container{row}, removeStatus: 500, inspectStatus: 500}
			if mode == "missing_label" {
				delete(plan.list[0].Labels, "instance-id")
			}
			if mode == "docker_list_failure" {
				plan.listStatus = 500
			}
			if mode == "redis_lookup_failure" {
				if err := f.rdb.Del(f.ctx, f.instanceKey).Err(); err != nil {
					t.Fatal(err)
				}
				if err := f.rdb.Set(f.ctx, f.instanceKey, "owned-invalid-type", 0).Err(); err != nil {
					t.Fatal(err)
				}
			}
			docker := newLifecycleDocker(t, f, plan)
			pa := ctfcontainer.NewPortAllocator(f.rdb, f.port, f.port)
			r := NewReconciler(f.rdb, docker, nil, time.Minute, pa)
			r.reconcile(f.ctx)
			if mode == "redis_lookup_failure" {
				if value, err := f.rdb.Get(f.ctx, f.instanceKey).Result(); err != nil || value != "owned-invalid-type" {
					t.Fatal("failed Redis lookup modified recovery data")
				}
				if err := f.rdb.Del(f.ctx, f.instanceKey).Err(); err != nil {
					t.Fatal(err)
				}
				if err := f.rdb.HSet(f.ctx, f.instanceKey, f.data).Err(); err != nil {
					t.Fatal(err)
				}
			}
			f.assertPreserved(t)
			if count, err := f.rdb.ZCard(f.ctx, "allocated_port_cooldowns").Result(); err != nil || count != 0 {
				t.Fatal("unconfirmed or unidentifiable Docker occupancy received a release deadline")
			}
			if mode == "remove_failure" && plan.removes.Load() != 1 {
				t.Fatal("removal failure fixture was not exercised")
			}
		})
	}
}

func TestReconcilerLatestInspectIntegration(t *testing.T) {
	for _, status := range []int{200, 404, 500} {
		t.Run(strconv.Itoa(status), func(t *testing.T) {
			f := newOwnedLifecycleFixture(t)
			f.seed(t)
			plan := &lifecycleDockerPlan{list: []dockertypes.Container{}, inspectStatus: status}
			docker := newLifecycleDocker(t, f, plan)
			var cleanups atomic.Int32
			r := NewReconciler(f.rdb, docker, func(id string, data map[string]string, reason string) { cleanups.Add(1) }, time.Minute, ctfcontainer.NewPortAllocator(f.rdb, f.port, f.port))
			r.reconcile(f.ctx)
			if status != 404 {
				f.assertPreserved(t)
				if cleanups.Load() != 0 {
					t.Fatal("unconfirmed absence revoked runtime")
				}
			} else {
				if exists, err := f.rdb.Exists(f.ctx, f.instanceKey).Result(); err != nil || exists != 0 {
					t.Fatal("latest NotFound did not clean confirmed missing metadata")
				}
				if cleanups.Load() != 1 {
					t.Fatal("confirmed absence skipped runtime cleanup")
				}
			}
			if port, err := f.rdb.SIsMember(f.ctx, "allocated_ports", f.port).Result(); err != nil || !port {
				t.Fatal("latest inspection bypassed the cooldown")
			}
		})
	}
}

func TestReconcilerConfirmedRemovalCooldownIntegration(t *testing.T) {
	for _, status := range []int{204, 404} {
		t.Run(strconv.Itoa(status), func(t *testing.T) {
			f := newOwnedLifecycleFixture(t)
			f.data["expires_at"] = strconv.FormatInt(time.Now().Add(-time.Minute).Unix(), 10)
			f.seed(t)
			plan := &lifecycleDockerPlan{list: []dockertypes.Container{f.row()}, removeStatus: status}
			docker := newLifecycleDocker(t, f, plan)
			var cleanups atomic.Int32
			r := NewReconciler(f.rdb, docker, func(id string, data map[string]string, reason string) {
				if reason != "reconcile_cleanup" {
					t.Error("reconciliation cleanup reason changed")
				}
				cleanups.Add(1)
			}, time.Minute, ctfcontainer.NewPortAllocator(f.rdb, f.port, f.port))
			r.reconcile(f.ctx)
			if plan.removes.Load() != 1 || cleanups.Load() != 1 {
				t.Fatal("confirmed removal contract was not exercised")
			}
			if exists, err := f.rdb.Exists(f.ctx, f.instanceKey).Result(); err != nil || exists != 0 {
				t.Fatal("confirmed removal retained metadata")
			}
			if member, err := f.rdb.SIsMember(f.ctx, f.userKey, f.owner).Result(); err != nil || member {
				t.Fatal("confirmed removal retained owner membership")
			}
			if reserved, err := f.rdb.SIsMember(f.ctx, "allocated_ports", f.port).Result(); err != nil || !reserved {
				t.Fatal("reconciler bypassed the cooldown")
			}
			if count, err := f.rdb.ZCard(f.ctx, "allocated_port_cooldowns").Result(); err != nil || count != 1 {
				t.Fatal("reconciler did not persist the confirmed removal cooldown")
			}
		})
	}
}

func TestReconcilerOrphanCooldownIntegration(t *testing.T) {
	for _, legacy := range []bool{false, true} {
		t.Run(strconv.FormatBool(legacy), func(t *testing.T) {
			f := newOwnedLifecycleFixture(t)
			pa := ctfcontainer.NewPortAllocator(f.rdb, f.port, f.port)
			owner := f.owner
			if legacy {
				owner = ""
				if err := f.rdb.SAdd(f.ctx, "allocated_ports", f.port).Err(); err != nil {
					t.Fatal(err)
				}
			} else if _, err := pa.AllocateWithinLimitForOwner(f.ctx, 1, owner); err != nil {
				t.Fatal(err)
			}
			plan := &lifecycleDockerPlan{list: []dockertypes.Container{}, inspectStatus: 404}
			r := NewReconciler(f.rdb, newLifecycleDocker(t, f, plan), nil, time.Minute, pa)
			r.reconcile(f.ctx)
			if plan.removes.Load() != 0 {
				t.Fatal("orphan reservation cleanup removed an unobserved Docker resource")
			}
			if reserved, err := f.rdb.SIsMember(f.ctx, "allocated_ports", f.port).Result(); err != nil || !reserved {
				t.Fatal("orphan reservation skipped the cooldown")
			}
			if _, err := f.rdb.ZScore(f.ctx, "allocated_port_cooldowns", fmt.Sprintf("%d|%s", f.port, owner)).Result(); err != nil {
				t.Fatal("orphan cooldown was not bound to the observed owner")
			}
		})
	}
}

func TestReconcilerCreationAndSnapshotRaceIntegration(t *testing.T) {
	for _, mode := range []string{"long_creation", "metadata_after_list", "metadata_before_orphan_release"} {
		t.Run(mode, func(t *testing.T) {
			f := newOwnedLifecycleFixture(t)
			pa := ctfcontainer.NewPortAllocator(f.rdb, f.port, f.port)
			endCreation := pa.BeginCreation(f.owner)
			defer endCreation()
			if _, err := pa.AllocateWithinLimitForOwner(f.ctx, 1, f.owner); err != nil {
				t.Fatal(err)
			}
			plan := &lifecycleDockerPlan{list: []dockertypes.Container{}, inspectStatus: 404, removeStatus: 500}
			if mode == "long_creation" {
				plan.list = []dockertypes.Container{f.row()}
			}
			if mode == "metadata_after_list" {
				plan.inspectStatus = 200
				plan.onList = func() error {
					if err := f.seedData(); err != nil {
						return err
					}
					endCreation()
					return nil
				}
			}
			if mode == "metadata_before_orphan_release" {
				endCreation()
				plan.onInspect = f.seedData
			}
			docker := newLifecycleDocker(t, f, plan)
			r := NewReconciler(f.rdb, docker, nil, time.Minute, pa)
			r.reconcile(f.ctx)
			if plan.removes.Load() != 0 {
				t.Fatal("ongoing or newly persisted creation was removed")
			}
			if port, err := f.rdb.SIsMember(f.ctx, "allocated_ports", f.port).Result(); err != nil || !port {
				t.Fatal("creation race freed the reserved port")
			}
			if count, err := f.rdb.ZCard(f.ctx, "allocated_port_cooldowns").Result(); err != nil || count != 0 {
				t.Fatal("creation race scheduled orphan release")
			}
			if mode != "long_creation" {
				f.assertPreserved(t)
			}
		})
	}
}

func TestLifecycleCooldownWriteFailureIntegration(t *testing.T) {
	f := newOwnedLifecycleFixture(t)
	f.seed(t)
	plan := &lifecycleDockerPlan{stopStatus: 204, removeStatus: 204}
	docker := newLifecycleDocker(t, f, plan)
	if err := f.rdb.Set(f.ctx, "allocated_port_cooldowns", "owned-invalid-type", 0).Err(); err != nil {
		t.Fatal(err)
	}
	f.wrongCooldown = true
	w := NewTTLWatcher(f.rdb, docker, ctfcontainer.NewPortAllocator(f.rdb, f.port, f.port), nil, time.Second)
	w.cleanupInstance(f.ctx, f.instanceKey)
	f.assertPreserved(t)
	if err := f.rdb.Del(f.ctx, "allocated_port_cooldowns").Err(); err != nil {
		t.Fatal(err)
	}
}

// This models the state after the actual boundary/reuse flow in allocator tests;
// it is not a second claim that 30 seconds elapsed in this fixture.
func (f *ownedLifecycleFixture) seedReusedReservation(t *testing.T) (*ctfcontainer.PortAllocator, string, float64) {
	t.Helper()
	f.seed(t)
	current := f.owner + "_new"
	f.owners[current] = true
	if err := f.rdb.HSet(f.ctx, "allocated_port_owners", strconv.Itoa(f.port), current).Err(); err != nil {
		t.Fatal(err)
	}
	pa := ctfcontainer.NewPortAllocator(f.rdb, f.port, f.port)
	if err := pa.ReleaseOwned(f.ctx, f.port, current); err != nil {
		t.Fatal(err)
	}
	member := fmt.Sprintf("%d|%s", f.port, current)
	deadline, err := f.rdb.ZScore(f.ctx, "allocated_port_cooldowns", member).Result()
	if err != nil {
		t.Fatal(err)
	}
	return pa, member, deadline
}

func (f *ownedLifecycleFixture) assertOldMetadataRemovedWithoutChangingCurrent(t *testing.T, member string, deadline float64) {
	t.Helper()
	if exists, err := f.rdb.Exists(f.ctx, f.instanceKey).Result(); err != nil || exists != 0 {
		t.Fatal("confirmed old metadata still occupies capacity")
	}
	if retained, err := f.rdb.SIsMember(f.ctx, f.userKey, f.owner).Result(); err != nil || retained {
		t.Fatal("confirmed old owner membership still occupies capacity")
	}
	if owner, err := f.rdb.HGet(f.ctx, "allocated_port_owners", strconv.Itoa(f.port)).Result(); err != nil || owner != f.owner+"_new" {
		t.Fatal("old metadata cleanup changed the current reservation owner")
	}
	if score, err := f.rdb.ZScore(f.ctx, "allocated_port_cooldowns", member).Result(); err != nil || score != deadline {
		t.Fatal("old metadata cleanup changed the current first deadline")
	}
}

func TestConfirmedRemovedOldMetadataAfterReuseIntegration(t *testing.T) {
	for _, mode := range []string{"ttl", "reconciler"} {
		t.Run(mode, func(t *testing.T) {
			f := newOwnedLifecycleFixture(t)
			pa, member, deadline := f.seedReusedReservation(t)
			plan := &lifecycleDockerPlan{stopStatus: 404, removeStatus: 404, inspectStatus: 404, list: []dockertypes.Container{f.row()}}
			docker := newLifecycleDocker(t, f, plan)
			if mode == "ttl" {
				NewTTLWatcher(f.rdb, docker, pa, nil, time.Second).cleanupInstance(f.ctx, f.instanceKey)
			} else {
				NewReconciler(f.rdb, docker, nil, time.Minute, pa).reconcile(f.ctx)
			}
			f.assertOldMetadataRemovedWithoutChangingCurrent(t, member, deadline)
		})
	}
}

func TestReconcilerConflictInspectTransitionsIntegration(t *testing.T) {
	f := newOwnedLifecycleFixture(t)
	pa, member, _ := f.seedReusedReservation(t)
	now, err := f.rdb.Time(f.ctx).Result()
	if err != nil {
		t.Fatal(err)
	}
	// Controlled expiry exercises guarded recovery without another 30s wait.
	due := float64(now.UnixMilli() - 1)
	if err := f.rdb.ZAdd(f.ctx, "allocated_port_cooldowns", redis.Z{Score: due, Member: member}).Err(); err != nil {
		t.Fatal(err)
	}
	plan := &lifecycleDockerPlan{list: []dockertypes.Container{f.row()}, inspectStatus: 200}
	docker := newLifecycleDocker(t, f, plan)
	r := NewReconciler(f.rdb, docker, nil, time.Minute, pa)
	r.reconcile(f.ctx)
	if len(pa.PortConflicts()) == 0 {
		t.Fatal("verified conflicting Docker occupancy was not guarded")
	}
	if score, err := f.rdb.ZScore(f.ctx, "allocated_port_cooldowns", member).Result(); err != nil || score != due {
		t.Fatal("actual conflicting occupancy changed the different owner's deadline")
	}
	plan.inspectOverride.Store(500)
	plan.omitList.Store(true)
	r.reconcile(f.ctx)
	if len(pa.PortConflicts()) == 0 {
		t.Fatal("list omission or failed inspect cleared the pending conflict")
	}
	reaped, allocated := make(chan error, 1), make(chan error, 1)
	go func() { reaped <- pa.ReapCooldowns(f.ctx) }()
	go func() { _, err := pa.AllocateWithinLimitForOwner(f.ctx, 1, f.owner); allocated <- err }()
	reapErr, allocateErr := <-reaped, <-allocated
	if reapErr != nil {
		t.Fatal("guarded reaper unexpectedly failed")
	}
	if allocateErr == nil {
		t.Fatal("unknown occupancy was concurrently reallocated")
	}
	if score, err := f.rdb.ZScore(f.ctx, "allocated_port_cooldowns", member).Result(); err != nil || score != due {
		t.Fatal("unknown occupancy allowed concurrent deadline recovery")
	}
	plan.inspectOverride.Store(404)
	r.reconcile(f.ctx)
	if len(pa.PortConflicts()) != 0 {
		t.Fatal("latest NotFound did not resolve the observed conflict identity")
	}
	if reserved, err := f.rdb.SIsMember(f.ctx, "allocated_ports", f.port).Result(); err != nil || reserved {
		t.Fatal("resolved absence did not allow atomic expired cooldown recovery")
	}
	if exists, err := f.rdb.Exists(f.ctx, f.instanceKey).Result(); err != nil || exists != 0 {
		t.Fatal("resolved old metadata was not cleaned")
	}
	if count, err := f.rdb.ZCard(f.ctx, "allocated_port_cooldowns").Result(); err != nil || count != 0 {
		t.Fatal("resolved expired deadline was not recovered")
	}
}

func TestReconcilerCurrentActualOwnerKeepsFirstDeadlineGuardIntegration(t *testing.T) {
	f := newOwnedLifecycleFixture(t)
	f.seed(t)
	pa := ctfcontainer.NewPortAllocator(f.rdb, f.port, f.port)
	if err := pa.ObservePort(f.ctx, f.port, f.owner); err != nil {
		t.Fatal(err)
	}
	if err := pa.ReleaseOwned(f.ctx, f.port, f.owner); err != nil {
		t.Fatal(err)
	}
	member := fmt.Sprintf("%d|%s", f.port, f.owner)
	deadline, err := f.rdb.ZScore(f.ctx, "allocated_port_cooldowns", member).Result()
	if err != nil {
		t.Fatal(err)
	}
	pa.HoldPortConflict(ctfcontainer.PortConflict{Port: f.port, Owner: f.owner, ContainerID: f.data["container_id"]})
	plan := &lifecycleDockerPlan{list: []dockertypes.Container{f.row()}, inspectStatus: 200}
	r := NewReconciler(f.rdb, newLifecycleDocker(t, f, plan), nil, time.Minute, pa)
	r.reconcile(f.ctx)
	if len(pa.PortConflicts()) == 0 {
		t.Fatal("actual occupancy released an existing first-deadline guard")
	}
	if score, err := f.rdb.ZScore(f.ctx, "allocated_port_cooldowns", member).Result(); err != nil || score != deadline {
		t.Fatal("matching-owner actual occupancy changed the first deadline")
	}
	f.assertPreserved(t)
}

func TestStaleSameOwnerListAfterStopKeepsFirstDeadlineIntegration(t *testing.T) {
	f := newOwnedLifecycleFixture(t)
	f.seed(t)
	pa := ctfcontainer.NewPortAllocator(f.rdb, f.port, f.port)
	var manager atomic.Pointer[ctfcontainer.Manager]
	var firstDeadline atomic.Int64
	member := fmt.Sprintf("%d|%s", f.port, f.owner)
	plan := &lifecycleDockerPlan{list: []dockertypes.Container{f.row()}, stopStatus: 204, removeStatus: 204, inspectStatus: 404}
	plan.onList = func() error {
		current := manager.Load()
		if current == nil {
			return fmt.Errorf("owned manager was not initialized")
		}
		if err := current.StopInstance(f.ctx, f.owner, f.owner); err != nil {
			return err
		}
		deadline, err := f.rdb.ZScore(f.ctx, "allocated_port_cooldowns", member).Result()
		if err != nil {
			return err
		}
		firstDeadline.Store(int64(deadline))
		return nil
	}
	docker := newLifecycleDocker(t, f, plan)
	owned := ctfcontainer.NewManager(pa, f.rdb, nil, docker, "", "", "owned", 30, 1, 1, "", time.Second)
	owned.SetDynamicFlagRuntimeDir(t.TempDir())
	manager.Store(owned)
	NewReconciler(f.rdb, docker, nil, time.Minute, pa).reconcile(f.ctx)
	if firstDeadline.Load() == 0 || plan.removes.Load() != 1 {
		t.Fatal("Stop-before-stale-List ordering was not exercised exactly once")
	}
	if score, err := f.rdb.ZScore(f.ctx, "allocated_port_cooldowns", member).Result(); err != nil || score != float64(firstDeadline.Load()) {
		t.Fatal("stale same-owner List removed or extended the first deadline")
	}
	if exists, err := f.rdb.Exists(f.ctx, f.instanceKey).Result(); err != nil || exists != 0 {
		t.Fatal("stale List recreated stopped metadata")
	}
	if retained, err := f.rdb.SIsMember(f.ctx, f.userKey, f.owner).Result(); err != nil || retained {
		t.Fatal("stale List recreated stopped membership")
	}
	if len(pa.PortConflicts()) != 0 {
		t.Fatal("latest NotFound did not resolve stale same-owner observation")
	}
}
