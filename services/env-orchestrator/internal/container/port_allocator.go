// Package container는 챌린지 컨테이너 관리를 담당한다.
package container

import (
	"context"
	crand "crypto/rand"
	"fmt"
	"log"
	"math/big"
	"net/http"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/redis/go-redis/v9"
)

const (
	allocatedPortsKey         = "allocated_ports"
	allocatedPortOwnersKey    = "allocated_port_owners"
	allocatedPortCooldownsKey = "allocated_port_cooldowns"
	maxRandomRetries          = 10
	portCooldownSec           = 30
)

const (
	portAllocated        int64 = 1
	globalCapacityFull   int64 = 0
	portAlreadyAllocated int64 = -1
)

// allocatePortWithLimitScript는 포트 예약과 전체 환경 상한 확인을 하나의 Redis
// 원자 연산으로 처리한다. CreateInstance 요청이 동시에 들어와도 최대치보다 많은
// 포트를 예약하지 않게 한다.
//
// maxContainers가 0이면 기존 Allocate와 같은 무제한 포트 풀 동작을 유지한다.
const portStateScript = `
local function requireType(key, expected)
  local kind = redis.call("TYPE", key).ok
  if kind ~= "none" and kind ~= expected then
    error("invalid port state type")
  end
end
requireType(KEYS[1], "set")
requireType(KEYS[2], "hash")
requireType(KEYS[3], "zset")
local function nowMillis()
  local now = redis.call("TIME")
  return tonumber(now[1]) * 1000 + math.floor(tonumber(now[2]) / 1000)
end
local function ownerOf(port)
  return redis.call("HGET", KEYS[2], port) or ""
end
local function reap(firstProtectedArgument)
  local protected = {}
  for index = firstProtectedArgument, #ARGV do protected[ARGV[index]] = true end
  for _, member in ipairs(redis.call("ZRANGEBYSCORE", KEYS[3], "-inf", nowMillis())) do
    local port, owner = string.match(member, "^(%d+)|(.*)$")
    if not port or not protected[port] then
      if port and ownerOf(port) == owner then
        redis.call("SREM", KEYS[1], port)
        redis.call("HDEL", KEYS[2], port)
      end
      redis.call("ZREM", KEYS[3], member)
    end
  end
  return protected
end
`

var allocatePortWithLimitScript = redis.NewScript(portStateScript + `
local port = ARGV[1]
local maxContainers = tonumber(ARGV[2])
local owner = ARGV[3]
local protected = reap(4)

if maxContainers > 0 and redis.call("SCARD", KEYS[1]) >= maxContainers then
  return 0
end
if protected[port] then return -1 end

if redis.call("SADD", KEYS[1], port) == 1 then
  if owner ~= "" then
    redis.call("HSET", KEYS[2], port, owner)
  else
    redis.call("HDEL", KEYS[2], port)
  end
  return 1
end

return -1
`)

var reapPortCooldownsScript = redis.NewScript(portStateScript + `reap(1); return 1`)

var releasePortScript = redis.NewScript(portStateScript + `
local port, owner = ARGV[1], ARGV[2]
if redis.call("SISMEMBER", KEYS[1], port) == 0 then return 0 end
local current = ownerOf(port)
if current ~= owner then
  -- Existing instances predate reservation ownership. Adopt only their own
  -- still-current metadata, never a different reservation's owner.
  if current ~= "" or owner == "" or ARGV[4] == "orphan" then return -1 end
  if redis.call("HGET", KEYS[4], "port") ~= port then return -1 end
  local legacyMember = port .. "|"
  local legacyDeadline = redis.call("ZSCORE", KEYS[3], legacyMember)
  redis.call("HSET", KEYS[2], port, owner)
  if legacyDeadline then
    local member = port .. "|" .. owner
    local ownedDeadline = redis.call("ZSCORE", KEYS[3], member)
    local firstDeadline = tonumber(legacyDeadline)
    if ownedDeadline then firstDeadline = math.min(firstDeadline, tonumber(ownedDeadline)) end
    redis.call("ZREM", KEYS[3], legacyMember)
    redis.call("ZADD", KEYS[3], firstDeadline, member)
  end
end
if ARGV[4] == "orphan" and owner ~= "" and redis.call("EXISTS", KEYS[4]) == 1 then
  return 0
end
return redis.call("ZADD", KEYS[3], "NX", nowMillis() + tonumber(ARGV[3]), port .. "|" .. owner)
`)

var observePortScript = redis.NewScript(portStateScript + `
local port, owner = ARGV[1], ARGV[2]
local current = ownerOf(port)
if current ~= "" and current ~= owner then return -1 end
if redis.call("ZSCORE", KEYS[3], port .. "|" .. current) or
   redis.call("ZSCORE", KEYS[3], port .. "|" .. owner) then return 2 end
redis.call("SADD", KEYS[1], port)
if current == "" and owner ~= "" then
  redis.call("HSET", KEYS[2], port, owner)
  current = owner
end
return 1
`)

var snapshotPortReservationsScript = redis.NewScript(portStateScript + `
local result = {}
for _, port in ipairs(redis.call("SMEMBERS", KEYS[1])) do
  table.insert(result, port)
  table.insert(result, ownerOf(port))
end
return result
`)

// PortReservation은 재사용되는 포트 번호가 아닌 관측한 예약 identity에 결속한다.
// 빈 owner는 기존 소유자 정보가 없는 예약을 뜻한다.
type PortReservation struct {
	Port  int
	Owner string
}

// PortConflict는 최신 조회로 해소할 때까지 보류하는 실제/미확인 Docker identity다.
type PortConflict struct {
	Port        int
	Owner       string
	ContainerID string
}

// GlobalCapacityError는 관리자 설정으로 정한 전체 환경 수 상한에 도달했음을
// 호출자에게 구분해서 알린다. 내부 포트·다른 사용자의 사용 현황은 노출하지 않는다.
type GlobalCapacityError struct {
	Limit int
}

func (e *GlobalCapacityError) Error() string {
	return fmt.Sprintf("현재 전체 실습 환경 수가 관리자 제한(%d개)에 도달했습니다. 잠시 후 다시 시도하세요", e.Limit)
}

// ReservationOwnerMismatchError는 실패한 Redis 쓰기와 이전 예약의 늦은 정리를 구분한다.
type ReservationOwnerMismatchError struct{}

func (*ReservationOwnerMismatchError) Error() string {
	return "포트 예약 소유자가 변경되었습니다"
}

// CooldownObservationPendingError는 기존 최초 deadline을 바꾸지 않고 최신 점유를 확인한다.
type CooldownObservationPendingError struct{}

func (*CooldownObservationPendingError) Error() string {
	return "기존 포트 쿨다운의 점유 확인이 필요합니다"
}

// PortAllocator는 Redis SET 기반으로 포트를 원자적으로 할당/해제한다.
type PortAllocator struct {
	rdb        *redis.Client
	portMin    int
	portMax    int
	creationMu sync.RWMutex
	creating   map[string]int
	conflicts  map[int]map[string]string
}

// NewPortAllocator는 새로운 포트 할당기를 생성한다.
// portMin은 1024 이상, portMax는 65535 이하여야 하며, portMin <= portMax이어야 한다.
func NewPortAllocator(rdb *redis.Client, portMin, portMax int) *PortAllocator {
	if portMin < 1024 || portMax > 65535 || portMin > portMax {
		log.Fatalf("잘못된 포트 범위: %d~%d (허용: 1024~65535, min <= max)", portMin, portMax)
	}
	return &PortAllocator{rdb: rdb, portMin: portMin, portMax: portMax, creating: make(map[string]int)}
}

// Allocate는 사용 가능한 포트를 원자적으로 할당한다.
// 1단계: 랜덤 선택 (최대 10회)
// 2단계: 순차 스캔 (실패 시)
// 3단계: 에러 반환 (포트 풀 소진)
func (pa *PortAllocator) Allocate(ctx context.Context) (int, error) {
	return pa.allocate(ctx, 0, "")
}

// AllocateWithinLimit는 포트 풀 예약과 전체 동시 환경 수 상한을 원자적으로 적용한다.
// 상한에는 실행 중인 환경뿐 아니라 생성 중인 환경과 포트 재사용 쿨다운도 포함된다.
// 이는 종료 직후 같은 HostPort를 재사용하는 경쟁 상태를 막기 위한 안전한 보수적 계산이다.
func (pa *PortAllocator) AllocateWithinLimit(ctx context.Context, maxContainers int) (int, error) {
	if maxContainers < 1 {
		return 0, fmt.Errorf("전체 실습 환경 상한은 1 이상이어야 합니다")
	}
	return pa.allocate(ctx, maxContainers, "")
}

// AllocateWithinLimitForOwner는 늦은 정리가 같은 포트를 재사용한 새 인스턴스를
// 해제하지 못하도록 예약 소유자를 함께 저장한다.
func (pa *PortAllocator) AllocateWithinLimitForOwner(ctx context.Context, maxContainers int, owner string) (int, error) {
	if maxContainers < 1 || owner == "" || !ValidPortOwner(owner) {
		return 0, fmt.Errorf("포트 예약의 상한 또는 소유자가 올바르지 않습니다")
	}
	return pa.allocate(ctx, maxContainers, owner)
}

func (pa *PortAllocator) allocate(ctx context.Context, maxContainers int, owner string) (int, error) {
	// 1단계: 랜덤 시도
	portRange := int64(pa.portMax - pa.portMin + 1)
	for i := 0; i < maxRandomRetries; i++ {
		n, err := crand.Int(crand.Reader, big.NewInt(portRange))
		if err != nil {
			return 0, fmt.Errorf("난수 생성 오류: %w", err)
		}
		port := pa.portMin + int(n.Int64())
		result, err := pa.tryAllocate(ctx, port, maxContainers, owner)
		if err != nil {
			return 0, err
		}
		if result == portAllocated {
			return port, nil
		}
		if result == globalCapacityFull {
			return 0, &GlobalCapacityError{Limit: maxContainers}
		}
	}

	// 2단계: 순차 스캔
	for port := pa.portMin; port <= pa.portMax; port++ {
		result, err := pa.tryAllocate(ctx, port, maxContainers, owner)
		if err != nil {
			return 0, err
		}
		if result == portAllocated {
			return port, nil
		}
		if result == globalCapacityFull {
			return 0, &GlobalCapacityError{Limit: maxContainers}
		}
	}

	// 포트 풀 소진
	go pa.notifyPortPoolExhausted()
	return 0, fmt.Errorf("포트 풀 소진: %d~%d 범위 내 사용 가능한 포트 없음", pa.portMin, pa.portMax)
}

func (pa *PortAllocator) tryAllocate(ctx context.Context, port, maxContainers int, owner string) (int64, error) {
	pa.creationMu.RLock()
	defer pa.creationMu.RUnlock()
	arguments := []interface{}{port, maxContainers, owner}
	arguments = append(arguments, pa.protectedPortArgumentsLocked()...)
	result, err := allocatePortWithLimitScript.Run(
		ctx,
		pa.rdb,
		pa.portStateKeys(),
		arguments...,
	).Int64()
	if err != nil {
		return 0, fmt.Errorf("Redis 오류: %w", err)
	}
	if result != portAllocated && result != globalCapacityFull && result != portAlreadyAllocated {
		return 0, fmt.Errorf("포트 할당 응답이 올바르지 않습니다")
	}
	return result, nil
}

// Release는 기존 소유자 없는 예약 API를 유지한다. 취소된 요청은 영속적인
// 30초 쿨다운을 단축하지 않으며 별도의 해제 고루틴을 만들지 않는다.
func (pa *PortAllocator) Release(ctx context.Context, port int) error {
	return pa.ReleaseOwned(ctx, port, "")
}

// ValidPortOwner는 복합 쿨다운 member의 경계를 보존한다. 빈 값은 기존 예약이나
// identity를 확인하지 못한 Docker 리소스를 뜻한다.
func ValidPortOwner(owner string) bool {
	return !strings.ContainsAny(owner, "|\r\n\x00")
}

func (pa *PortAllocator) portStateKeys() []string {
	return []string{allocatedPortsKey, allocatedPortOwnersKey, allocatedPortCooldownsKey}
}

func (pa *PortAllocator) ReleaseOwned(ctx context.Context, port int, owner string) error {
	_, err := pa.scheduleRelease(ctx, PortReservation{Port: port, Owner: owner}, false)
	return err
}

func (pa *PortAllocator) scheduleRelease(ctx context.Context, reservation PortReservation, orphan bool) (bool, error) {
	if err := ctx.Err(); err != nil {
		return false, err
	}
	if reservation.Port < 1 || reservation.Port > 65535 || !ValidPortOwner(reservation.Owner) {
		return false, fmt.Errorf("포트 예약이 올바르지 않습니다")
	}
	mode := "confirmed"
	if orphan {
		mode = "orphan"
	}
	keys := append(pa.portStateKeys(), "instance:"+reservation.Owner)
	result, err := releasePortScript.Run(ctx, pa.rdb, keys, reservation.Port, reservation.Owner, portCooldownSec*1000, mode).Int64()
	if err != nil {
		return false, fmt.Errorf("포트 쿨다운 저장 실패: %w", err)
	}
	if result < 0 {
		return false, &ReservationOwnerMismatchError{}
	}
	return result == 1, nil
}

// BeginCreation은 reconciler와 공유하며 메타데이터 저장 또는 실패 정리를 마친 뒤
// 보호를 해제한다.
func (pa *PortAllocator) BeginCreation(owner string) func() {
	pa.creationMu.Lock()
	if pa.creating == nil {
		pa.creating = make(map[string]int)
	}
	pa.creating[owner]++
	pa.creationMu.Unlock()
	var once sync.Once
	return func() {
		once.Do(func() {
			pa.creationMu.Lock()
			defer pa.creationMu.Unlock()
			pa.creating[owner]--
			if pa.creating[owner] == 0 {
				delete(pa.creating, owner)
			}
		})
	}
}

func (pa *PortAllocator) IsCreating(owner string) bool {
	pa.creationMu.RLock()
	defer pa.creationMu.RUnlock()
	return pa.creating[owner] > 0
}

func (pa *PortAllocator) ReleaseOrphan(ctx context.Context, reservation PortReservation) (bool, error) {
	pa.creationMu.RLock()
	defer pa.creationMu.RUnlock()
	if pa.creating[reservation.Owner] > 0 {
		return false, nil
	}
	return pa.scheduleRelease(ctx, reservation, true)
}

func (pa *PortAllocator) SnapshotReservations(ctx context.Context) ([]PortReservation, error) {
	values, err := snapshotPortReservationsScript.Run(ctx, pa.rdb, pa.portStateKeys()).StringSlice()
	if err != nil {
		return nil, err
	}
	if len(values)%2 != 0 {
		return nil, fmt.Errorf("포트 예약 응답이 올바르지 않습니다")
	}
	result := make([]PortReservation, 0, len(values)/2)
	for i := 0; i < len(values); i += 2 {
		port, err := strconv.Atoi(values[i])
		if err != nil || port < 1 || port > 65535 || !ValidPortOwner(values[i+1]) {
			return nil, fmt.Errorf("저장된 포트 예약이 올바르지 않습니다")
		}
		result = append(result, PortReservation{Port: port, Owner: values[i+1]})
	}
	return result, nil
}

// ObservePort는 다른 예약 소유자를 덮지 않고 모든 실제 Docker 점유를 복구한다.
func (pa *PortAllocator) ObservePort(ctx context.Context, port int, owner string, containerID ...string) error {
	if port < 1 || port > 65535 || !ValidPortOwner(owner) {
		return fmt.Errorf("관측한 포트 예약이 올바르지 않습니다")
	}
	if len(containerID) > 1 {
		return fmt.Errorf("관측한 컨테이너 identity가 올바르지 않습니다")
	}
	pa.creationMu.Lock()
	defer pa.creationMu.Unlock()
	result, err := observePortScript.Run(ctx, pa.rdb, pa.portStateKeys(), port, owner).Int64()
	if err != nil || result < 0 || result == 2 {
		if len(containerID) == 1 && containerID[0] != "" {
			pa.holdPortConflictLocked(PortConflict{Port: port, Owner: owner, ContainerID: containerID[0]})
		}
		if err != nil {
			return err
		}
		if result == 2 {
			return &CooldownObservationPendingError{}
		}
		return &ReservationOwnerMismatchError{}
	}
	return nil
}

func (pa *PortAllocator) ReapCooldowns(ctx context.Context) error {
	pa.creationMu.RLock()
	defer pa.creationMu.RUnlock()
	return reapPortCooldownsScript.Run(ctx, pa.rdb, pa.portStateKeys(), pa.protectedPortArgumentsLocked()...).Err()
}

func (pa *PortAllocator) protectedPortArgumentsLocked() []interface{} {
	arguments := make([]interface{}, 0, len(pa.conflicts))
	for port := range pa.conflicts {
		arguments = append(arguments, port)
	}
	return arguments
}

func (pa *PortAllocator) holdPortConflictLocked(conflict PortConflict) {
	if pa.conflicts == nil {
		pa.conflicts = make(map[int]map[string]string)
	}
	if pa.conflicts[conflict.Port] == nil {
		pa.conflicts[conflict.Port] = make(map[string]string)
	}
	pa.conflicts[conflict.Port][conflict.ContainerID] = conflict.Owner
}

func (pa *PortAllocator) HoldPortConflict(conflict PortConflict) {
	if conflict.Port < 1 || conflict.Port > 65535 || conflict.ContainerID == "" || !ValidPortOwner(conflict.Owner) {
		return
	}
	pa.creationMu.Lock()
	defer pa.creationMu.Unlock()
	pa.holdPortConflictLocked(conflict)
}

func (pa *PortAllocator) PortConflicts() []PortConflict {
	pa.creationMu.RLock()
	defer pa.creationMu.RUnlock()
	var result []PortConflict
	for port, containers := range pa.conflicts {
		for id, owner := range containers {
			result = append(result, PortConflict{Port: port, Owner: owner, ContainerID: id})
		}
	}
	return result
}

// ResolvePortConflict는 최신 NotFound 또는 같은 owner의 실제 점유 확인 뒤에만 호출한다.
func (pa *PortAllocator) ResolvePortConflict(conflict PortConflict) {
	pa.creationMu.Lock()
	defer pa.creationMu.Unlock()
	containers := pa.conflicts[conflict.Port]
	if owner, exists := containers[conflict.ContainerID]; exists && owner == conflict.Owner {
		delete(containers, conflict.ContainerID)
		if len(containers) == 0 {
			delete(pa.conflicts, conflict.Port)
		}
	}
}

// AllocatedCount는 현재 할당된 포트 수를 반환한다.
func (pa *PortAllocator) AllocatedCount(ctx context.Context) (int64, error) {
	if err := pa.ReapCooldowns(ctx); err != nil {
		return 0, err
	}
	return pa.rdb.SCard(ctx, allocatedPortsKey).Result()
}

// notifyPortPoolExhausted는 포트 풀 소진 시 관리자에게 알림을 보낸다.
func (pa *PortAllocator) notifyPortPoolExhausted() {
	webhookURL := os.Getenv("ALERT_WEBHOOK_URL")
	if webhookURL == "" {
		log.Println("[ALERT] 포트 풀 소진 — ALERT_WEBHOOK_URL 미설정")
		return
	}

	body := fmt.Sprintf(`{"text":"[CTF] 포트 풀 소진: %d~%d 범위 내 사용 가능한 포트 없음"}`, pa.portMin, pa.portMax)
	client := &http.Client{Timeout: 10 * time.Second}
	resp, err := client.Post(webhookURL, "application/json", strings.NewReader(body))
	if err != nil {
		log.Printf("[ALERT] 웹훅 전송 실패: %v\n", err)
		return
	}
	defer resp.Body.Close()
	log.Printf("[ALERT] 웹훅 전송 완료 (status: %d)\n", resp.StatusCode)
}
