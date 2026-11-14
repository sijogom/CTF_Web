package container

import (
	"bytes"
	"context"
	crypto_rand "crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"net/url"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	dockertypes "github.com/docker/docker/api/types"
	"github.com/docker/docker/api/types/container"
	"github.com/docker/docker/api/types/network"
	dockerclient "github.com/docker/docker/client"
	"github.com/docker/docker/errdefs"
	"github.com/docker/go-connections/nat"
	"github.com/redis/go-redis/v9"

	"github.com/ctf-platform/env-orchestrator/internal/security"
)

const (
	defaultReadinessTimeout   = 45 * time.Second
	defaultMaxContainers      = 50
	readinessProbeInterval    = 500 * time.Millisecond
	readinessDialTimeout      = 2 * time.Second
	instanceMetadataRetention = 24 * time.Hour
	defaultDynamicFlagDir     = "/var/lib/ctf-web/dynamic-flags"
	dynamicFlagRuntimePath    = "/flag.txt"
	dynamicFlagMaxLength      = 8192
	dynamicFlagMaxTTL         = 24 * time.Hour
	dynamicFlagOrphanGrace    = 2 * time.Minute
)

// adminSettings는 Redis admin:settings에서 읽어온 동적 설정이다.
type adminSettings struct {
	ContainerTimeout int `json:"container_timeout"` // 분 단위
	MaxContainers    int `json:"max_containers"`    // 전체 최대 (사용자당 아님)
}

// Instance는 실행 중인 챌린지 컨테이너 인스턴스를 나타낸다.
type Instance struct {
	ID             string    `json:"id"`
	UserID         string    `json:"user_id"`
	ChallengeID    int       `json:"challenge_id"`
	ContainerID    string    `json:"container_id"`
	Port           int       `json:"port"`
	ContainerPort  int       `json:"-"`
	Status         string    `json:"status"` // starting, running, unhealthy, unavailable, stopping, stopped
	ExpiresAt      time.Time `json:"expires_at"`
	CreatedAt      time.Time `json:"created_at"`
	ExtendCount    int       `json:"extend_count"`
	MaxExtendCount int       `json:"max_extend_count"`
}

// EnvironmentReadinessError는 사용자에게 Docker 내부 정보나 네트워크 주소를 노출하지 않고
// 실습 환경이 아직 사용할 수 없는 이유를 전달한다. 상세 원인은 서버 로그에만 기록한다.
type EnvironmentReadinessError struct {
	Code string
}

func (e *EnvironmentReadinessError) Error() string {
	return PublicEnvironmentErrorMessage(e.Code)
}

// PublicEnvironmentErrorCode returns only error codes that may cross the HTTP
// boundary. Downstream service details and Docker errors never become a code.
func PublicEnvironmentErrorCode(code string) string {
	switch code {
	case "container_exited",
		"container_starting",
		"container_mismatch",
		"build_context_unavailable",
		"dynamic_flag_required",
		"healthcheck_unhealthy",
		"inspect_failed",
		"invalid_environment",
		"network_unavailable",
		"port_unreachable",
		"start_timeout",
		"verification_in_progress",
		"verification_record_failed",
		"environment_verification_required",
		"environment_unavailable",
		"stale_environment_revision":
		return code
	default:
		return "environment_unavailable"
	}
}

// PublicEnvironmentErrorMessage is deliberately stable Korean UI text. It is
// selected from the allowlisted code above and must not include error details.
func PublicEnvironmentErrorMessage(code string) string {
	switch PublicEnvironmentErrorCode(code) {
	case "environment_verification_required":
		return "현재 실습 환경의 검증이 필요합니다. 관리자에게 환경 검증을 요청하세요."
	case "environment_unavailable":
		return "실습 환경 정보를 확인할 수 없습니다. 잠시 후 다시 시도하세요."
	case "build_context_unavailable":
		return "\ube4c\ub4dc \ucee8\ud14d\uc2a4\ud2b8\ub97c \ud655\uc778\ud560 \uc218 \uc5c6\uc2b5\ub2c8\ub2e4. \uc7a0\uc2dc \ud6c4 \ub2e4\uc2dc \uc2dc\ub3c4\ud558\uc138\uc694."
	case "dynamic_flag_required":
		return "이 문제의 HEALTHCHECK는 동적 플래그를 필요로 합니다. 관리자에게 플래그 정책을 확인해 달라고 요청하세요."
	case "container_exited":
		return "실습 환경 컨테이너가 종료되었습니다. 관리자에게 문의하세요."
	case "container_starting":
		return "실습 환경을 준비 중입니다. 잠시 후 다시 시도하세요."
	case "healthcheck_unhealthy", "container_mismatch", "inspect_failed":
		return "실습 환경 상태를 확인할 수 없습니다. 관리자에게 문의하세요."
	case "network_unavailable", "port_unreachable", "start_timeout":
		return "실습 환경에 연결할 수 없습니다. 잠시 후 다시 시도하세요."
	case "invalid_environment":
		return "문제의 실습 환경 설정이 올바르지 않습니다. 관리자에게 문의하세요."
	case "verification_in_progress":
		return "실습 환경 검증이 진행 중입니다. 잠시 후 다시 시도하세요."
	case "verification_record_failed":
		return "실습 환경 검증 결과를 기록하지 못했습니다. 잠시 후 다시 시도하세요."
	case "stale_environment_revision":
		return "빌드 또는 검증 중 실습 환경 설정이 변경되었습니다. 현재 설정으로 다시 진행하세요."
	default:
		return "실습 환경을 시작할 수 없습니다. 잠시 후 다시 시도하세요."
	}
}

// ServiceUnavailable lets the transport boundary return 503 instead of treating
// a known-but-temporarily unusable environment as a missing instance.
func (e *EnvironmentReadinessError) ServiceUnavailable() bool {
	return true
}

func ReadinessErrorCode(err error) string {
	var readinessErr *EnvironmentReadinessError
	if errors.As(err, &readinessErr) {
		return PublicEnvironmentErrorCode(readinessErr.Code)
	}
	return "environment_unavailable"
}

// ExtensionResult는 UI가 서버의 실제 TTL 정책을 추측하지 않도록 인스턴스 연장 결과를
// 명시적으로 전달한다. 시간은 API 경계에서 UTC RFC3339로 직렬화된다.
type ExtensionResult struct {
	ExpiresAt      time.Time `json:"expires_at"`
	ExtendCount    int       `json:"extend_count"`
	MaxExtendCount int       `json:"max_extend_count"`
}

// CreateRequest는 컨테이너 생성 요청 파라미터이다.
type CreateRequest struct {
	UserID        string `json:"user_id" binding:"required"`
	ChallengeID   int    `json:"challenge_id" binding:"required"`
	CompetitionID string `json:"competition_id"`
}

// challengeEnvResponse는 learn-service에서 반환하는 환경 정보이다.
type challengeEnvResponse struct {
	EnvType                    string                    `json:"env_type"`
	DockerImage                string                    `json:"docker_image"`
	ContainerPort              int                       `json:"container_port"`
	ChallengeType              string                    `json:"challenge_type"`
	EnvironmentRevision        int                       `json:"environment_revision"`
	EnvironmentReady           bool                      `json:"environment_ready"`
	EnvironmentVerifiedImageID string                    `json:"environment_verified_image_id"`
	DynamicFlag                dynamicFlagPolicyResponse `json:"dynamic_flag"`
}

// dynamicFlagPolicyResponse는 정책 메타데이터만 담는다. 동적 플래그 원문은 문제
// 환경 조회 응답·Redis·로그에 절대 포함하지 않는다.
type dynamicFlagPolicyResponse struct {
	Enabled        bool   `json:"enabled"`
	RuntimePath    string `json:"runtime_path"`
	TTLSeconds     int    `json:"ttl_seconds"`
	PolicyRevision int    `json:"policy_revision"`
}

type dynamicFlagIssueRequest struct {
	ChallengeID         int       `json:"challenge_id"`
	InstanceID          string    `json:"instance_id"`
	UserID              string    `json:"user_id"`
	CompetitionID       *string   `json:"competition_id"`
	EnvironmentRevision int       `json:"environment_revision"`
	VerifiedImageID     string    `json:"verified_image_id"`
	ExpiresAt           time.Time `json:"expires_at"`
}

// dynamicFlagIssueResponse의 원문 플래그는 챌린지 컨테이너에 마운트할 읽기 전용
// 호스트 파일을 만드는 짧은 시간 동안만 메모리에 존재해야 한다.
type dynamicFlagIssueResponse struct {
	Flag            string    `json:"flag"`
	InstanceLocator string    `json:"instance_locator"`
	ExpiresAt       time.Time `json:"expires_at"`
	RuntimePath     string    `json:"runtime_path"`
}

// dynamicFlagIssueError는 Learn이 one-shot 발급을 회전한 뒤의 실패와 단순 요청
// 거부를 구분한다. 호출자는 회전 뒤 실패 시 영향을 받은 인스턴스를 종료할 수 있다.
type dynamicFlagIssueError struct {
	err    error
	issued bool
}

func (e *dynamicFlagIssueError) Error() string { return e.err.Error() }
func (e *dynamicFlagIssueError) Unwrap() error { return e.err }

type challengeEnvironmentPurpose string

const (
	challengeEnvironmentPurposeInstance     challengeEnvironmentPurpose = "instance"
	challengeEnvironmentPurposeVerification challengeEnvironmentPurpose = "verification"
)

// environmentVerificationRecord is deliberately limited to the public audit
// metadata accepted by learn-service. Never add Docker logs, container IDs,
// network addresses, or challenge secrets here.
type environmentVerificationRecord struct {
	Revision       int     `json:"revision"`
	Status         string  `json:"status"`
	FailureCode    *string `json:"failure_code"`
	ImageReference string  `json:"image_reference"`
	ImageID        *string `json:"image_id"`
	VerifiedBy     string  `json:"verified_by"`
}

// Manager는 챌린지 컨테이너의 생명주기를 관리한다.
type Manager struct {
	dynamicFlagDir   string
	portAllocator    *PortAllocator
	rdb              *redis.Client
	authRdb          *redis.Client // auth Redis (DB 0) — admin:settings 읽기용
	docker           *dockerclient.Client
	httpClient       *http.Client
	learnURL         string        // learn-service 내부 URL
	authURL          string        // auth-service 내부 URL
	challengeNet     string        // 챌린지 전용 Docker 네트워크
	ttlMinutes       int           // 기본값 (admin:settings 미설정 시 폴백)
	maxPerUser       int           // 기본값 (admin:settings 미설정 시 폴백)
	maxContainers    int           // 전체 상한 기본값 (admin:settings 미설정 시 폴백)
	internalAPIKey   string        // 내부 서비스 간 통신 인증 키
	maxExtendCount   int           // 인스턴스당 최대 연장 횟수
	readinessTimeout time.Duration // 컨테이너 준비 상태 확인 제한 시간
}

// NewManager는 새로운 컨테이너 매니저를 생성한다.
func NewManager(
	portAllocator *PortAllocator,
	rdb *redis.Client,
	authRdb *redis.Client,
	docker *dockerclient.Client,
	learnURL string,
	authURL string,
	challengeNet string,
	ttlMinutes int,
	maxPerUser int,
	maxContainers int,
	internalAPIKey string,
	readinessTimeout time.Duration,
) *Manager {
	if readinessTimeout <= 0 {
		readinessTimeout = defaultReadinessTimeout
	}
	if maxContainers < 1 {
		maxContainers = defaultMaxContainers
	}
	return &Manager{
		dynamicFlagDir:   defaultDynamicFlagDir,
		portAllocator:    portAllocator,
		rdb:              rdb,
		authRdb:          authRdb,
		docker:           docker,
		httpClient:       &http.Client{Timeout: 15 * time.Second},
		learnURL:         learnURL,
		authURL:          authURL,
		challengeNet:     challengeNet,
		ttlMinutes:       ttlMinutes,
		maxPerUser:       maxPerUser,
		maxContainers:    maxContainers,
		internalAPIKey:   internalAPIKey,
		maxExtendCount:   3, // 기본값: 최대 3회 연장
		readinessTimeout: readinessTimeout,
	}
}

// SetDynamicFlagRuntimeDir는 짧은 수명의 동적 플래그 파일을 둘 호스트 디렉터리만
// 변경한다. 참가자 요청이나 문제 build context가 아닌 배포 설정에서만 지정한다.
func (m *Manager) SetDynamicFlagRuntimeDir(dir string) {
	if strings.TrimSpace(dir) == "" {
		m.dynamicFlagDir = defaultDynamicFlagDir
		return
	}
	m.dynamicFlagDir = filepath.Clean(dir)
}

// getAdminSettings는 auth Redis(DB 0)의 admin:settings에서 동적 설정을 읽는다.
// 실패 시 생성자에서 받은 기본값을 반환한다.
func (m *Manager) getAdminSettings(ctx context.Context) (ttl int, maxPerUser int, maxContainers int) {
	ttl = m.ttlMinutes
	maxPerUser = m.maxPerUser
	maxContainers = m.maxContainers

	if m.authRdb == nil {
		return
	}

	raw, err := m.authRdb.Get(ctx, "admin:settings").Result()
	if err != nil {
		return
	}

	var settings adminSettings
	if err := json.Unmarshal([]byte(raw), &settings); err != nil {
		return
	}

	if settings.ContainerTimeout > 0 {
		ttl = settings.ContainerTimeout
	}
	if settings.MaxContainers > 0 {
		maxContainers = settings.MaxContainers
	}
	// max_per_user 필드가 추가되기 전까지 사용자당 제한은 생성자 기본값을 유지한다.
	return
}

// cleanAndCountInstances는 user_instances 셋에서 좀비 참조를 정리하고 실제 활성 인스턴스 수를 반환한다.
// healthcheckDependsOnFlag는 healthcheck가 instance-specific flag 값을 비교하는지
// 판별한다. static/regex에는 실제 참가자 flag를 마운트하지 않으므로 이 검사는
// 준비 신호로 사용할 수 없고, dynamic_instance에서만 실제 /flag.txt와 함께 신뢰한다.
func healthcheckDependsOnFlag(test []string) bool {
	joined := strings.ToLower(strings.Join(test, " "))
	for _, marker := range []string{
		"flag.txt",
		"flag_path",
		"get('flag')",
		`get("flag")`,
		"flag ==",
		"flag==",
	} {
		if strings.Contains(joined, marker) {
			return true
		}
	}
	return false
}

// imageNeedsCompatibilityFlag inspects the already-built image rather than the
// uploaded flag.txt. static/regex challenges never receive a participant flag,
// but legacy images may make readiness depend on /flag.txt.
func (m *Manager) imageNeedsCompatibilityFlag(ctx context.Context, image string) (bool, error) {
	if m.docker == nil || strings.TrimSpace(image) == "" {
		return false, &EnvironmentReadinessError{Code: "inspect_failed"}
	}
	imageInfo, _, err := m.docker.ImageInspectWithRaw(ctx, image)
	if err != nil {
		return false, &EnvironmentReadinessError{Code: "inspect_failed"}
	}
	return imageHealthcheckDependsOnFlag(imageInfo), nil
}

func imageHealthcheckDependsOnFlag(imageInfo dockertypes.ImageInspect) bool {
	if imageInfo.Config == nil || imageInfo.Config.Healthcheck == nil {
		return false
	}
	return healthcheckDependsOnFlag(imageInfo.Config.Healthcheck.Test)
}

// flagHealthcheckFailureIsFatal keeps a flag-aware HEALTHCHECK from becoming a
// false negative for a generic Docker challenge. A participant flag is either
// intentionally absent (Static/regex) or freshly generated (dynamic_instance),
// so a check that compares the file contents to an author-specific literal is
// not a portable readiness signal. The runtime mount itself is still enforced
// for dynamic_instance; process liveness and the declared challenge TCP port
// remain the authoritative readiness contract here.
func flagHealthcheckFailureIsFatal(flagDependent, dynamicRuntime bool) bool {
	_ = dynamicRuntime
	return !flagDependent
}

// getRunningContainerTarget은 실행 중인 컨테이너의 챌린지 전용 네트워크 주소를 반환한다.
// 호스트 공개 포트로 폴백하지 않으므로 env-orchestrator가 어느 네트워크 모드에서 실행되더라도
// 사용자 컨테이너에 직접 접근하지 못하는 잘못된 경로를 성공으로 오인하지 않는다.
func (m *Manager) getRunningContainerTarget(
	ctx context.Context,
	containerID string,
	containerPort int,
	expectedInstanceID string,
	expectedImageID string,
	allowFlagDependentHealthcheck bool,
) (host string, port int, healthStatus string, err error) {
	if containerID == "" || containerPort <= 0 {
		return "", 0, "", &EnvironmentReadinessError{Code: "inspect_failed"}
	}

	inspect, inspectErr := m.docker.ContainerInspect(ctx, containerID)
	if inspectErr != nil {
		log.Printf("[Manager] 컨테이너 상태 조회 실패 (%s): %v", containerID, inspectErr)
		return "", 0, "", &EnvironmentReadinessError{Code: "inspect_failed"}
	}
	if inspect.State == nil || !inspect.State.Running {
		return "", 0, "", &EnvironmentReadinessError{Code: "container_exited"}
	}
	if inspect.Config == nil || inspect.Config.Labels["ctf-challenge"] != "true" ||
		(expectedInstanceID != "" && inspect.Config.Labels["instance-id"] != expectedInstanceID) {
		return "", 0, "", &EnvironmentReadinessError{Code: "container_mismatch"}
	}
	if expectedImageID != "" && inspect.Image != expectedImageID {
		return "", 0, "", &EnvironmentReadinessError{Code: "container_mismatch"}
	}

	// Docker가 HEALTHCHECK 상태 구조를 초기화하기 전에도, 이미지가 HEALTHCHECK를
	// 선언했다면 포트만 열렸다는 이유로 준비 완료로 처리하면 안 된다.
	healthcheckConfigured := inspect.Config.Healthcheck != nil && len(inspect.Config.Healthcheck.Test) > 0
	flagDependentHealthcheck := healthcheckConfigured && healthcheckDependsOnFlag(inspect.Config.Healthcheck.Test)
	if healthcheckConfigured && inspect.State.Health == nil {
		healthStatus = "starting"
	} else if inspect.State.Health != nil {
		healthStatus = inspect.State.Health.Status
		// A healthcheck that compares the instance flag is not portable enough to be
		// authoritative: static/regex deliberately do not mount a participant flag,
		// while dynamic_instance mounts a newly generated value that may not equal an
		// author-specific literal. For both modes, the running process, challenge
		// network, and TCP port probe are the readiness contract.
		if healthStatus == "unhealthy" &&
			flagHealthcheckFailureIsFatal(flagDependentHealthcheck, allowFlagDependentHealthcheck) {
			return "", 0, healthStatus, &EnvironmentReadinessError{Code: "healthcheck_unhealthy"}
		}
	}

	if m.challengeNet == "" || inspect.NetworkSettings == nil {
		return "", 0, healthStatus, &EnvironmentReadinessError{Code: "network_unavailable"}
	}
	networkSettings, ok := inspect.NetworkSettings.Networks[m.challengeNet]
	if !ok || networkSettings == nil || networkSettings.IPAddress == "" {
		return "", 0, healthStatus, &EnvironmentReadinessError{Code: "network_unavailable"}
	}
	return networkSettings.IPAddress, containerPort, healthStatus, nil
}

// waitForContainerReady는 ContainerStart 성공만으로 사용 가능 상태를 가정하지 않는다.
// 컨테이너가 실행 중이고, 전용 네트워크를 통해 선언한 내부 TCP 포트에 연결되며,
// 일반 Docker HEALTHCHECK가 선언됐다면 healthy 상태일 때만 성공으로 처리한다.
// 단, instance-specific flag 값을 비교하는 HEALTHCHECK는 동적 플래그 런타임이
// 실제 마운트를 제공하는 경우에만 TCP/readiness를 보조한다. static/regex에는
// flag.txt가 없으므로 unhealthy를 성공으로 우회하지 않는다.
func (m *Manager) waitForContainerReady(
	ctx context.Context,
	containerID string,
	containerPort int,
	expectedInstanceID string,
	allowFlagDependentHealthcheck bool,
) error {
	readyCtx, cancel := context.WithTimeout(ctx, m.readinessTimeout)
	defer cancel()

	lastState := "pending"
	for {
		host, port, healthStatus, targetErr := m.getRunningContainerTarget(
			readyCtx,
			containerID,
			containerPort,
			expectedInstanceID,
			"",
			allowFlagDependentHealthcheck,
		)
		if targetErr != nil {
			if readinessErr, ok := targetErr.(*EnvironmentReadinessError); ok {
				switch readinessErr.Code {
				case "container_exited", "healthcheck_unhealthy", "network_unavailable", "container_mismatch":
					return readinessErr
				default:
					lastState = readinessErr.Code
				}
			} else {
				lastState = "target_resolution_failed"
			}
		} else if probeErr := probeContainerPort(readyCtx, host, port); probeErr != nil {
			lastState = "port_unreachable"
		} else if healthStatus == "starting" {
			lastState = "healthcheck_starting"
		} else {
			return nil
		}

		timer := time.NewTimer(readinessProbeInterval)
		select {
		case <-readyCtx.Done():
			timer.Stop()
			log.Printf("[Manager] 컨테이너 준비 상태 시간 초과 (%s, 마지막 상태: %s)", containerID, lastState)
			if lastState == "port_unreachable" {
				return &EnvironmentReadinessError{Code: "port_unreachable"}
			}
			return &EnvironmentReadinessError{Code: "start_timeout"}
		case <-timer.C:
		}
	}
}

func probeContainerPort(ctx context.Context, host string, port int) error {
	dialer := &net.Dialer{Timeout: readinessDialTimeout}
	conn, err := dialer.DialContext(ctx, "tcp", net.JoinHostPort(host, strconv.Itoa(port)))
	if err != nil {
		return err
	}
	return conn.Close()
}

func dynamicFlagEnabled(data map[string]string) bool {
	return data["dynamic_flag_enabled"] == "true" || data["dynamic_flag_enabled"] == "1"
}

// cleanupDynamicFlagRuntime은 로컬 마운트 원본을 지우기 전에 서버 측 lease를
// 폐기한다. Docker 정리가 실패해도 실행하여, 오래된 컨테이너가 유효한 동적 플래그를
// 유지하지 못하게 한다.
func (m *Manager) cleanupDynamicFlagRuntime(instanceID string, data map[string]string, reason string) {
	if !dynamicFlagEnabled(data) {
		return
	}
	cleanupCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	m.revokeDynamicFlag(cleanupCtx, instanceID, "", reason)
	m.removeDynamicFlagFile(instanceID)
}

// CleanupInstanceRuntime은 lifecycle TTL watcher와 reconciler가 호출하는
// 인스턴스 정리 hook이다. dynamic_instance만 Learn issuance와 host runtime file을
// 즉시 폐기하며, static/regex는 metadata를 읽기만 하고 아무 작업도 하지 않는다.
func (m *Manager) CleanupInstanceRuntime(instanceID string, data map[string]string, reason string) {
	// static/regex compatibility mounts carry no participant secret, but their
	// source file must still follow the container lifecycle and never leak after
	// TTL/reconcile cleanup.
	m.removeCompatibilityFlagFile(instanceID)
	if dynamicFlagEnabled(data) {
		m.cleanupDynamicFlagRuntime(instanceID, data, reason)
		return
	}
	if len(data) == 0 {
		m.cleanupUnrecordedDynamicFlagRuntime(instanceID, reason)
	}
}

// cleanupStartedContainer는 아직 Redis에 저장되지 않은 생성 시도를 정리한다.
// 동적 플래그 폐기는 실패한 컨테이너 생명주기의 보안 경계이므로 Docker 정리 성공과
// 독립적으로 수행한다.
func (m *Manager) cleanupStartedContainer(
	ctx context.Context,
	containerID string,
	hostPort int,
	instanceID string,
	dynamicEnabled bool,
	reason string,
) {
	m.removeCompatibilityFlagFile(instanceID)
	if dynamicEnabled {
		m.cleanupDynamicFlagRuntime(instanceID, map[string]string{"dynamic_flag_enabled": "true"}, reason)
	}

	removed := true
	if containerID == "" {
		log.Printf("[Manager] 시작 실패 컨테이너 identity 미확인 (%s)", instanceID)
		removed = false
	} else {
		if err := m.docker.ContainerRemove(ctx, containerID, container.RemoveOptions{Force: true}); err != nil && !errdefs.IsNotFound(err) {
			// Docker가 포트를 아직 점유할 수 있으므로 이 경우에는 Redis 할당을 해제하지 않는다.
			log.Printf("[Manager] 시작 실패 컨테이너 정리 실패 (%s): %v", containerID, err)
			removed = false
		}
	}
	if removed && hostPort > 0 {
		m.portAllocator.ResolvePortConflict(PortConflict{Port: hostPort, Owner: instanceID, ContainerID: containerID})
		m.portAllocator.ResolvePortConflict(PortConflict{Port: hostPort, Owner: instanceID, ContainerID: "ctf-" + instanceID})
		if err := m.portAllocator.ReleaseOwned(ctx, hostPort, instanceID); err != nil {
			log.Printf("[Manager] 시작 실패 포트 쿨다운 보류 (%s): %v", instanceID, err)
		}
	}
}

var deleteInstanceRecordScript = redis.NewScript(`
local instanceType = redis.call("TYPE", KEYS[1]).ok
local userType = redis.call("TYPE", KEYS[2]).ok
if (instanceType ~= "none" and instanceType ~= "hash") or (userType ~= "none" and userType ~= "set") then
  return redis.error_reply("invalid instance metadata type")
end
if instanceType ~= "none" then
  for index, field in ipairs({"container_id", "user_id", "port"}) do
    if (redis.call("HGET", KEYS[1], field) or "") ~= ARGV[index + 1] then return -1 end
  end
end
redis.call("SREM", KEYS[2], ARGV[1])
redis.call("DEL", KEYS[1])
return 1
`)

// DeleteInstanceRecord는 호출자가 제거와 쿨다운을 확인한 뒤 관측한 인스턴스와
// 소유자 참조만 원자적으로 정리한다.
func DeleteInstanceRecord(ctx context.Context, rdb *redis.Client, instanceID string, data map[string]string) error {
	if instanceID == "" || data["user_id"] == "" {
		return fmt.Errorf("인스턴스 정리 소유자가 올바르지 않습니다")
	}
	result, err := deleteInstanceRecordScript.Run(ctx, rdb,
		[]string{"instance:" + instanceID, "user_instances:" + data["user_id"]},
		instanceID, data["container_id"], data["user_id"], data["port"]).Int64()
	if err != nil {
		return fmt.Errorf("인스턴스 정리 저장 실패: %w", err)
	}
	if result != 1 {
		return fmt.Errorf("인스턴스 정리 대상이 변경되었습니다")
	}
	return nil
}

// FinalizeRemovedInstance는 Docker 제거 성공/NotFound를 확인한 호출자 전용이다.
// 포트가 이미 재예약됐으면 새 예약을 건드리지 않고 이전 메타데이터 CAS만 재시도한다.
func FinalizeRemovedInstance(ctx context.Context, rdb *redis.Client, allocator *PortAllocator, instanceID string, data map[string]string, observedPorts ...int) error {
	port, err := strconv.Atoi(data["port"])
	if err != nil || port < 1 || port > 65535 {
		return fmt.Errorf("인스턴스 포트가 올바르지 않습니다")
	}
	ports := map[int]bool{port: true}
	for _, observed := range observedPorts {
		if observed < 1 || observed > 65535 {
			return fmt.Errorf("관측한 인스턴스 포트가 올바르지 않습니다")
		}
		ports[observed] = true
	}
	for reserved := range ports {
		allocator.ResolvePortConflict(PortConflict{Port: reserved, Owner: instanceID, ContainerID: data["container_id"]})
		// 이 이름은 같은 고유 instance identity에서 생성된다.
		allocator.ResolvePortConflict(PortConflict{Port: reserved, Owner: instanceID, ContainerID: "ctf-" + instanceID})
		if err := allocator.ReleaseOwned(ctx, reserved, instanceID); err != nil {
			var mismatch *ReservationOwnerMismatchError
			if !errors.As(err, &mismatch) {
				return err
			}
		}
	}
	return DeleteInstanceRecord(ctx, rdb, instanceID, data)
}

// cleanupUnavailableInstance는 종료되었거나 전용 네트워크에서 분리된 인스턴스의
// Redis 참조와 포트 할당을 함께 제거해 사용자가 고아 인스턴스 때문에 새 환경을 만들지 못하는 일을 막는다.
func (m *Manager) cleanupUnavailableInstance(ctx context.Context, instanceID string, data map[string]string, reason string) bool {
	m.removeCompatibilityFlagFile(instanceID)
	m.cleanupDynamicFlagRuntime(instanceID, data, reason)
	containerID := data["container_id"]
	if containerID == "" {
		return false
	}
	if err := m.docker.ContainerRemove(ctx, containerID, container.RemoveOptions{Force: true}); err != nil && !errdefs.IsNotFound(err) {
		// 삭제에 실패한 컨테이너는 포트를 계속 점유할 수 있으므로 메타데이터를 보존해 재시도한다.
		log.Printf("[Manager] 사용할 수 없는 컨테이너 정리 실패 (%s): %v", containerID, err)
		return false
	}
	if err := FinalizeRemovedInstance(ctx, m.rdb, m.portAllocator, instanceID, data); err != nil {
		log.Printf("[Manager] 사용할 수 없는 인스턴스 메타데이터 정리 보류 (%s): %v", instanceID, err)
		return false
	}
	return true
}

func isInstanceExpired(data map[string]string) bool {
	expiresAt, err := strconv.ParseInt(data["expires_at"], 10, 64)
	if err != nil || expiresAt <= 0 {
		return true
	}
	return expiresAt <= time.Now().Unix()
}

// ResolveInstanceTarget은 HTTP/WebSocket 프록시가 사용하는 대상 해석 경계다.
// 이미 종료된 컨테이너를 "running" Redis 레코드만으로 프록시하지 않으며, 전용 네트워크 주소만 허용한다.
func (m *Manager) ResolveInstanceTarget(ctx context.Context, instanceID string) (string, int, error) {
	instanceKey := fmt.Sprintf("instance:%s", instanceID)
	data, err := m.rdb.HGetAll(ctx, instanceKey).Result()
	if err != nil || len(data) == 0 {
		return "", 0, fmt.Errorf("실습 환경을 찾을 수 없습니다")
	}
	if isInstanceExpired(data) {
		m.cleanupUnavailableInstance(ctx, instanceID, data, "instance_expired")
		return "", 0, fmt.Errorf("실습 환경 사용 시간이 만료되었습니다")
	}
	if err := m.requireCurrentInstanceEnvironment(ctx, instanceID, data); err != nil {
		return "", 0, err
	}

	containerPort, err := strconv.Atoi(data["container_port"])
	if err != nil || containerPort <= 0 {
		return "", 0, fmt.Errorf("실습 환경 포트 정보가 올바르지 않습니다")
	}

	host, port, healthStatus, targetErr := m.getRunningContainerTarget(
		ctx,
		data["container_id"],
		containerPort,
		instanceID,
		data["environment_verified_image_id"],
		dynamicFlagEnabled(data),
	)
	if targetErr != nil {
		code := "unavailable"
		if readinessErr, ok := targetErr.(*EnvironmentReadinessError); ok {
			code = readinessErr.Code
			if code == "container_exited" || code == "network_unavailable" || code == "container_mismatch" {
				m.cleanupUnavailableInstance(ctx, instanceID, data, "reconcile_cleanup")
			} else {
				status := "unavailable"
				if code == "healthcheck_unhealthy" {
					status = "unhealthy"
				}
				m.rdb.HSet(ctx, instanceKey, "status", status, "failure_code", code)
			}
		}
		return "", 0, targetErr
	}
	if healthStatus == "starting" {
		m.rdb.HSet(ctx, instanceKey, "status", "starting", "failure_code", "container_starting")
		return "", 0, &EnvironmentReadinessError{Code: "container_starting"}
	}
	// ContainerInspect의 Running/Healthy 상태만으로는 애플리케이션 프로세스가
	// 실제 내부 포트에서 리스닝 중인지 보장할 수 없다. 생성 직후의 race나
	// 프로세스 재시작을 여기서 정규화해, 첫 프록시 요청이 불투명한 502로
	// 끝나지 않고 공통 port_unreachable 계약을 사용하게 한다.
	probeCtx, cancel := context.WithTimeout(ctx, readinessDialTimeout)
	probeErr := probeContainerPort(probeCtx, host, port)
	cancel()
	if probeErr != nil {
		m.rdb.HSet(ctx, instanceKey, "status", "unavailable", "failure_code", "port_unreachable")
		return "", 0, &EnvironmentReadinessError{Code: "port_unreachable"}
	}
	return host, port, nil
}

func (m *Manager) cleanAndCountInstances(ctx context.Context, userKey string) (int, error) {
	members, err := m.rdb.SMembers(ctx, userKey).Result()
	if err != nil {
		return 0, err
	}

	activeCount := 0
	for _, instanceID := range members {
		instanceKey := fmt.Sprintf("instance:%s", instanceID)
		data, err := m.rdb.HGetAll(ctx, instanceKey).Result()
		if err != nil {
			return 0, err
		}
		if len(data) == 0 {
			// 좀비 참조 — instance 키가 없으므로 셋에서 제거
			if err := m.rdb.SRem(ctx, userKey, instanceID).Err(); err != nil {
				return 0, err
			}
			log.Printf("[Manager] 좀비 인스턴스 참조 정리: %s\n", instanceID)
			continue
		}
		if isInstanceExpired(data) {
			if !m.cleanupUnavailableInstance(ctx, instanceID, data, "instance_expired") {
				activeCount++
			}
			log.Printf("[Manager] 만료 인스턴스 정리: %s\n", instanceID)
			continue
		}
		if _, _, metadataErr := instanceEnvironmentMetadata(data); metadataErr != nil {
			// revision/image identity가 없던 레거시 Redis 인스턴스는 현재 검증
			// 결과와 연결할 수 없으므로 새 인스턴스 한도를 점유하지 못하게 한다.
			if !m.cleanupUnavailableInstance(ctx, instanceID, data, "environment_drift") {
				activeCount++
			}
			log.Printf("[Manager] 검증 증거 없는 레거시 인스턴스 정리: %s\n", instanceID)
			continue
		}

		containerPort, portErr := strconv.Atoi(data["container_port"])
		_, _, healthStatus, targetErr := m.getRunningContainerTarget(
			ctx,
			data["container_id"],
			containerPort,
			instanceID,
			data["environment_verified_image_id"],
			dynamicFlagEnabled(data),
		)
		if targetErr == nil {
			if healthStatus == "starting" {
				m.rdb.HSet(ctx, instanceKey, "status", "starting", "failure_code", "container_starting")
			}
			activeCount++
			continue
		}

		code := ReadinessErrorCode(targetErr)
		if portErr == nil && code == "inspect_failed" {
			// Docker API의 일시 장애는 실제 컨테이너를 삭제하는 근거가 아니다.
			activeCount++
			continue
		}
		if !m.cleanupUnavailableInstance(ctx, instanceID, data, "reconcile_cleanup") {
			activeCount++
		}
		log.Printf("[Manager] 사용할 수 없는 인스턴스 정리: %s (%s)\n", instanceID, code)
	}
	return activeCount, nil
}

// fetchChallengeEnv는 learn-service에서 챌린지 환경 정보를 조회한다.
func (m *Manager) fetchInstanceChallengeEnv(ctx context.Context, challengeID int) (*challengeEnvResponse, error) {
	return m.fetchChallengeEnv(ctx, challengeID, challengeEnvironmentPurposeInstance)
}

func (m *Manager) fetchVerificationChallengeEnv(ctx context.Context, challengeID int) (*challengeEnvResponse, error) {
	return m.fetchChallengeEnv(ctx, challengeID, challengeEnvironmentPurposeVerification)
}

func (m *Manager) fetchChallengeEnv(
	ctx context.Context,
	challengeID int,
	purpose challengeEnvironmentPurpose,
) (*challengeEnvResponse, error) {
	if challengeID <= 0 || (purpose != challengeEnvironmentPurposeInstance && purpose != challengeEnvironmentPurposeVerification) {
		return nil, &EnvironmentReadinessError{Code: "invalid_environment"}
	}
	endpoint, err := url.Parse(fmt.Sprintf("%s/internal/challenges/%d/env", m.learnURL, challengeID))
	if err != nil {
		return nil, &EnvironmentReadinessError{Code: "environment_unavailable"}
	}
	query := endpoint.Query()
	query.Set("purpose", string(purpose))
	endpoint.RawQuery = query.Encode()

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, endpoint.String(), nil)
	if err != nil {
		return nil, &EnvironmentReadinessError{Code: "environment_unavailable"}
	}
	if m.internalAPIKey != "" {
		req.Header.Set("X-Internal-Key", m.internalAPIKey)
	}

	resp, err := m.httpClient.Do(req)
	if err != nil {
		return nil, &EnvironmentReadinessError{Code: "environment_unavailable"}
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		// Learn의 응답 본문과 detail은 문제 설정 또는 내부 상태를 포함할 수 있으므로
		// 사용자 오류와 로그 어느 쪽에도 전달하지 않는다.
		if resp.StatusCode == http.StatusUnprocessableEntity {
			// Learn exposes a stable contract code in the detail suffix, but the
			// rest of the response body is private and must never cross the
			// orchestrator boundary. Read only a small bounded body and match the
			// allowlisted code without logging or returning the detail.
			body, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
			if bytes.Contains(body, []byte("(dynamic_flag_required)")) {
				return nil, &EnvironmentReadinessError{Code: "dynamic_flag_required"}
			}
		}
		switch resp.StatusCode {
		case http.StatusBadRequest, http.StatusNotFound, http.StatusUnprocessableEntity:
			return nil, &EnvironmentReadinessError{Code: "invalid_environment"}
		case http.StatusConflict:
			return nil, &EnvironmentReadinessError{Code: "environment_verification_required"}
		default:
			return nil, &EnvironmentReadinessError{Code: "environment_unavailable"}
		}
	}

	var envResp challengeEnvResponse
	if err := json.NewDecoder(resp.Body).Decode(&envResp); err != nil {
		return nil, &EnvironmentReadinessError{Code: "environment_unavailable"}
	}
	return &envResp, nil
}

// instanceEnvironmentMetadata is the immutable environment evidence stored
// with a running instance. Older Redis records did not carry this evidence and
// are intentionally treated as stale rather than trusted.
func instanceEnvironmentMetadata(data map[string]string) (int, string, error) {
	revision, err := strconv.Atoi(data["environment_revision"])
	if err != nil || revision <= 0 || data["environment_verified_image_id"] == "" {
		return 0, "", &EnvironmentReadinessError{Code: "environment_verification_required"}
	}
	return revision, data["environment_verified_image_id"], nil
}

func validateCurrentInstanceEnvironment(
	data map[string]string,
	envInfo *challengeEnvResponse,
) (string, error) {
	storedRevision, storedImageID, err := instanceEnvironmentMetadata(data)
	if err != nil {
		return "", err
	}
	if envInfo == nil || !envInfo.EnvironmentReady || envInfo.EnvironmentRevision <= 0 ||
		envInfo.EnvironmentVerifiedImageID == "" ||
		envInfo.EnvironmentRevision != storedRevision ||
		envInfo.EnvironmentVerifiedImageID != storedImageID {
		return "", &EnvironmentReadinessError{Code: "environment_verification_required"}
	}
	if err := validateCurrentDynamicFlagPolicy(data, envInfo.DynamicFlag); err != nil {
		return "", err
	}
	return storedImageID, nil
}

func validateInstanceCreationEnvironment(envInfo *challengeEnvResponse) error {
	if envInfo == nil || envInfo.EnvType == "none" || envInfo.DockerImage == "" ||
		envInfo.ContainerPort <= 0 || envInfo.EnvironmentRevision <= 0 {
		return &EnvironmentReadinessError{Code: "invalid_environment"}
	}
	if !envInfo.EnvironmentReady || envInfo.EnvironmentVerifiedImageID == "" {
		return &EnvironmentReadinessError{Code: "environment_verification_required"}
	}
	return nil
}

// requireCurrentInstanceEnvironment refreshes the authoritative Learn state on
// every externally usable path. A revision, verified image identity, or ready
// state mismatch destroys the stale instance before returning a safe error.
func (m *Manager) requireCurrentInstanceEnvironment(
	ctx context.Context,
	instanceID string,
	data map[string]string,
) error {
	challengeID, err := strconv.Atoi(data["challenge_id"])
	if err != nil || challengeID <= 0 {
		m.cleanupUnavailableInstance(ctx, instanceID, data, "environment_drift")
		return &EnvironmentReadinessError{Code: "environment_verification_required"}
	}

	envInfo, err := m.fetchInstanceChallengeEnv(ctx, challengeID)
	if err != nil {
		// The current state is unavailable, so no old verification result may
		// authorize a connection or extension. Keep no zombie instance alive.
		m.cleanupUnavailableInstance(ctx, instanceID, data, "environment_drift")
		return err
	}
	expectedImageID, err := validateCurrentInstanceEnvironment(data, envInfo)
	if err != nil {
		m.cleanupUnavailableInstance(ctx, instanceID, data, "environment_drift")
		return err
	}

	inspect, inspectErr := m.docker.ContainerInspect(ctx, data["container_id"])
	if inspectErr != nil || inspect.Image == "" {
		m.cleanupUnavailableInstance(ctx, instanceID, data, "environment_drift")
		return &EnvironmentReadinessError{Code: "inspect_failed"}
	}
	if inspect.Image != expectedImageID {
		m.cleanupUnavailableInstance(ctx, instanceID, data, "environment_drift")
		return &EnvironmentReadinessError{Code: "environment_verification_required"}
	}
	return nil
}

// recordEnvironmentVerification sends only the minimal audit metadata that
// learn-service is allowed to persist. It intentionally excludes Docker logs,
// container IDs, network addresses, and challenge secrets.
func (m *Manager) recordEnvironmentVerification(
	ctx context.Context,
	challengeID int,
	record environmentVerificationRecord,
) error {
	if record.Status == "ready" && (record.ImageID == nil || *record.ImageID == "") {
		return fmt.Errorf("ready environment verification requires an image identity")
	}
	if record.Status == "failed" {
		// 실패한 검증은 이미지 ID를 신뢰 가능한 결과로 취급하지 않는다. Learn의
		// nullable 계약에 맞춰 빈 문자열 대신 JSON null을 전송한다.
		record.ImageID = nil
	}
	if m.internalAPIKey == "" {
		return fmt.Errorf("internal API key is not configured")
	}

	payload, err := json.Marshal(record)
	if err != nil {
		return fmt.Errorf("environment verification record serialization failed: %w", err)
	}

	endpoint := fmt.Sprintf(
		"%s/internal/challenges/%d/environment-verifications",
		m.learnURL,
		challengeID,
	)
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, bytes.NewReader(payload))
	if err != nil {
		return fmt.Errorf("environment verification record request creation failed: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Internal-Key", m.internalAPIKey)

	resp, err := m.httpClient.Do(req)
	if err != nil {
		return fmt.Errorf("environment verification record request failed: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK && resp.StatusCode != http.StatusCreated {
		return fmt.Errorf("environment verification record rejected (status=%d)", resp.StatusCode)
	}
	return nil
}

func verificationFailureCode(err error) *string {
	var readinessErr *EnvironmentReadinessError
	if !errors.As(err, &readinessErr) {
		code := "verification_execution_failed"
		return &code
	}

	switch readinessErr.Code {
	case "container_exited",
		"container_mismatch",
		"healthcheck_unhealthy",
		"inspect_failed",
		"invalid_environment",
		"network_unavailable",
		"port_unreachable",
		"start_timeout",
		"verification_in_progress",
		"verification_record_failed":
		code := readinessErr.Code
		return &code
	default:
		code := "verification_execution_failed"
		return &code
	}
}

// recordFailedEnvironmentVerification is best effort. It never replaces the
// execution failure returned to the caller.
func (m *Manager) recordFailedEnvironmentVerification(
	challengeID int,
	envInfo *challengeEnvResponse,
	verifiedBy string,
	imageID string,
	verificationErr error,
) {
	recordCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	var imageIDPtr *string
	if trimmedImageID := strings.TrimSpace(imageID); trimmedImageID != "" {
		imageIDPtr = &trimmedImageID
	}

	err := m.recordEnvironmentVerification(recordCtx, challengeID, environmentVerificationRecord{
		Revision:       envInfo.EnvironmentRevision,
		Status:         "failed",
		FailureCode:    verificationFailureCode(verificationErr),
		ImageReference: envInfo.DockerImage,
		ImageID:        imageIDPtr,
		VerifiedBy:     verifiedBy,
	})
	if err != nil {
		// Do not emit the payload or a downstream response body. Either could be
		// correlated with private challenge configuration.
		log.Printf(
			"[Manager] environment verification failure record was not persisted (challenge=%d, revision=%d)",
			challengeID,
			envInfo.EnvironmentRevision,
		)
	}
}

// requireCompetitionEnvironmentAccess는 대회 문제 컨테이너 생성 전에 Auth 서비스의
// 참가·배정·진행 기간 권한 경계를 다시 확인한다. 내부 키가 없거나 Auth 확인이 실패하면
// 대회 환경을 시작하지 않는다.
func (m *Manager) requireCompetitionEnvironmentAccess(
	ctx context.Context,
	competitionID string,
	userID string,
	challengeID int,
) error {
	if competitionID == "" {
		return fmt.Errorf("대회 문제 환경에는 competition_id가 필요합니다")
	}
	if m.internalAPIKey == "" {
		return fmt.Errorf("대회 문제 환경 권한 확인을 위한 내부 키가 설정되지 않았습니다")
	}

	payload, err := json.Marshal(map[string]interface{}{
		"user_id":      userID,
		"challenge_id": challengeID,
		"purpose":      "environment",
	})
	if err != nil {
		return fmt.Errorf("대회 환경 권한 요청 직렬화 실패: %w", err)
	}

	endpoint := fmt.Sprintf(
		"%s/internal/competitions/%s/access",
		m.authURL,
		url.PathEscape(competitionID),
	)
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, bytes.NewReader(payload))
	if err != nil {
		return fmt.Errorf("대회 환경 권한 요청 생성 실패: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Internal-Key", m.internalAPIKey)

	resp, err := m.httpClient.Do(req)
	if err != nil {
		return fmt.Errorf("대회 환경 권한 확인 실패: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("배정된 진행 중인 대회 문제만 환경을 시작할 수 있습니다")
	}

	var accessResponse struct {
		Allowed bool `json:"allowed"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&accessResponse); err != nil {
		return fmt.Errorf("대회 환경 권한 응답 파싱 실패: %w", err)
	}
	if !accessResponse.Allowed {
		return fmt.Errorf("배정된 진행 중인 대회 문제만 환경을 시작할 수 있습니다")
	}
	return nil
}

// RequireInstanceAccess는 인스턴스 소유자와, 대회 인스턴스인 경우 현재 대회 접근
// 조건까지 재검증한다. URL을 아는 것만으로 종료된 대회나 타인 인스턴스에 연결할 수 없게
// HTTP 프록시·WebSocket·연장 요청이 같은 경계를 사용한다.
func (m *Manager) RequireInstanceAccess(ctx context.Context, instanceID, userID string) error {
	instanceKey := fmt.Sprintf("instance:%s", instanceID)
	data, err := m.rdb.HGetAll(ctx, instanceKey).Result()
	if err != nil || len(data) == 0 {
		return fmt.Errorf("인스턴스를 찾을 수 없습니다: %s", instanceID)
	}
	if data["user_id"] != userID {
		return fmt.Errorf("해당 인스턴스에 대한 권한이 없습니다")
	}
	if isInstanceExpired(data) {
		m.cleanupUnavailableInstance(ctx, instanceID, data, "instance_expired")
		return fmt.Errorf("실습 환경 사용 시간이 만료되었습니다")
	}

	competitionID := data["competition_id"]
	if competitionID != "" {
		challengeID, err := strconv.Atoi(data["challenge_id"])
		if err != nil || challengeID <= 0 {
			return fmt.Errorf("대회 인스턴스의 챌린지 정보가 올바르지 않습니다")
		}
		if err := m.requireCompetitionEnvironmentAccess(ctx, competitionID, userID, challengeID); err != nil {
			return err
		}
	}

	// 워게임과 CTF 모두 이 경계를 거치므로, 이전 검증 revision이나 이미지
	// identity를 가진 인스턴스는 HTTP/WS/연장 전에 삭제되고 재생성해야 한다.
	return m.requireCurrentInstanceEnvironment(ctx, instanceID, data)
}

func challengeContainerLabels(challengeID int, instanceID string) map[string]string {
	return map[string]string{
		"ctf-challenge": "true",
		"challenge-id":  strconv.Itoa(challengeID),
		"instance-id":   instanceID,
		// 빈 값도 명시해야 이미지의 운영 Compose 라벨 상속을 차단한다.
		"com.docker.compose.project": "",
		"com.docker.compose.service": "",
	}
}

func instanceContainerLabels(req CreateRequest, instanceID string) map[string]string {
	labels := challengeContainerLabels(req.ChallengeID, instanceID)
	labels["user-id"] = req.UserID
	return labels
}

func verificationContainerLabels(challengeID int, verificationID string) map[string]string {
	labels := challengeContainerLabels(challengeID, verificationID)
	labels["verification"] = "true"
	return labels
}

// CreateInstance는 새 챌린지 컨테이너를 생성한다.
func (m *Manager) CreateInstance(ctx context.Context, req CreateRequest) (*Instance, error) {
	endOperation, err := acquireChallengeOperation(ctx, req.ChallengeID, false)
	if err != nil {
		return nil, err
	}
	defer endOperation()
	// admin:settings에서 동적 설정 읽기
	ttl, maxPerUser, maxContainers := m.getAdminSettings(ctx)

	// 사용자당 최대 인스턴스 수 확인 (좀비 참조 자동 정리)
	userKey := fmt.Sprintf("user_instances:%s", req.UserID)
	activeCount, err := m.cleanAndCountInstances(ctx, userKey)
	if err != nil {
		return nil, fmt.Errorf("Redis 오류: %w", err)
	}
	if activeCount >= maxPerUser {
		return nil, fmt.Errorf("사용자당 최대 %d개의 인스턴스만 허용됩니다", maxPerUser)
	}

	// learn-service에서 챌린지 환경 정보 조회
	envInfo, err := m.fetchInstanceChallengeEnv(ctx, req.ChallengeID)
	if err != nil {
		return nil, fmt.Errorf("챌린지 환경 조회 실패: %w", err)
	}
	if err := validateInstanceCreationEnvironment(envInfo); err != nil {
		return nil, err
	}
	if envInfo.ChallengeType == "competition" {
		if err := m.requireCompetitionEnvironmentAccess(
			ctx,
			req.CompetitionID,
			req.UserID,
			req.ChallengeID,
		); err != nil {
			return nil, err
		}
	} else if req.CompetitionID != "" {
		return nil, fmt.Errorf("워게임 환경에는 competition_id를 사용할 수 없습니다")
	}

	// 생성 identity와 보호 경계를 포트 예약보다 먼저 만든다.
	randBytes := make([]byte, 16)
	if _, err := crypto_rand.Read(randBytes); err != nil {
		return nil, fmt.Errorf("인스턴스 ID 생성 실패: %w", err)
	}
	instanceID := fmt.Sprintf("inst_%s", hex.EncodeToString(randBytes))
	endCreation := m.portAllocator.BeginCreation(instanceID)
	defer endCreation()
	hostPort, err := m.portAllocator.AllocateWithinLimitForOwner(ctx, maxContainers, instanceID)
	if err != nil {
		return nil, fmt.Errorf("포트 할당 실패: %w", err)
	}
	releasePort := func() {
		if err := m.portAllocator.ReleaseOwned(ctx, hostPort, instanceID); err != nil {
			log.Printf("[Manager] 생성 실패 포트 쿨다운 보류 (%s): %v", instanceID, err)
		}
	}
	containerName := fmt.Sprintf("ctf-%s", instanceID)
	requestedExpiresAt := time.Now().UTC().Add(time.Duration(ttl) * time.Minute)

	// 생성 요청 중에도 build tag가 다른 이미지를 가리킬 수 있다. 동적 플래그 발급과
	// 컨테이너 생성 전에 기준 환경을 다시 읽고, 아래 ContainerStart 직전에도 한 번 더
	// 같은 검사를 수행한다.
	currentEnvInfo, currentEnvErr := m.fetchInstanceChallengeEnv(ctx, req.ChallengeID)
	if currentEnvErr != nil {
		releasePort()
		return nil, currentEnvErr
	}
	if err := validateInstanceCreationEnvironment(currentEnvInfo); err != nil {
		releasePort()
		return nil, err
	}
	if currentEnvInfo.EnvironmentRevision != envInfo.EnvironmentRevision ||
		currentEnvInfo.EnvironmentVerifiedImageID != envInfo.EnvironmentVerifiedImageID ||
		currentEnvInfo.DockerImage != envInfo.DockerImage ||
		!sameDynamicFlagPolicy(currentEnvInfo.DynamicFlag, envInfo.DynamicFlag) {
		releasePort()
		return nil, &EnvironmentReadinessError{Code: "environment_verification_required"}
	}

	dynamicRuntimePath, err := dynamicFlagPolicyRuntimePath(envInfo.DynamicFlag)
	if err != nil {
		releasePort()
		return nil, err
	}
	dynamicEnabled := envInfo.DynamicFlag.Enabled
	dynamicFlagSource := ""
	if dynamicEnabled {
		requestedExpiresAt, err = dynamicFlagRequestedExpiry(time.Now().UTC(), requestedExpiresAt, envInfo.DynamicFlag)
		if err != nil {
			releasePort()
			return nil, err
		}
		var competitionID *string
		if req.CompetitionID != "" {
			competitionID = &req.CompetitionID
		}
		issued, issueErr := m.issueDynamicFlag(ctx, dynamicFlagIssueRequest{
			ChallengeID:         req.ChallengeID,
			InstanceID:          instanceID,
			UserID:              req.UserID,
			CompetitionID:       competitionID,
			EnvironmentRevision: envInfo.EnvironmentRevision,
			VerifiedImageID:     envInfo.EnvironmentVerifiedImageID,
			ExpiresAt:           requestedExpiresAt,
		}, envInfo.DynamicFlag)
		if issueErr != nil {
			releasePort()
			return nil, issueErr
		}
		// Keep the instance lifetime no later than the server-side flag lease.
		requestedExpiresAt = issued.ExpiresAt.UTC()
		dynamicFlagSource, err = m.writeDynamicFlagFile(instanceID, issued.Flag)
		issued.Flag = ""
		if err != nil {
			m.revokeDynamicFlag(context.Background(), instanceID, issued.InstanceLocator, "issuance_materialization_failed")
			releasePort()
			return nil, err
		}
	}
	compatibilityFlagSource := ""
	compatibilityFlagEnabled := false
	if !dynamicEnabled {
		needsCompatibilityFlag, inspectErr := m.imageNeedsCompatibilityFlag(ctx, envInfo.DockerImage)
		if inspectErr != nil {
			releasePort()
			return nil, inspectErr
		}
		if needsCompatibilityFlag {
			compatibilityFlagSource, err = m.writeCompatibilityFlagFile(instanceID)
			if err != nil {
				releasePort()
				return nil, err
			}
			compatibilityFlagEnabled = true
		}
	}

	// Docker 컨테이너 생성
	containerPort := strconv.Itoa(envInfo.ContainerPort)
	portBinding := nat.PortMap{
		nat.Port(containerPort + "/tcp"): []nat.PortBinding{
			// 외부 포트 직접 접근은 우회 경로가 되므로 호스트 loopback에만 바인딩한다.
			// 공개 접근은 인증·소유권을 확인하는 Env 프록시를 통해서만 제공한다.
			{HostIP: "127.0.0.1", HostPort: strconv.Itoa(hostPort)},
		},
	}

	policy := security.DefaultPolicy()
	hostConfig := policy.ToHostConfig()
	hostConfig.PortBindings = portBinding
	if dynamicEnabled {
		applyDynamicFlagRuntime(hostConfig, dynamicFlagSource, dynamicRuntimePath)
	} else if compatibilityFlagEnabled {
		applyDynamicFlagRuntime(hostConfig, compatibilityFlagSource, dynamicFlagRuntimePath)
	}

	// 챌린지 네트워크에 연결 (설정되어 있는 경우)
	var networkConfig *network.NetworkingConfig
	if m.challengeNet != "" {
		networkConfig = &network.NetworkingConfig{
			EndpointsConfig: map[string]*network.EndpointSettings{
				m.challengeNet: {},
			},
		}
	}

	containerConfig := &container.Config{
		Image:  envInfo.DockerImage,
		Labels: instanceContainerLabels(req, instanceID),
		ExposedPorts: nat.PortSet{
			nat.Port(containerPort + "/tcp"): struct{}{},
		},
	}
	if dynamicEnabled {
		containerConfig.Env = []string{"FLAG_PATH=" + dynamicRuntimePath}
	} else if compatibilityFlagEnabled {
		containerConfig.Env = []string{"FLAG_PATH=" + dynamicFlagRuntimePath}
	}

	resp, err := m.docker.ContainerCreate(ctx,
		containerConfig,
		hostConfig,
		networkConfig,
		nil,
		containerName,
	)
	if err != nil {
		// 생성 응답이 실패해도 Docker에는 생성됐을 수 있다. 요청 고유 이름으로
		// 제거를 확인한 뒤에만 쿨다운을 등록한다.
		m.cleanupStartedContainer(ctx, containerName, hostPort, instanceID, dynamicEnabled, "container_start_failed")
		return nil, fmt.Errorf("컨테이너 생성 실패: %w", err)
	}
	if resp.ID == "" {
		m.cleanupStartedContainer(ctx, containerName, hostPort, instanceID, dynamicEnabled, "container_start_failed")
		return nil, fmt.Errorf("컨테이너 생성 결과를 확인할 수 없습니다")
	}

	// Docker tag는 재빌드되면 다른 이미지를 가리킬 수 있다. 컨테이너 생성과
	// 시작 사이에도 Learn의 revision이 바뀔 수 있으므로, 시작 직전에 현재
	// instance-purpose 결과를 한 번 더 읽어 검증 evidence를 재확인한다.
	currentEnvInfo, currentEnvErr = m.fetchInstanceChallengeEnv(ctx, req.ChallengeID)
	if currentEnvErr != nil {
		m.cleanupStartedContainer(ctx, resp.ID, hostPort, instanceID, dynamicEnabled, "environment_drift")
		return nil, currentEnvErr
	}
	if err := validateInstanceCreationEnvironment(currentEnvInfo); err != nil {
		m.cleanupStartedContainer(ctx, resp.ID, hostPort, instanceID, dynamicEnabled, "environment_drift")
		return nil, err
	}
	if currentEnvInfo.EnvironmentRevision != envInfo.EnvironmentRevision ||
		currentEnvInfo.EnvironmentVerifiedImageID != envInfo.EnvironmentVerifiedImageID ||
		currentEnvInfo.DockerImage != envInfo.DockerImage ||
		!sameDynamicFlagPolicy(currentEnvInfo.DynamicFlag, envInfo.DynamicFlag) {
		m.cleanupStartedContainer(ctx, resp.ID, hostPort, instanceID, dynamicEnabled, "environment_drift")
		return nil, &EnvironmentReadinessError{Code: "environment_verification_required"}
	}

	// 현재 Learn revision에서 검증한 정확한 image ID인지 반드시 확인한다.
	inspect, inspectErr := m.docker.ContainerInspect(ctx, resp.ID)
	if inspectErr != nil || inspect.Image == "" {
		m.cleanupStartedContainer(ctx, resp.ID, hostPort, instanceID, dynamicEnabled, "environment_drift")
		return nil, &EnvironmentReadinessError{Code: "inspect_failed"}
	}
	if inspect.Image != envInfo.EnvironmentVerifiedImageID {
		m.cleanupStartedContainer(ctx, resp.ID, hostPort, instanceID, dynamicEnabled, "environment_drift")
		return nil, &EnvironmentReadinessError{Code: "environment_verification_required"}
	}

	// 컨테이너 시작
	if err := m.docker.ContainerStart(ctx, resp.ID, container.StartOptions{}); err != nil {
		// 롤백: 컨테이너 삭제 + 포트 해제
		m.cleanupStartedContainer(ctx, resp.ID, hostPort, instanceID, dynamicEnabled, "container_start_failed")
		return nil, fmt.Errorf("컨테이너 시작 실패: %w", err)
	}

	// ContainerStart 성공은 프로세스가 실제로 리스닝 중이라는 뜻이 아니다.
	// Redis에 running 상태를 기록하기 전에 전용 네트워크·내부 포트·HEALTHCHECK를 확인한다.
	if err := m.waitForContainerReady(
		ctx,
		resp.ID,
		envInfo.ContainerPort,
		instanceID,
		dynamicEnabled,
	); err != nil {
		log.Printf(
			"[Manager] 컨테이너 준비 상태 실패 (challenge=%d, container=%s, code=%s)",
			req.ChallengeID,
			resp.ID,
			ReadinessErrorCode(err),
		)
		m.cleanupStartedContainer(ctx, resp.ID, hostPort, instanceID, dynamicEnabled, "readiness_failed")
		return nil, err
	}

	instance := &Instance{
		ID:             instanceID,
		UserID:         req.UserID,
		ChallengeID:    req.ChallengeID,
		ContainerID:    resp.ID,
		Port:           hostPort,
		ContainerPort:  envInfo.ContainerPort,
		Status:         "running",
		ExpiresAt:      requestedExpiresAt,
		CreatedAt:      time.Now().UTC(),
		ExtendCount:    0,
		MaxExtendCount: m.maxExtendCount,
	}

	// Redis에 인스턴스 정보 저장
	instanceKey := fmt.Sprintf("instance:%s", instanceID)
	instanceMetadata := map[string]interface{}{
		"user_id":                       instance.UserID,
		"challenge_id":                  instance.ChallengeID,
		"competition_id":                req.CompetitionID,
		"container_id":                  instance.ContainerID,
		"port":                          instance.Port,
		"container_port":                instance.ContainerPort,
		"environment_revision":          envInfo.EnvironmentRevision,
		"environment_verified_image_id": envInfo.EnvironmentVerifiedImageID,
		"dynamic_flag_enabled":          dynamicEnabled,
		"status":                        instance.Status,
		"expires_at":                    instance.ExpiresAt.Unix(),
		"created_at":                    instance.CreatedAt.Unix(),
		"extend_count":                  instance.ExtendCount,
		"max_extend_count":              instance.MaxExtendCount,
	}
	if dynamicEnabled {
		instanceMetadata["dynamic_flag_policy_revision"] = envInfo.DynamicFlag.PolicyRevision
		instanceMetadata["dynamic_flag_runtime_path"] = dynamicRuntimePath
	}
	if err := m.rdb.HSet(ctx, instanceKey, instanceMetadata).Err(); err != nil {
		// 롤백: 컨테이너 삭제 + 포트 해제
		m.cleanupStartedContainer(ctx, resp.ID, hostPort, instanceID, dynamicEnabled, "metadata_persist_failed")
		return nil, fmt.Errorf("인스턴스 저장 실패: %w", err)
	}

	// 사용자 인스턴스 목록에 추가
	if err := m.rdb.SAdd(ctx, userKey, instanceID).Err(); err != nil {
		log.Printf("[Manager] 사용자 인스턴스 목록 추가 실패: %v\n", err)
	}

	// TTL 설정
	// TTL watcher가 컨테이너·포트·사용자 참조를 함께 정리할 시간을 보장한다.
	// expires_at은 실제 접근 차단 시각이고 Redis 키 TTL은 정리 메타데이터 보존 시각이다.
	if err := m.rdb.ExpireAt(ctx, instanceKey, instance.ExpiresAt.Add(instanceMetadataRetention)).Err(); err != nil {
		persisted, fetchErr := m.rdb.HGetAll(ctx, instanceKey).Result()
		if fetchErr == nil && len(persisted) > 0 {
			m.cleanupUnavailableInstance(ctx, instanceID, persisted, "metadata_persist_failed")
		} else {
			m.cleanupStartedContainer(ctx, resp.ID, hostPort, instanceID, dynamicEnabled, "metadata_persist_failed")
		}
		return nil, fmt.Errorf("인스턴스 만료 시각 저장 실패: %w", err)
	}

	log.Printf("[Manager] 인스턴스 생성: %s (이미지: %s, 포트 %d→%s, TTL %d분)\n",
		instanceID, envInfo.DockerImage, hostPort, containerPort, ttl)
	return instance, nil
}

// EnvironmentVerificationResult는 관리자 실행 전 검증의 공개 가능한 결과만 반환한다.
// Docker 로그·네트워크 주소·컨테이너 ID는 응답에 포함하지 않는다.
type EnvironmentVerificationResult struct {
	Status    string    `json:"status"`
	CheckedAt time.Time `json:"checked_at"`
	Revision  int       `json:"revision"`
}

// VerifyChallengeEnvironment은 관리자가 문제를 공개·배정하기 전에 이미지의 실제 실행 계약을 확인한다.
// 검증 컨테이너는 외부 포트를 열거나 Redis 인스턴스를 만들지 않으며 완료 후 항상 제거한다.
func (m *Manager) VerifyChallengeEnvironment(
	ctx context.Context,
	challengeID int,
	verifiedBy string,
) (*EnvironmentVerificationResult, error) {
	if challengeID <= 0 {
		return nil, &EnvironmentReadinessError{Code: "invalid_environment"}
	}
	if verifiedBy == "" {
		return nil, fmt.Errorf("verification actor is required")
	}
	endOperation, err := acquireChallengeOperation(ctx, challengeID, false)
	if err != nil {
		return nil, err
	}
	defer endOperation()

	envInfo, err := m.fetchVerificationChallengeEnv(ctx, challengeID)
	if err != nil {
		return nil, fmt.Errorf("챌린지 환경 조회 실패: %w", err)
	}
	failVerification := func(verificationErr error, imageID string) (*EnvironmentVerificationResult, error) {
		m.recordFailedEnvironmentVerification(challengeID, envInfo, verifiedBy, imageID, verificationErr)
		return nil, verificationErr
	}

	if envInfo.EnvironmentRevision <= 0 {
		return nil, &EnvironmentReadinessError{Code: "invalid_environment"}
	}
	if envInfo.EnvType == "none" || envInfo.DockerImage == "" || envInfo.ContainerPort <= 0 || m.challengeNet == "" {
		return failVerification(&EnvironmentReadinessError{Code: "invalid_environment"}, "")
	}

	lockKey := fmt.Sprintf("environment_verification:%d", challengeID)
	locked, err := m.rdb.SetNX(ctx, lockKey, "1", m.readinessTimeout+30*time.Second).Result()
	if err != nil {
		m.recordFailedEnvironmentVerification(challengeID, envInfo, verifiedBy, "", err)
		return nil, fmt.Errorf("환경 검증 잠금 설정 실패: %w", err)
	}
	if !locked {
		return nil, &EnvironmentReadinessError{Code: "verification_in_progress"}
	}
	defer m.rdb.Del(context.Background(), lockKey)

	randomBytes := make([]byte, 16)
	if _, err := crypto_rand.Read(randomBytes); err != nil {
		m.recordFailedEnvironmentVerification(challengeID, envInfo, verifiedBy, "", err)
		return nil, fmt.Errorf("검증 ID 생성 실패: %w", err)
	}
	verificationID := fmt.Sprintf("verify_%s", hex.EncodeToString(randomBytes))
	containerPort := strconv.Itoa(envInfo.ContainerPort)
	policy := security.DefaultPolicy()
	verificationHostConfig := policy.ToHostConfig()
	dynamicRuntimePath, dynamicPolicyErr := dynamicFlagPolicyRuntimePath(envInfo.DynamicFlag)
	if dynamicPolicyErr != nil {
		return failVerification(dynamicPolicyErr, "")
	}
	verificationDynamicEnabled := envInfo.DynamicFlag.Enabled
	verificationCompatibilityEnabled := false
	if verificationDynamicEnabled {
		// Verification must prove the read-only runtime mount contract without
		// asking Learn to issue a participant-bound dynamic flag.
		probeSource, probeErr := m.writeDynamicFlagFile(verificationID, "CTF_DYNAMIC_FLAG_PROBE")
		if probeErr != nil {
			return failVerification(probeErr, "")
		}
		defer m.removeDynamicFlagFile(verificationID)
		applyDynamicFlagRuntime(verificationHostConfig, probeSource, dynamicRuntimePath)
	} else {
		needsCompatibilityFlag, compatibilityErr := m.imageNeedsCompatibilityFlag(ctx, envInfo.DockerImage)
		if compatibilityErr != nil {
			return failVerification(compatibilityErr, "")
		}
		if needsCompatibilityFlag {
			compatibilitySource, writeErr := m.writeCompatibilityFlagFile(verificationID)
			if writeErr != nil {
				return failVerification(writeErr, "")
			}
			verificationCompatibilityEnabled = true
			defer m.removeCompatibilityFlagFile(verificationID)
			applyDynamicFlagRuntime(verificationHostConfig, compatibilitySource, dynamicFlagRuntimePath)
		}
	}
	networkConfig := &network.NetworkingConfig{
		EndpointsConfig: map[string]*network.EndpointSettings{
			m.challengeNet: {},
		},
	}

	verificationConfig := &container.Config{
		Image:  envInfo.DockerImage,
		Labels: verificationContainerLabels(challengeID, verificationID),
		ExposedPorts: nat.PortSet{
			nat.Port(containerPort + "/tcp"): struct{}{},
		},
	}
	if verificationDynamicEnabled {
		verificationConfig.Env = []string{"FLAG_PATH=" + dynamicRuntimePath}
	} else if verificationCompatibilityEnabled {
		verificationConfig.Env = []string{"FLAG_PATH=" + dynamicFlagRuntimePath}
	}

	response, err := m.docker.ContainerCreate(ctx,
		verificationConfig,
		verificationHostConfig,
		networkConfig,
		nil,
		"ctf-"+verificationID,
	)
	if err != nil {
		if verificationDynamicEnabled {
			m.removeDynamicFlagFile(verificationID)
		}
		if verificationCompatibilityEnabled {
			m.removeCompatibilityFlagFile(verificationID)
		}
		m.recordFailedEnvironmentVerification(challengeID, envInfo, verifiedBy, "", err)
		return nil, fmt.Errorf("환경 검증 컨테이너 생성 실패: %w", err)
	}

	removeVerificationContainer := func() error {
		if verificationDynamicEnabled {
			m.removeDynamicFlagFile(verificationID)
		}
		if verificationCompatibilityEnabled {
			m.removeCompatibilityFlagFile(verificationID)
		}
		err := m.docker.ContainerRemove(context.Background(), response.ID, container.RemoveOptions{Force: true})
		if err != nil && !errdefs.IsNotFound(err) {
			return err
		}
		return nil
	}

	inspect, inspectErr := m.docker.ContainerInspect(ctx, response.ID)
	if inspectErr != nil || inspect.Image == "" {
		verificationErr := &EnvironmentReadinessError{Code: "inspect_failed"}
		m.recordFailedEnvironmentVerification(challengeID, envInfo, verifiedBy, "", verificationErr)
		if cleanupErr := removeVerificationContainer(); cleanupErr != nil {
			log.Printf("[Manager] environment verification identity failure cleanup failed: %v", cleanupErr)
		}
		return nil, verificationErr
	}
	imageID := inspect.Image

	if err := m.docker.ContainerStart(ctx, response.ID, container.StartOptions{}); err != nil {
		m.recordFailedEnvironmentVerification(challengeID, envInfo, verifiedBy, imageID, err)
		if cleanupErr := removeVerificationContainer(); cleanupErr != nil {
			log.Printf("[Manager] 환경 검증 시작 실패 후 컨테이너 정리 실패: %v", cleanupErr)
		}
		return nil, fmt.Errorf("환경 검증 컨테이너 시작 실패: %w", err)
	}

	if err := m.waitForContainerReady(
		ctx,
		response.ID,
		envInfo.ContainerPort,
		verificationID,
		verificationDynamicEnabled,
	); err != nil {
		m.recordFailedEnvironmentVerification(challengeID, envInfo, verifiedBy, imageID, err)
		if cleanupErr := removeVerificationContainer(); cleanupErr != nil {
			log.Printf("[Manager] 환경 검증 실패 후 컨테이너 정리 실패: %v", cleanupErr)
		}
		return nil, err
	}
	if err := removeVerificationContainer(); err != nil {
		m.recordFailedEnvironmentVerification(challengeID, envInfo, verifiedBy, imageID, err)
		return nil, fmt.Errorf("환경 검증 컨테이너 정리 실패: %w", err)
	}

	checkedAt := time.Now().UTC()
	recordCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := m.recordEnvironmentVerification(recordCtx, challengeID, environmentVerificationRecord{
		Revision:       envInfo.EnvironmentRevision,
		Status:         "ready",
		FailureCode:    nil,
		ImageReference: envInfo.DockerImage,
		ImageID:        &imageID,
		VerifiedBy:     verifiedBy,
	}); err != nil {
		// A ready result is not usable until learn-service has durably recorded it.
		log.Printf(
			"[Manager] environment verification ready record was not persisted (challenge=%d, revision=%d)",
			challengeID,
			envInfo.EnvironmentRevision,
		)
		return nil, &EnvironmentReadinessError{Code: "verification_record_failed"}
	}

	return &EnvironmentVerificationResult{
		Status:    "ready",
		CheckedAt: checkedAt,
		Revision:  envInfo.EnvironmentRevision,
	}, nil
}

// StopInstance는 컨테이너를 정지하고 리소스를 해제한다.
func (m *Manager) StopInstance(ctx context.Context, instanceID, userID string) error {
	instanceKey := fmt.Sprintf("instance:%s", instanceID)

	// 인스턴스 존재 및 소유자 확인
	data, err := m.rdb.HGetAll(ctx, instanceKey).Result()
	if err != nil || len(data) == 0 {
		return fmt.Errorf("인스턴스를 찾을 수 없습니다: %s", instanceID)
	}
	if data["user_id"] != userID {
		return fmt.Errorf("해당 인스턴스에 대한 권한이 없습니다")
	}
	// Docker 강제 삭제가 실패해도 유효한 동적 플래그가 남지 않도록 먼저 폐기한다.
	// 같은 helper가 로컬 원본을 지우며, 폐기 endpoint는 멱등이다.
	m.removeCompatibilityFlagFile(instanceID)
	m.cleanupDynamicFlagRuntime(instanceID, data, "instance_stopped")

	// Docker 컨테이너 정지 + 삭제
	containerID := data["container_id"]
	if containerID == "" {
		return fmt.Errorf("인스턴스 컨테이너를 확인할 수 없습니다")
	}
	timeout := 10
	stopOptions := container.StopOptions{Timeout: &timeout}
	_ = m.docker.ContainerStop(ctx, containerID, stopOptions)
	if err := m.docker.ContainerRemove(ctx, containerID, container.RemoveOptions{Force: true}); err != nil && !errdefs.IsNotFound(err) {
		log.Printf("[Manager] 요청한 컨테이너 삭제 미확인 (%s): %v", instanceID, err)
		return fmt.Errorf("컨테이너 삭제를 확인하지 못했습니다")
	}

	// 포트 해제 (파싱 오류 시 로깅)
	port, parseErr := strconv.Atoi(data["port"])
	if parseErr != nil {
		log.Printf("[Manager] 포트 파싱 오류 (instance: %s, port: %q): %v\n", instanceID, data["port"], parseErr)
		return fmt.Errorf("인스턴스 포트가 올바르지 않습니다")
	}
	if port < 1 || port > 65535 {
		return fmt.Errorf("인스턴스 포트가 올바르지 않습니다")
	}
	// Redis 정리
	if err := FinalizeRemovedInstance(ctx, m.rdb, m.portAllocator, instanceID, data); err != nil {
		return err
	}

	log.Printf("[Manager] 인스턴스 정지: %s (컨테이너: %s, 포트 %d)\n", instanceID, containerID, port)
	return nil
}

const instanceExtensionDuration = 30 * time.Minute

func extendedInstanceExpiry(expiresAt string, now time.Time) (time.Time, error) {
	seconds, err := strconv.ParseInt(expiresAt, 10, 64)
	if err != nil || seconds <= now.Unix() {
		return time.Time{}, fmt.Errorf("실습 환경 사용 시간이 만료되었거나 만료 정보가 올바르지 않습니다")
	}
	return time.Unix(seconds, 0).UTC().Add(instanceExtensionDuration), nil
}

// ExtendInstance는 기존 만료 시각에 30분을 추가한다 (최대 maxExtendCount회).
// 대회 인스턴스는 연장 시에도 참가·배정·진행 기간을 다시 확인한다.
func (m *Manager) ExtendInstance(ctx context.Context, instanceID, userID string) (*ExtensionResult, error) {
	if err := m.RequireInstanceAccess(ctx, instanceID, userID); err != nil {
		return nil, err
	}

	instanceKey := fmt.Sprintf("instance:%s", instanceID)

	data, err := m.rdb.HGetAll(ctx, instanceKey).Result()
	if err != nil || len(data) == 0 {
		return nil, fmt.Errorf("인스턴스를 찾을 수 없습니다: %s", instanceID)
	}
	if data["user_id"] != userID {
		return nil, fmt.Errorf("해당 인스턴스에 대한 권한이 없습니다")
	}

	// 연장 횟수 제한
	extendCount, _ := strconv.Atoi(data["extend_count"])
	if extendCount >= m.maxExtendCount {
		return nil, fmt.Errorf("최대 연장 횟수(%d회)를 초과했습니다", m.maxExtendCount)
	}

	// 초기 실행 TTL과 연장 간격은 별개다. 남아 있는 시간을 보존한다.
	newExpiry, err := extendedInstanceExpiry(data["expires_at"], time.Now().UTC())
	if err != nil {
		return nil, err
	}
	if dynamicFlagEnabled(data) {
		var refreshErr error
		newExpiry, refreshErr = m.refreshDynamicFlagForExtension(ctx, instanceID, data, newExpiry)
		if refreshErr != nil {
			return nil, refreshErr
		}
	}
	if err := m.rdb.HSet(ctx, instanceKey, "expires_at", newExpiry.Unix(), "extend_count", extendCount+1).Err(); err != nil {
		if dynamicFlagEnabled(data) {
			m.cleanupUnavailableInstance(ctx, instanceID, data, "metadata_persist_failed")
		}
		return nil, fmt.Errorf("인스턴스 연장 저장 실패: %w", err)
	}
	if err := m.rdb.ExpireAt(ctx, instanceKey, newExpiry.Add(instanceMetadataRetention)).Err(); err != nil {
		if dynamicFlagEnabled(data) {
			m.cleanupUnavailableInstance(ctx, instanceID, data, "metadata_persist_failed")
		}
		return nil, fmt.Errorf("인스턴스 연장 만료 시각 저장 실패: %w", err)
	}

	log.Printf("[Manager] 인스턴스 연장: %s → %v (횟수: %d/%d)\n", instanceID, newExpiry, extendCount+1, m.maxExtendCount)
	return &ExtensionResult{
		ExpiresAt:      newExpiry,
		ExtendCount:    extendCount + 1,
		MaxExtendCount: m.maxExtendCount,
	}, nil
}

// GetDockerClient는 매니저의 Docker 클라이언트를 반환한다.
func (m *Manager) GetDockerClient() *dockerclient.Client {
	return m.docker
}

// GetPortAllocator는 매니저의 포트 할당기를 반환한다.
func (m *Manager) GetPortAllocator() *PortAllocator {
	return m.portAllocator
}
