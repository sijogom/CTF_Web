package container

import (
	"context"
	"errors"
	"fmt"
	"net"
	"os"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/redis/go-redis/v9"
)

type ownedRedisFixture struct {
	rdb           *redis.Client
	ctx           context.Context
	prefix        string
	ports         map[int]bool
	owners        map[string]bool
	keys          map[string]bool
	wrongCooldown bool
}

// Integration is opt-in and requires an independently identified, empty,
// exclusive loopback DB. Never flush a DB or clean an unrecognized resource.
func newOwnedRedisFixture(t *testing.T) *ownedRedisFixture {
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
	if err != nil || host != "127.0.0.1" || port == "" || os.Getenv("CTF_TEST_REDIS_DB") != "15" {
		t.Fatal("integration Redis is not an explicit loopback DB15")
	}
	nonce := os.Getenv("CTF_TEST_REDIS_NONCE")
	if len(nonce) != 36 || strings.ContainsAny(nonce, "|\r\n\x00") {
		t.Fatal("integration ownership nonce missing")
	}
	pid := os.Getenv("CTF_TEST_REDIS_PROCESS_ID")
	processID, err := strconv.Atoi(pid)
	runID := os.Getenv("CTF_TEST_REDIS_RUN_ID")
	if err != nil || processID <= 0 || len(runID) != 40 || os.Getenv("CTF_GO_LIFECYCLE_REDIS_PASSWORD") == "" {
		t.Fatal("integration process binding missing")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 40*time.Second)
	rdb := redis.NewClient(&redis.Options{Addr: address, DB: 15, Password: os.Getenv("CTF_GO_LIFECYCLE_REDIS_PASSWORD"), PoolSize: 1, MaxRetries: -1, DialTimeout: time.Second, ReadTimeout: time.Second, WriteTimeout: time.Second})
	t.Cleanup(func() { cancel(); rdb.Close() })
	info, err := rdb.Info(ctx, "server").Result()
	if err != nil {
		t.Fatal("cannot read the identified integration Redis")
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
		t.Fatal("integration DB is not empty; no writes performed")
	}
	clientID, err := rdb.Do(ctx, "CLIENT", "ID").Int64()
	if err != nil {
		t.Fatal("cannot bind the integration client")
	}
	clients, err := rdb.ClientList(ctx).Result()
	if err != nil {
		t.Fatal("cannot verify exclusive integration clients")
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
	f := &ownedRedisFixture{rdb: rdb, ctx: ctx, prefix: strings.ReplaceAll(nonce, "-", ""), ports: map[int]bool{}, owners: map[string]bool{"": true}, keys: map[string]bool{}}
	t.Cleanup(func() { f.cleanup(t) })
	return f
}

func (f *ownedRedisFixture) owner(label string) string {
	owner := "inst_" + f.prefix + "_" + label
	f.owners[owner] = true
	return owner
}

func (f *ownedRedisFixture) cleanup(t *testing.T) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	keys, err := f.rdb.Keys(ctx, "*").Result()
	if err != nil {
		t.Error("cannot inspect owned integration cleanup")
		return
	}
	for _, key := range keys {
		if key != allocatedPortsKey && key != allocatedPortOwnersKey && key != allocatedPortCooldownsKey && !f.keys[key] {
			t.Error("unknown integration key preserved")
			return
		}
	}
	ports, err := f.rdb.SMembers(ctx, allocatedPortsKey).Result()
	if err != nil {
		t.Error("owned port set cleanup failed")
		return
	}
	for _, value := range ports {
		port, err := strconv.Atoi(value)
		if err != nil || !f.ports[port] {
			t.Error("unknown port preserved")
			return
		}
	}
	owners, err := f.rdb.HGetAll(ctx, allocatedPortOwnersKey).Result()
	if err != nil {
		t.Error("owned reservation cleanup failed")
		return
	}
	for value, owner := range owners {
		port, err := strconv.Atoi(value)
		if err != nil || !f.ports[port] || !f.owners[owner] {
			t.Error("unknown port owner preserved")
			return
		}
	}
	if kind, err := f.rdb.Type(ctx, allocatedPortCooldownsKey).Result(); err != nil {
		t.Error("cooldown type inspection failed")
		return
	} else if kind == "string" && f.wrongCooldown {
		value, err := f.rdb.Get(ctx, allocatedPortCooldownsKey).Result()
		if err != nil || value != "owned-invalid-type" {
			t.Error("unknown wrong-type cooldown preserved")
			return
		}
		if err := f.rdb.Del(ctx, allocatedPortCooldownsKey).Err(); err != nil {
			t.Error("owned wrong-type cooldown cleanup failed")
			return
		}
	}
	members, err := f.rdb.ZRange(ctx, allocatedPortCooldownsKey, 0, -1).Result()
	if err != nil {
		t.Error("owned cooldown cleanup failed")
		return
	}
	for _, member := range members {
		parts := strings.SplitN(member, "|", 2)
		if len(parts) != 2 {
			t.Error("unknown cooldown preserved")
			return
		}
		port, err := strconv.Atoi(parts[0])
		if err != nil || !f.ports[port] || !f.owners[parts[1]] {
			t.Error("unknown cooldown owner preserved")
			return
		}
	}
	for _, key := range keys {
		if f.keys[key] {
			if err := f.rdb.Del(ctx, key).Err(); err != nil {
				t.Error("owned metadata cleanup failed")
			}
		}
	}
	for value := range f.ports {
		if err := f.rdb.SRem(ctx, allocatedPortsKey, value).Err(); err != nil {
			t.Error("owned port cleanup failed")
		}
		if err := f.rdb.HDel(ctx, allocatedPortOwnersKey, strconv.Itoa(value)).Err(); err != nil {
			t.Error("owned owner cleanup failed")
		}
	}
	for _, member := range members {
		if err := f.rdb.ZRem(ctx, allocatedPortCooldownsKey, member).Err(); err != nil {
			t.Error("owned cooldown cleanup failed")
		}
	}
	if size, err := f.rdb.DBSize(ctx).Result(); err != nil || size != 0 {
		t.Error("integration DB did not return to its empty baseline")
	}
}

func (f *ownedRedisFixture) seedInstance(t *testing.T, port int, owner string) map[string]string {
	t.Helper()
	f.ports[port] = true
	f.owners[owner] = true
	key, userKey := "instance:"+owner, "user_instances:"+owner
	f.keys[key], f.keys[userKey] = true, true
	data := map[string]string{"container_id": "docker-" + owner, "user_id": owner, "port": strconv.Itoa(port), "expires_at": strconv.FormatInt(time.Now().Add(time.Hour).Unix(), 10), "dynamic_flag_enabled": "false"}
	if err := f.rdb.HSet(f.ctx, key, data).Err(); err != nil {
		t.Fatal("owned instance seed failed")
	}
	if err := f.rdb.SAdd(f.ctx, userKey, owner).Err(); err != nil {
		t.Fatal("owned user seed failed")
	}
	if err := f.rdb.SAdd(f.ctx, allocatedPortsKey, port).Err(); err != nil {
		t.Fatal("owned port seed failed")
	}
	return data
}

func redisMillis(t *testing.T, f *ownedRedisFixture) int64 {
	t.Helper()
	now, err := f.rdb.Time(f.ctx).Result()
	if err != nil {
		t.Fatal("Redis time observation failed")
	}
	return now.UnixMilli()
}

func TestPortAllocatorThirtySecondCooldownIntegration(t *testing.T) {
	f := newOwnedRedisFixture(t)
	const port = 36001
	f.ports[port] = true
	ownerA, ownerB := f.owner("cooldown_a"), f.owner("cooldown_b")
	pa := NewPortAllocator(f.rdb, port, port)
	if got, err := pa.AllocateWithinLimitForOwner(f.ctx, 1, ownerA); err != nil || got != port {
		t.Fatal("first reservation failed")
	}
	oldData := f.seedInstance(t, port, ownerA)
	if err := f.rdb.Del(f.ctx, "user_instances:"+ownerA).Err(); err != nil {
		t.Fatal(err)
	}
	if err := f.rdb.Set(f.ctx, "user_instances:"+ownerA, "owned-delete-failure", 0).Err(); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(f.ctx)
	before := redisMillis(t, f)
	if err := FinalizeRemovedInstance(ctx, f.rdb, pa, ownerA, oldData); err == nil {
		t.Fatal("injected metadata deletion failure was not exercised")
	}
	if exists, err := f.rdb.Exists(f.ctx, "instance:"+ownerA).Result(); err != nil || exists != 1 {
		t.Fatal("failed metadata deletion lost the recovery record")
	}
	cancel()
	after := redisMillis(t, f)
	member := fmt.Sprintf("%d|%s", port, ownerA)
	deadline, err := f.rdb.ZScore(f.ctx, allocatedPortCooldownsKey, member).Result()
	if err != nil || deadline < float64(before+30000) || deadline > float64(after+30000) {
		t.Fatal("cooldown is not exactly 30 seconds from Redis TIME")
	}
	if err := pa.ReleaseOwned(f.ctx, port, ownerA); err != nil {
		t.Fatal(err)
	}
	if repeated, err := f.rdb.ZScore(f.ctx, allocatedPortCooldownsKey, member).Result(); err != nil || repeated != deadline {
		t.Fatal("duplicate release changed the first deadline")
	}
	if err := pa.ReleaseOwned(ctx, port, ownerA); err == nil {
		t.Fatal("canceled write must not report persisted success")
	}
	// A new allocator instance must retain the persisted deadline without the
	// original allocator or any release goroutine remaining alive.
	pa = NewPortAllocator(f.rdb, port, port)
	end := time.Now().Add(34 * time.Second)
	for {
		observedBefore := redisMillis(t, f)
		got, err := pa.AllocateWithinLimitForOwner(f.ctx, 1, ownerB)
		observedAfter := redisMillis(t, f)
		if err == nil {
			if got != port || float64(observedAfter) < deadline {
				t.Fatal("port was reused before the actual Redis deadline")
			}
			break
		}
		var capacity *GlobalCapacityError
		if !errors.As(err, &capacity) || float64(observedBefore) >= deadline || time.Now().After(end) {
			t.Fatalf("cooldown failed to reap at its actual deadline: %v", err)
		}
		if reserved, err := f.rdb.SIsMember(f.ctx, allocatedPortsKey, port).Result(); err != nil || !reserved {
			t.Fatal("cooldown no longer counts toward capacity")
		}
		time.Sleep(100 * time.Millisecond)
	}
	var mismatch *ReservationOwnerMismatchError
	if err := pa.ReleaseOwned(f.ctx, port, ownerA); !errors.As(err, &mismatch) {
		t.Fatal("late previous owner release did not retain its typed failure")
	}
	if _, err := pa.ReleaseOrphan(f.ctx, PortReservation{Port: port, Owner: ownerA}); err == nil {
		t.Fatal("stale reconciliation reservation was accepted")
	}
	if owner, err := f.rdb.HGet(f.ctx, allocatedPortOwnersKey, strconv.Itoa(port)).Result(); err != nil || owner != ownerB {
		t.Fatal("late release changed the new reservation")
	}
	if err := pa.ReleaseOwned(f.ctx, port, ownerB); err != nil {
		t.Fatal(err)
	}
	newMember := fmt.Sprintf("%d|%s", port, ownerB)
	newDeadline, err := f.rdb.ZScore(f.ctx, allocatedPortCooldownsKey, newMember).Result()
	if err != nil {
		t.Fatal(err)
	}
	if err := FinalizeRemovedInstance(f.ctx, f.rdb, pa, ownerA, oldData); err == nil {
		t.Fatal("genuine metadata write failure was swallowed after an owner mismatch")
	}
	if exists, err := f.rdb.Exists(f.ctx, "instance:"+ownerA).Result(); err != nil || exists != 1 {
		t.Fatal("failed old metadata retry lost recovery state")
	}
	if score, err := f.rdb.ZScore(f.ctx, allocatedPortCooldownsKey, newMember).Result(); err != nil || score != newDeadline {
		t.Fatal("failed old metadata retry changed the new deadline")
	}
	if err := f.rdb.Del(f.ctx, "user_instances:"+ownerA).Err(); err != nil {
		t.Fatal(err)
	}
	if err := f.rdb.SAdd(f.ctx, "user_instances:"+ownerA, ownerA).Err(); err != nil {
		t.Fatal(err)
	}
	if err := FinalizeRemovedInstance(f.ctx, f.rdb, pa, ownerA, oldData); err != nil {
		t.Fatal("confirmed removed metadata could not recover after port reuse")
	}
	if exists, err := f.rdb.Exists(f.ctx, "instance:"+ownerA).Result(); err != nil || exists != 0 {
		t.Fatal("old recovery metadata still occupies capacity")
	}
	if retained, err := f.rdb.SIsMember(f.ctx, "user_instances:"+ownerA, ownerA).Result(); err != nil || retained {
		t.Fatal("old owner membership still occupies capacity")
	}
	if score, err := f.rdb.ZScore(f.ctx, allocatedPortCooldownsKey, newMember).Result(); err != nil || score != newDeadline {
		t.Fatal("old metadata retry changed the new owner's first deadline")
	}
	if owner, err := f.rdb.HGet(f.ctx, allocatedPortOwnersKey, strconv.Itoa(port)).Result(); err != nil || owner != ownerB {
		t.Fatal("old metadata retry changed the new owner")
	}
	t.Log("actual Redis TIME 30-second boundary and recovery after deletion failure/reuse were verified without changing the new reservation")
}

func TestPortAllocatorConcurrentCapacityIntegration(t *testing.T) {
	f := newOwnedRedisFixture(t)
	pa := NewPortAllocator(f.rdb, 36002, 36004)
	for port := 36002; port <= 36004; port++ {
		f.ports[port] = true
	}
	owners := make([]string, 8)
	for i := range owners {
		owners[i] = f.owner(fmt.Sprintf("concurrent_%d", i))
	}
	var wg sync.WaitGroup
	results := make(chan error, len(owners))
	for _, owner := range owners {
		wg.Add(1)
		go func(owner string) {
			defer wg.Done()
			_, err := pa.AllocateWithinLimitForOwner(f.ctx, 2, owner)
			results <- err
		}(owner)
	}
	wg.Wait()
	close(results)
	successes := 0
	for err := range results {
		if err == nil {
			successes++
			continue
		}
		var capacity *GlobalCapacityError
		if !errors.As(err, &capacity) {
			t.Fatal(err)
		}
	}
	if successes != 2 {
		t.Fatalf("capacity reservation successes=%d, want2", successes)
	}
	if count, err := f.rdb.SCard(f.ctx, allocatedPortsKey).Result(); err != nil || count != 2 {
		t.Fatal("atomic global capacity was exceeded")
	}
}

func TestPortAllocatorOrphanAndLegacyGuardsIntegration(t *testing.T) {
	f := newOwnedRedisFixture(t)
	const port = 36005
	f.ports[port] = true
	owner := f.owner("creating")
	pa := NewPortAllocator(f.rdb, port, port)
	end := pa.BeginCreation(owner)
	if _, err := pa.AllocateWithinLimitForOwner(f.ctx, 1, owner); err != nil {
		t.Fatal(err)
	}
	reservation := PortReservation{Port: port, Owner: owner}
	if changed, err := pa.ReleaseOrphan(f.ctx, reservation); err != nil || changed {
		t.Fatal("creating reservation was released")
	}
	f.seedInstance(t, port, owner)
	end()
	end()
	if pa.IsCreating(owner) {
		t.Fatal("creation protection did not end idempotently")
	}
	if changed, err := pa.ReleaseOrphan(f.ctx, reservation); err != nil || changed {
		t.Fatal("metadata created after the snapshot was ignored")
	}
	if err := pa.ReleaseOwned(f.ctx, port, owner); err != nil {
		t.Fatal(err)
	}
	memberBefore := fmt.Sprintf("%d|%s", port, owner)
	firstDeadline, err := f.rdb.ZScore(f.ctx, allocatedPortCooldownsKey, memberBefore).Result()
	if err != nil {
		t.Fatal(err)
	}
	var pending *CooldownObservationPendingError
	if err := pa.ObservePort(f.ctx, port, owner, "docker-"+owner); !errors.As(err, &pending) {
		t.Fatal("existing cooldown was not deferred for latest occupancy confirmation")
	}
	if score, err := f.rdb.ZScore(f.ctx, allocatedPortCooldownsKey, memberBefore).Result(); err != nil || score != firstDeadline {
		t.Fatal("observation changed the existing first deadline")
	}
	if err := pa.Release(f.ctx, port); err == nil {
		t.Fatal("unowned legacy caller released a new owned reservation")
	}
	if err := f.rdb.HDel(f.ctx, allocatedPortOwnersKey, strconv.Itoa(port)).Err(); err != nil {
		t.Fatal(err)
	}
	if err := pa.ReleaseOwned(f.ctx, port, owner); err != nil {
		t.Fatal("legacy instance could not adopt its own reservation")
	}
	other := f.owner("different")
	member := fmt.Sprintf("%d|%s", port, owner)
	deadline, err := f.rdb.ZScore(f.ctx, allocatedPortCooldownsKey, member).Result()
	if err != nil {
		t.Fatal(err)
	}
	var mismatch *ReservationOwnerMismatchError
	if err := pa.ObservePort(f.ctx, port, other); !errors.As(err, &mismatch) {
		t.Fatal("different observed owner did not report a typed mismatch")
	}
	if got, err := f.rdb.HGet(f.ctx, allocatedPortOwnersKey, strconv.Itoa(port)).Result(); err != nil || got != owner {
		t.Fatal("observation overwrote another reservation owner")
	}
	if score, err := f.rdb.ZScore(f.ctx, allocatedPortCooldownsKey, member).Result(); err != nil || score != deadline {
		t.Fatal("stale observation changed the current owner's cooldown")
	}
}

func TestLegacyCooldownAdoptionPreservesFirstDeadlineIntegration(t *testing.T) {
	f := newOwnedRedisFixture(t)
	owner := f.owner("legacy_deadline")
	f.seedInstance(t, 36016, owner)
	pa := NewPortAllocator(f.rdb, 36016, 36016)
	if err := pa.Release(f.ctx, 36016); err != nil {
		t.Fatal(err)
	}
	legacy := "36016|"
	deadline, err := f.rdb.ZScore(f.ctx, allocatedPortCooldownsKey, legacy).Result()
	if err != nil {
		t.Fatal(err)
	}
	var pending *CooldownObservationPendingError
	if err := pa.ObservePort(f.ctx, 36016, owner, "docker-"+owner); !errors.As(err, &pending) {
		t.Fatal("legacy pending cooldown was adopted before removal confirmation")
	}
	if exists, err := f.rdb.HExists(f.ctx, allocatedPortOwnersKey, "36016").Result(); err != nil || exists {
		t.Fatal("pending legacy observation changed reservation identity")
	}
	if err := pa.ReleaseOwned(f.ctx, 36016, owner); err != nil {
		t.Fatal(err)
	}
	if score, err := f.rdb.ZScore(f.ctx, allocatedPortCooldownsKey, "36016|"+owner).Result(); err != nil || score != deadline {
		t.Fatal("legacy adoption reset the original deadline")
	}
	if _, err := f.rdb.ZScore(f.ctx, allocatedPortCooldownsKey, legacy).Result(); err != redis.Nil {
		t.Fatal("legacy deadline was duplicated instead of preserving its identity transition")
	}
}

func TestObservedPortConflictGuardsConcurrentReapIntegration(t *testing.T) {
	f := newOwnedRedisFixture(t)
	const port = 36014
	f.ports[port] = true
	current, stale := f.owner("conflict_current"), f.owner("conflict_stale")
	pa := NewPortAllocator(f.rdb, port, port)
	if _, err := pa.AllocateWithinLimitForOwner(f.ctx, 1, current); err != nil {
		t.Fatal(err)
	}
	if err := pa.ReleaseOwned(f.ctx, port, current); err != nil {
		t.Fatal(err)
	}
	member := fmt.Sprintf("%d|%s", port, current)
	// This controlled due-state checks the reaper guard, not elapsed duration.
	due := float64(redisMillis(t, f) - 1)
	if err := f.rdb.ZAdd(f.ctx, allocatedPortCooldownsKey, redis.Z{Score: due, Member: member}).Err(); err != nil {
		t.Fatal(err)
	}
	conflict := PortConflict{Port: port, Owner: stale, ContainerID: "docker-" + stale}
	var mismatch *ReservationOwnerMismatchError
	if err := pa.ObservePort(f.ctx, port, stale, conflict.ContainerID); !errors.As(err, &mismatch) {
		t.Fatal("conflict observation did not report owner mismatch")
	}
	var wg sync.WaitGroup
	results := make(chan error, 4)
	for i := 0; i < 4; i++ {
		wg.Add(1)
		go func() { defer wg.Done(); results <- pa.ReapCooldowns(f.ctx) }()
	}
	wg.Wait()
	close(results)
	for err := range results {
		if err != nil {
			t.Fatal(err)
		}
	}
	if _, err := pa.AllocateWithinLimitForOwner(f.ctx, 1, f.owner("conflict_next")); err == nil {
		t.Fatal("conflicting occupancy was reallocated")
	}
	if score, err := f.rdb.ZScore(f.ctx, allocatedPortCooldownsKey, member).Result(); err != nil || score != due {
		t.Fatal("pending conflict changed the first deadline")
	}
	pa.ResolvePortConflict(conflict)
	if err := pa.ReapCooldowns(f.ctx); err != nil {
		t.Fatal(err)
	}
	if reserved, err := f.rdb.SIsMember(f.ctx, allocatedPortsKey, port).Result(); err != nil || reserved {
		t.Fatal("resolved conflict did not allow atomic expired cooldown recovery")
	}
}

func TestPortAllocatorRedisFailureAndMetadataCASIntegration(t *testing.T) {
	f := newOwnedRedisFixture(t)
	owner := f.owner("failure")
	data := f.seedInstance(t, 36006, owner)
	pa := NewPortAllocator(f.rdb, 36006, 36006)
	if err := f.rdb.Set(f.ctx, allocatedPortCooldownsKey, "owned-invalid-type", 0).Err(); err != nil {
		t.Fatal(err)
	}
	f.wrongCooldown = true
	if err := pa.ReleaseOwned(f.ctx, 36006, owner); err == nil {
		t.Fatal("failed cooldown storage reported success")
	}
	if _, err := pa.AllocateWithinLimitForOwner(f.ctx, 1, f.owner("another")); err == nil {
		t.Fatal("invalid Redis state accepted an allocation")
	}
	if exists, err := f.rdb.Exists(f.ctx, "instance:"+owner).Result(); err != nil || exists != 1 {
		t.Fatal("failed release deleted metadata")
	}
	if reserved, err := f.rdb.SIsMember(f.ctx, allocatedPortsKey, 36006).Result(); err != nil || !reserved {
		t.Fatal("failed release freed the occupied port")
	}
	// This wrong-type key was created by this test after the empty DB gate.
	if err := f.rdb.Del(f.ctx, allocatedPortCooldownsKey).Err(); err != nil {
		t.Fatal(err)
	}
	if err := f.rdb.HSet(f.ctx, "instance:"+owner, "container_id", "replacement").Err(); err != nil {
		t.Fatal(err)
	}
	if err := DeleteInstanceRecord(f.ctx, f.rdb, owner, data); err == nil {
		t.Fatal("stale cleanup deleted replacement metadata")
	}
	if retained, err := f.rdb.SIsMember(f.ctx, "user_instances:"+owner, owner).Result(); err != nil || !retained {
		t.Fatal("metadata CAS failure deleted owner membership")
	}
}
