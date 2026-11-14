package lifecycle

import (
	"context"
	"errors"
	"fmt"
	"log"
	"strconv"
	"time"

	dockertypes "github.com/docker/docker/api/types"
	"github.com/docker/docker/api/types/container"
	"github.com/docker/docker/api/types/filters"
	dockerclient "github.com/docker/docker/client"
	"github.com/docker/docker/errdefs"
	"github.com/redis/go-redis/v9"

	ctfcontainer "github.com/ctf-platform/env-orchestrator/internal/container"
)

const instanceCreationGrace = 2 * time.Minute

// Reconciler는 Docker 컨테이너, Redis 인스턴스 메타데이터, 포트 할당의 정합성을 검증한다.
// 유효한 Redis 레코드가 없는 챌린지 컨테이너는 인가·만료 처리를 보장할 수 없으므로 정리한다.
type Reconciler struct {
	rdb            *redis.Client
	docker         *dockerclient.Client
	portAllocator  *ctfcontainer.PortAllocator
	runtimeCleanup InstanceRuntimeCleanup
	interval       time.Duration
	stopCh         chan struct{}
}

// NewReconciler는 새로운 정합성 검증기를 생성한다.
func NewReconciler(
	rdb *redis.Client,
	docker *dockerclient.Client,
	runtimeCleanup InstanceRuntimeCleanup,
	interval time.Duration,
	allocators ...*ctfcontainer.PortAllocator,
) *Reconciler {
	if len(allocators) > 1 {
		panic("reconciler accepts one port allocator")
	}
	allocator := ctfcontainer.NewPortAllocator(rdb, 1024, 65535)
	if len(allocators) == 1 {
		if allocators[0] == nil {
			panic("reconciler port allocator is nil")
		}
		allocator = allocators[0]
	}
	return &Reconciler{
		rdb:            rdb,
		docker:         docker,
		portAllocator:  allocator,
		runtimeCleanup: runtimeCleanup,
		interval:       interval,
		stopCh:         make(chan struct{}),
	}
}

// Start는 정합성 검증을 시작한다 (백그라운드 고루틴).
func (r *Reconciler) Start(ctx context.Context) {
	go func() {
		ticker := time.NewTicker(r.interval)
		defer ticker.Stop()

		for {
			select {
			case <-ticker.C:
				r.reconcile(ctx)
			case <-r.stopCh:
				return
			case <-ctx.Done():
				return
			}
		}
	}()
	log.Printf("[Reconciler] 시작됨 (간격: %v)\n", r.interval)
}

// Stop은 정합성 검증을 중단한다.
func (r *Reconciler) Stop() {
	close(r.stopCh)
}

// reconcile은 Docker 실제 상태와 Redis 인스턴스·포트 목록을 비교하여 고아 리소스를 정리한다.
func (r *Reconciler) reconcile(ctx context.Context) {
	r.revalidatePortConflicts(ctx)
	// 고아 정리는 Docker 목록 조회 전에 관측한 예약에만 적용한다.
	reservations, err := r.portAllocator.SnapshotReservations(ctx)
	if err != nil {
		log.Printf("[Reconciler] 포트 예약 조회 실패: %v", err)
		return
	}
	filterArgs := filters.NewArgs()
	filterArgs.Add("label", "ctf-challenge=true")

	containers, err := r.docker.ContainerList(ctx, container.ListOptions{
		All:     true,
		Filters: filterArgs,
	})
	if err != nil {
		log.Printf("[Reconciler] Docker 컨테이너 목록 조회 실패: %v\n", err)
		return
	}

	dockerPorts := make(map[int]bool)
	seenInstances := make(map[string]bool)
	now := time.Now().Unix()

	for _, c := range containers {
		instanceID := c.Labels["instance-id"]
		if !ctfcontainer.ValidPortOwner(instanceID) {
			instanceID = ""
		}
		observed, observeErr := r.observePorts(ctx, c, instanceID)
		if observeErr != nil {
			log.Printf("[Reconciler] Docker 점유 확인 보류: %v", observeErr)
			return
		}
		if !observed {
			continue
		}
		r.collectPorts(c.Ports, dockerPorts)
		if instanceID == "" {
			log.Printf("[Reconciler] instance-id 라벨이 없는 관리 대상 컨테이너 보류: %s\n", c.ID)
			continue
		}
		seenInstances[instanceID] = true
		if r.portAllocator.IsCreating(instanceID) {
			continue
		}
		instanceKey := fmt.Sprintf("instance:%s", instanceID)
		data, dataErr := r.rdb.HGetAll(ctx, instanceKey).Result()
		if dataErr != nil {
			log.Printf("[Reconciler] Redis 인스턴스 조회 실패 (%s): %v\n", instanceID, dataErr)
			continue
		}

		if len(data) == 0 {
			if time.Since(time.Unix(c.Created, 0)) < instanceCreationGrace {
				// 생성 요청은 준비 상태 확인 후 Redis를 기록하므로 짧은 생성 창은 보존한다.
				continue
			}
			log.Printf("[Reconciler] Redis 참조가 없는 컨테이너 정리: %s\n", c.ID)
			runInstanceRuntimeCleanup(r.runtimeCleanup, instanceID, data, "reconcile_cleanup")
			if r.removeContainer(ctx, c.ID) {
				r.resolveContainerConflicts(c, instanceID)
				r.releasePorts(ctx, c.Ports, instanceID)
			}
			continue
		}

		expiresAt, expiresErr := strconv.ParseInt(data["expires_at"], 10, 64)
		if expiresErr != nil || expiresAt <= now || c.State != "running" {
			runInstanceRuntimeCleanup(r.runtimeCleanup, instanceID, data, "reconcile_cleanup")
			log.Printf("[Reconciler] 만료 또는 비실행 인스턴스 정리: %s (state=%s)\n", instanceID, c.State)
			if r.removeContainer(ctx, c.ID) {
				r.resolveContainerConflicts(c, instanceID)
				r.finalizeRemovedInstance(ctx, instanceID, data, c.Ports)
			}
			continue
		}

		r.collectPorts(c.Ports, dockerPorts)
	}

	// Docker에서 사라진 Redis 인스턴스도 포트·사용자 참조와 함께 정리한다.
	if r.cleanupMissingDockerInstances(ctx, seenInstances, dockerPorts) {
		r.reconcilePortSet(ctx, dockerPorts, reservations)
	}
}

func (r *Reconciler) collectPorts(ports []dockertypes.Port, destination map[int]bool) {
	for _, p := range ports {
		if p.PublicPort > 0 {
			destination[int(p.PublicPort)] = true
		}
	}
}

func deferredPortObservation(err error) bool {
	var mismatch *ctfcontainer.ReservationOwnerMismatchError
	var pending *ctfcontainer.CooldownObservationPendingError
	return errors.As(err, &mismatch) || errors.As(err, &pending)
}

func (r *Reconciler) observePorts(ctx context.Context, observed dockertypes.Container, owner string) (bool, error) {
	inspected := false
	for _, p := range observed.Ports {
		if p.PublicPort > 0 {
			if err := r.portAllocator.ObservePort(ctx, int(p.PublicPort), owner, observed.ID); err != nil {
				if !deferredPortObservation(err) {
					return false, err
				}
				if !inspected {
					_, inspectErr := r.docker.ContainerInspect(ctx, observed.ID)
					if errdefs.IsNotFound(inspectErr) {
						r.resolveContainerConflicts(observed, owner)
						return false, nil
					}
					if inspectErr != nil {
						return false, inspectErr
					}
					inspected = true
				}
			}
		}
	}
	return true, nil
}

func (r *Reconciler) resolveContainerConflicts(observed dockertypes.Container, owner string) {
	for _, port := range observed.Ports {
		if port.PublicPort > 0 {
			r.portAllocator.ResolvePortConflict(ctfcontainer.PortConflict{Port: int(port.PublicPort), Owner: owner, ContainerID: observed.ID})
			if owner != "" {
				r.portAllocator.ResolvePortConflict(ctfcontainer.PortConflict{Port: int(port.PublicPort), Owner: owner, ContainerID: "ctf-" + owner})
			}
		}
	}
}

func (r *Reconciler) revalidatePortConflicts(ctx context.Context) {
	for _, conflict := range r.portAllocator.PortConflicts() {
		_, err := r.docker.ContainerInspect(ctx, conflict.ContainerID)
		if errdefs.IsNotFound(err) {
			r.portAllocator.ResolvePortConflict(conflict)
		} else if err == nil {
			if err := r.portAllocator.ObservePort(ctx, conflict.Port, conflict.Owner, conflict.ContainerID); err == nil {
				r.portAllocator.ResolvePortConflict(conflict)
			}
		}
	}
}

func (r *Reconciler) releasePorts(ctx context.Context, ports []dockertypes.Port, owner string) bool {
	for _, p := range ports {
		if p.PublicPort > 0 {
			if err := r.portAllocator.ReleaseOwned(ctx, int(p.PublicPort), owner); err != nil {
				var mismatch *ctfcontainer.ReservationOwnerMismatchError
				if errors.As(err, &mismatch) {
					continue
				}
				log.Printf("[Reconciler] 포트 쿨다운 저장 보류: %v", err)
				return false
			}
		}
	}
	return true
}

func (r *Reconciler) removeContainer(ctx context.Context, containerID string) bool {
	if err := r.docker.ContainerRemove(ctx, containerID, container.RemoveOptions{Force: true}); err != nil && !errdefs.IsNotFound(err) {
		log.Printf("[Reconciler] 컨테이너 삭제 실패 (%s): %v\n", containerID, err)
		return false
	}
	return true
}

func (r *Reconciler) finalizeRemovedInstance(ctx context.Context, instanceID string, data map[string]string, observed []dockertypes.Port) bool {
	var ports []int
	for _, port := range observed {
		if port.PublicPort > 0 {
			ports = append(ports, int(port.PublicPort))
		}
	}
	if err := ctfcontainer.FinalizeRemovedInstance(ctx, r.rdb, r.portAllocator, instanceID, data, ports...); err != nil {
		log.Printf("[Reconciler] 인스턴스 메타데이터 정리 보류: %v", err)
		return false
	}
	return true
}

func (r *Reconciler) cleanupMissingDockerInstances(ctx context.Context, seenInstances map[string]bool, dockerPorts map[int]bool) bool {
	var cursor uint64
	for {
		keys, nextCursor, err := r.rdb.Scan(ctx, cursor, "instance:*", 100).Result()
		if err != nil {
			log.Printf("[Reconciler] Redis 인스턴스 스캔 실패: %v\n", err)
			return false
		}
		for _, key := range keys {
			instanceID := key[len("instance:"):]
			data, dataErr := r.rdb.HGetAll(ctx, key).Result()
			if dataErr != nil {
				return false
			}
			if len(data) == 0 {
				continue
			}
			port, portErr := strconv.Atoi(data["port"])
			if portErr != nil || port < 1 || port > 65535 {
				return false
			}
			// 메타데이터는 소유자 정보가 없는 기존 예약도 보호한다.
			dockerPorts[port] = true
			if seenInstances[instanceID] || r.portAllocator.IsCreating(instanceID) {
				continue
			}
			if data["container_id"] == "" {
				continue
			}
			conflict := ctfcontainer.PortConflict{Port: port, Owner: instanceID, ContainerID: data["container_id"]}
			r.portAllocator.HoldPortConflict(conflict)
			_, inspectErr := r.docker.ContainerInspect(ctx, data["container_id"])
			if inspectErr == nil {
				if err := r.portAllocator.ObservePort(ctx, port, instanceID, data["container_id"]); err == nil {
					r.portAllocator.ResolvePortConflict(conflict)
				} else {
					if !deferredPortObservation(err) {
						return false
					}
				}
				continue
			}
			if !errdefs.IsNotFound(inspectErr) {
				log.Printf("[Reconciler] 최신 컨테이너 조회 미확인 (%s): %v", instanceID, inspectErr)
				continue
			}
			r.portAllocator.ResolvePortConflict(conflict)
			runInstanceRuntimeCleanup(r.runtimeCleanup, instanceID, data, "reconcile_cleanup")
			if r.finalizeRemovedInstance(ctx, instanceID, data, nil) {
				log.Printf("[Reconciler] Docker 컨테이너가 없는 Redis 인스턴스 정리: %s\n", instanceID)
			}
		}
		if nextCursor == 0 {
			return true
		}
		cursor = nextCursor
	}
}

func (r *Reconciler) reconcilePortSet(ctx context.Context, dockerPorts map[int]bool, reservations []ctfcontainer.PortReservation) {
	for _, reservation := range reservations {
		if dockerPorts[reservation.Port] {
			continue
		}
		if reservation.Owner != "" {
			if r.portAllocator.IsCreating(reservation.Owner) {
				continue
			}
			conflict := ctfcontainer.PortConflict{Port: reservation.Port, Owner: reservation.Owner, ContainerID: "ctf-" + reservation.Owner}
			r.portAllocator.HoldPortConflict(conflict)
			_, err := r.docker.ContainerInspect(ctx, conflict.ContainerID)
			if err == nil {
				if err := r.portAllocator.ObservePort(ctx, reservation.Port, reservation.Owner, conflict.ContainerID); err != nil {
					if !deferredPortObservation(err) {
						log.Printf("[Reconciler] 최신 점유 포트 보존 실패: %v", err)
					}
				} else {
					r.portAllocator.ResolvePortConflict(conflict)
				}
				continue
			}
			if !errdefs.IsNotFound(err) {
				continue
			}
			r.portAllocator.ResolvePortConflict(conflict)
		}
		if _, err := r.portAllocator.ReleaseOrphan(ctx, reservation); err != nil {
			log.Printf("[Reconciler] 고아 예약 쿨다운 보류: %v", err)
		}
	}
	// 현재 Docker 점유는 위에서 해당 쿨다운을 취소했다.
	if err := r.portAllocator.ReapCooldowns(ctx); err != nil {
		log.Printf("[Reconciler] 만료 쿨다운 회수 보류: %v", err)
	}
}
