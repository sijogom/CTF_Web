// Package lifecycle는 컨테이너 생명주기 관리를 담당한다.
package lifecycle

import (
	"context"
	"fmt"
	"log"
	"strconv"
	"time"

	"github.com/docker/docker/api/types/container"
	dockerclient "github.com/docker/docker/client"
	"github.com/docker/docker/errdefs"
	"github.com/redis/go-redis/v9"

	ctfcontainer "github.com/ctf-platform/env-orchestrator/internal/container"
)

// TTLWatcher는 만료된 인스턴스를 감지하고 정리한다.
type TTLWatcher struct {
	rdb            *redis.Client
	docker         *dockerclient.Client
	portAllocator  *ctfcontainer.PortAllocator
	runtimeCleanup InstanceRuntimeCleanup
	checkInterval  time.Duration
	stopCh         chan struct{}
}

// NewTTLWatcher는 새로운 TTL 감시자를 생성한다.
func NewTTLWatcher(
	rdb *redis.Client,
	docker *dockerclient.Client,
	portAllocator *ctfcontainer.PortAllocator,
	runtimeCleanup InstanceRuntimeCleanup,
	checkInterval time.Duration,
) *TTLWatcher {
	return &TTLWatcher{
		rdb:            rdb,
		docker:         docker,
		portAllocator:  portAllocator,
		runtimeCleanup: runtimeCleanup,
		checkInterval:  checkInterval,
		stopCh:         make(chan struct{}),
	}
}

// Start는 TTL 감시를 시작한다 (백그라운드 고루틴).
func (w *TTLWatcher) Start(ctx context.Context) {
	go func() {
		ticker := time.NewTicker(w.checkInterval)
		defer ticker.Stop()

		for {
			select {
			case <-ticker.C:
				w.cleanupExpired(ctx)
			case <-w.stopCh:
				return
			case <-ctx.Done():
				return
			}
		}
	}()
	log.Printf("[TTLWatcher] 시작됨 (간격: %v)\n", w.checkInterval)
}

// Stop은 TTL 감시를 중단한다.
func (w *TTLWatcher) Stop() {
	close(w.stopCh)
}

// cleanupExpired는 만료된 인스턴스를 찾아 정리한다.
func (w *TTLWatcher) cleanupExpired(ctx context.Context) {
	var cursor uint64
	now := time.Now().Unix()

	for {
		keys, nextCursor, err := w.rdb.Scan(ctx, cursor, "instance:*", 100).Result()
		if err != nil {
			log.Printf("[TTLWatcher] 스캔 오류 (cursor=%d): %v — 다음 주기에 재시도\n", cursor, err)
			break // 전체 중단 대신 이번 주기만 종료
		}

		for _, key := range keys {
			expiresStr, err := w.rdb.HGet(ctx, key, "expires_at").Result()
			if err != nil {
				continue
			}

			var expiresAt int64
			if _, err := fmt.Sscanf(expiresStr, "%d", &expiresAt); err != nil {
				log.Printf("[TTLWatcher] expires_at 파싱 오류 (key=%s, value=%q): %v\n", key, expiresStr, err)
				continue
			}

			if expiresAt > 0 && expiresAt <= now {
				w.cleanupInstance(ctx, key)
			}
		}

		if nextCursor == 0 {
			break
		}
		cursor = nextCursor
	}
}

// cleanupInstance는 단일 인스턴스를 정리한다.
func (w *TTLWatcher) cleanupInstance(ctx context.Context, instanceKey string) {
	data, err := w.rdb.HGetAll(ctx, instanceKey).Result()
	if err != nil || len(data) == 0 {
		return
	}

	log.Printf("[TTLWatcher] 만료된 인스턴스 정리: %s\n", instanceKey)
	instanceID := instanceKey[len("instance:"):]

	// Docker 삭제보다 먼저 issuance를 revoke하고 host runtime file을 제거한다.
	// 컨테이너 삭제가 실패하더라도 Learn 검증은 즉시 무효화된다.
	runInstanceRuntimeCleanup(w.runtimeCleanup, instanceID, data, "instance_expired")

	// Docker 컨테이너 정지 + 삭제
	containerID := data["container_id"]
	if containerID == "" {
		log.Printf("[TTLWatcher] 인스턴스 컨테이너 identity 없음 (%s)", instanceID)
		return
	}
	if containerID != "" {
		timeout := 5
		stopOptions := container.StopOptions{Timeout: &timeout}
		_ = w.docker.ContainerStop(ctx, containerID, stopOptions)
		if err := w.docker.ContainerRemove(ctx, containerID, container.RemoveOptions{Force: true}); err != nil && !errdefs.IsNotFound(err) {
			log.Printf("[TTLWatcher] 컨테이너 삭제 미확인 (%s): %v", instanceID, err)
			return
		}
	}

	// 포트 해제
	port, err := strconv.Atoi(data["port"])
	if err != nil || port < 1 || port > 65535 {
		log.Printf("[TTLWatcher] 인스턴스 포트가 올바르지 않음 (%s)", instanceID)
		return
	}
	if err := ctfcontainer.FinalizeRemovedInstance(ctx, w.rdb, w.portAllocator, instanceID, data); err != nil {
		log.Printf("[TTLWatcher] 메타데이터 정리 보류 (%s): %v", instanceID, err)
	}
}
