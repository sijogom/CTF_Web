package container

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"path"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"github.com/docker/docker/api/types/container"
	"github.com/docker/docker/api/types/mount"
	"github.com/docker/docker/errdefs"
)

const (
	dynamicFlagIssuePath  = "/internal/dynamic-flags/issue"
	dynamicFlagRevokePath = "/internal/dynamic-flags/revoke"
)

// 로컬 생명주기 상세 사유를 Learn의 안정적인 감사 allowlist로 매핑한다. 내부 구현
// 용어가 별도 정책 표면이 되는 것을 막으면서 서버 측 분류를 안전하게 유지한다.
var dynamicFlagRevocationReasons = map[string]string{
	"container_start_failed":         "instance_failed",
	"readiness_failed":               "instance_failed",
	"instance_stopped":               "instance_stopped",
	"instance_expired":               "instance_expired",
	"environment_drift":              "environment_stale",
	"reconcile_cleanup":              "cleanup_failed",
	"metadata_persist_failed":        "cleanup_failed",
	"issuance_materialization_failed": "cleanup_failed",
}

type dynamicFlagRevokeRequest struct {
	InstanceID      string `json:"instance_id"`
	InstanceLocator string `json:"instance_locator,omitempty"`
	Reason          string `json:"reason"`
}

func dynamicFlagPolicyRuntimePath(policy dynamicFlagPolicyResponse) (string, error) {
	if !policy.Enabled {
		return "", nil
	}
	runtimePath := strings.TrimSpace(policy.RuntimePath)
	if runtimePath == "" {
		runtimePath = dynamicFlagRuntimePath
	}
	if runtimePath != dynamicFlagRuntimePath || !path.IsAbs(runtimePath) || path.Clean(runtimePath) != runtimePath {
		return "", &EnvironmentReadinessError{Code: "invalid_environment"}
	}
	if policy.TTLSeconds <= 0 || policy.TTLSeconds > int(dynamicFlagMaxTTL/time.Second) || policy.PolicyRevision <= 0 {
		return "", &EnvironmentReadinessError{Code: "invalid_environment"}
	}
	return runtimePath, nil
}

// dynamicFlagRequestedExpiry는 일반 컨테이너 TTL보다 짧을 수 있는 동적 플래그
// 정책 TTL을 발급 요청에 강제한다. Learn은 정책 TTL을 초과하는 one-shot issuance를
// 거부하므로, 호출자는 이 값을 인스턴스의 실제 만료 시각으로 사용해야 한다.
func dynamicFlagRequestedExpiry(now, requested time.Time, policy dynamicFlagPolicyResponse) (time.Time, error) {
	if _, err := dynamicFlagPolicyRuntimePath(policy); err != nil {
		return time.Time{}, err
	}
	if requested.IsZero() {
		return time.Time{}, &EnvironmentReadinessError{Code: "invalid_environment"}
	}
	policyExpiry := now.UTC().Add(time.Duration(policy.TTLSeconds) * time.Second)
	if requested.After(policyExpiry) {
		return policyExpiry, nil
	}
	return requested.UTC(), nil
}

func sameDynamicFlagPolicy(left, right dynamicFlagPolicyResponse) bool {
	return left.Enabled == right.Enabled &&
		left.RuntimePath == right.RuntimePath &&
		left.TTLSeconds == right.TTLSeconds &&
		left.PolicyRevision == right.PolicyRevision
}

func validateCurrentDynamicFlagPolicy(data map[string]string, policy dynamicFlagPolicyResponse) error {
	instanceUsesDynamicFlag := dynamicFlagEnabled(data)
	if !instanceUsesDynamicFlag {
		if policy.Enabled {
			return &EnvironmentReadinessError{Code: "environment_verification_required"}
		}
		return nil
	}

	runtimePath, err := dynamicFlagPolicyRuntimePath(policy)
	if err != nil {
		return err
	}
	storedRevision, err := strconv.Atoi(data["dynamic_flag_policy_revision"])
	if err != nil || storedRevision <= 0 || storedRevision != policy.PolicyRevision || data["dynamic_flag_runtime_path"] != runtimePath {
		return &EnvironmentReadinessError{Code: "environment_verification_required"}
	}
	return nil
}

func (m *Manager) dynamicFlagHostPath(instanceID string) (string, error) {
	if instanceID == "" || strings.ContainsAny(instanceID, "/\\") || filepath.Base(instanceID) != instanceID {
		return "", &EnvironmentReadinessError{Code: "invalid_environment"}
	}
	dir := strings.TrimSpace(m.dynamicFlagDir)
	if dir == "" {
		dir = defaultDynamicFlagDir
	}
	base := filepath.Clean(dir)
	if !filepath.IsAbs(base) {
		return "", &EnvironmentReadinessError{Code: "invalid_environment"}
	}
	filePath := filepath.Join(base, instanceID+".flag")
	if filepath.Dir(filePath) != base {
		return "", &EnvironmentReadinessError{Code: "invalid_environment"}
	}
	return filePath, nil
}

// compatibilityFlagHostPath는 static/regex 이미지의 레거시 HEALTHCHECK가
// /flag.txt의 존재·내용만 확인할 때 사용하는 비밀값이 아닌 호환성 파일이다.
// 사용자 플래그나 dynamic issuance와 같은 이름공간을 사용하지 않으며, 파일명
// prefix도 sweeper가 dynamic issuance로 오인하지 않도록 분리한다.
func (m *Manager) compatibilityFlagHostPath(instanceID string) (string, error) {
	if instanceID == "" || strings.ContainsAny(instanceID, "/\\") || filepath.Base(instanceID) != instanceID {
		return "", &EnvironmentReadinessError{Code: "invalid_environment"}
	}
	return m.dynamicFlagHostPath("compat_" + instanceID)
}

func (m *Manager) writeCompatibilityFlagFile(instanceID string) (string, error) {
	path, err := m.compatibilityFlagHostPath(instanceID)
	if err != nil {
		return "", err
	}
	// 일부 레거시 HEALTHCHECK는 trim 후 "ok"를 확인한다. 이 값은 참가자
	// 플래그가 아니며 static/regex 제출 검증에도 사용하지 않는다.
	return m.writeRuntimeFlagFile(path, "ok\n")
}

func (m *Manager) removeCompatibilityFlagFile(instanceID string) {
	path, err := m.compatibilityFlagHostPath(instanceID)
	if err != nil {
		return
	}
	if err := os.Remove(path); err != nil && !errors.Is(err, os.ErrNotExist) {
		logDynamicFlagCleanupFailure(instanceID)
	}
}

func validateDynamicFlagValue(flag string) error {
	if len(flag) == 0 || len(flag) > dynamicFlagMaxLength || strings.ContainsRune(flag, '\x00') || strings.ContainsAny(flag, "\r\n") {
		return &EnvironmentReadinessError{Code: "environment_unavailable"}
	}
	return nil
}

// writeDynamicFlagFile은 비밀값을 호스트 로컬 0444 파일에만 실체화한다. 호스트 부모
// 디렉터리는 0700으로 제한하고 컨테이너에는 read-only bind mount로만 노출한다. 따라서
// 문제 이미지가 임의의 USER를 선언하더라도 FLAG_PATH를 읽을 수 있지만, 호스트의 다른
// 사용자나 컨테이너 내부에서는 파일을 수정할 수 없다. 같은 파일
// 시스템 안의 rename은 원자적이지만 실행 중인 컨테이너는 기존 inode를 계속 보므로,
// 호출자는 ContainerCreate 전에 만들고 ContainerRemove 뒤에만 제거해야 한다.
func (m *Manager) writeDynamicFlagFile(instanceID, flag string) (string, error) {
	if err := validateDynamicFlagValue(flag); err != nil {
		return "", err
	}
	filePath, err := m.dynamicFlagHostPath(instanceID)
	if err != nil {
		return "", err
	}
	return m.writeRuntimeFlagFile(filePath, flag)
}

// writeRuntimeFlagFile은 dynamic flag와 static/regex 호환성 파일이 동일한
// 0700 디렉터리·0444 파일·원자적 rename 계약을 공유하도록 하는 내부 writer다.
func (m *Manager) writeRuntimeFlagFile(filePath, value string) (string, error) {
	if filePath == "" || !filepath.IsAbs(filePath) {
		return "", &EnvironmentReadinessError{Code: "invalid_environment"}
	}
	dir := filepath.Dir(filePath)
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return "", fmt.Errorf("dynamic flag directory creation failed: %w", err)
	}
	if err := os.Chmod(dir, 0o700); err != nil {
		return "", fmt.Errorf("dynamic flag directory permission hardening failed: %w", err)
	}

	// CreateTemp는 O_CREATE|O_EXCL을 사용한다. 발급과 원자적 rename 사이 프로세스가
	// 중단되어도 sweeper가 발급을 폐기할 수 있게 임시 파일명에 instance ID를 넣는다.
	tempPrefix := "." + strings.TrimSuffix(filepath.Base(filePath), filepath.Ext(filePath)) + ".tmp-*"
	tmp, err := os.CreateTemp(dir, tempPrefix)
	if err != nil {
		return "", fmt.Errorf("dynamic flag temporary file creation failed: %w", err)
	}
	tmpPath := tmp.Name()
	cleaned := false
	defer func() {
		if !cleaned {
			_ = tmp.Close()
			_ = os.Remove(tmpPath)
		}
	}()
	if err := tmp.Chmod(0o600); err != nil {
		return "", fmt.Errorf("dynamic flag file permission hardening failed: %w", err)
	}
	if _, err := tmp.WriteString(value); err != nil {
		return "", fmt.Errorf("dynamic flag file write failed: %w", err)
	}
	// 작성 중에는 소유자만 읽고 쓸 수 있게 유지한 뒤, 원자적 rename 직전에 일반
	// Dockerfile USER도 읽을 수 있도록 읽기 전용 모드로 낮춘다.
	if err := tmp.Chmod(0o444); err != nil {
		return "", fmt.Errorf("dynamic flag file read-only permission failed: %w", err)
	}
	if err := tmp.Sync(); err != nil {
		return "", fmt.Errorf("dynamic flag file sync failed: %w", err)
	}
	if err := tmp.Close(); err != nil {
		return "", fmt.Errorf("dynamic flag file close failed: %w", err)
	}
	if err := os.Rename(tmpPath, filePath); err != nil {
		return "", fmt.Errorf("dynamic flag atomic replace failed: %w", err)
	}
	cleaned = true
	return filePath, nil
}

func (m *Manager) removeDynamicFlagFile(instanceID string) {
	filePath, err := m.dynamicFlagHostPath(instanceID)
	if err != nil {
		return
	}
	if err := os.Remove(filePath); err != nil && !errors.Is(err, os.ErrNotExist) {
		// The path is never included because it is an infrastructure detail.
		logDynamicFlagCleanupFailure(instanceID)
	}
}

// cleanupUnrecordedDynamicFlagRuntime handles the small crash window after a
// dynamic file has been materialized but before the instance metadata reaches
// Redis. It only acts when the protected runtime directory contains the
// expected regular file, so static/regex cleanup does not make a revoke call.
func (m *Manager) cleanupUnrecordedDynamicFlagRuntime(instanceID, reason string) {
	filePath, err := m.dynamicFlagHostPath(instanceID)
	if err != nil {
		return
	}
	info, err := os.Lstat(filePath)
	if err != nil || !info.Mode().IsRegular() {
		return
	}
	if strings.HasPrefix(instanceID, "inst_") {
		cleanupCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		m.revokeDynamicFlag(cleanupCtx, instanceID, "", reason)
	}
	m.removeDynamicFlagFile(instanceID)
}

func dynamicFlagInstanceIDFromFileName(name string) (string, bool) {
	var instanceID string
	switch {
	case strings.HasSuffix(name, ".flag"):
		instanceID = strings.TrimSuffix(name, ".flag")
	case strings.HasPrefix(name, ".") && strings.Contains(name, ".tmp-"):
		instanceID = strings.TrimPrefix(name, ".")
		instanceID = strings.SplitN(instanceID, ".tmp-", 2)[0]
	default:
		return "", false
	}
	prefixLength := 0
	switch {
	case strings.HasPrefix(instanceID, "inst_") && len(instanceID) == len("inst_")+32:
		prefixLength = len("inst_")
	case strings.HasPrefix(instanceID, "verify_") && len(instanceID) == len("verify_")+32:
		prefixLength = len("verify_")
	default:
		return "", false
	}
	for _, character := range instanceID[prefixLength:] {
		if (character < '0' || character > '9') && (character < 'a' || character > 'f') {
			return "", false
		}
	}
	return instanceID, true
}

func compatibilityFlagInstanceIDFromFileName(name string) (string, bool) {
	const prefix = "compat_"
	if !strings.HasPrefix(name, prefix) {
		return "", false
	}
	return dynamicFlagInstanceIDFromFileName(strings.TrimPrefix(name, prefix))
}

// SweepDynamicFlagRuntime은 동적 플래그 내부를 알지 못하는 범용 lifecycle watcher를
// 보완한다. TTL 정리, reconciler 삭제, 프로세스 중단, Redis 저장 전 생성 실패 뒤의
// 파일을 회수한다. 짧은 유예 시간은 진행 중인 ContainerCreate 트랜잭션을 보존한다.
func (m *Manager) SweepDynamicFlagRuntime(ctx context.Context) {
	if m.rdb == nil {
		return
	}
	dir := strings.TrimSpace(m.dynamicFlagDir)
	if dir == "" {
		dir = defaultDynamicFlagDir
	}
	base := filepath.Clean(dir)
	if !filepath.IsAbs(base) {
		log.Printf("[Manager] dynamic flag sweep rejected a non-absolute runtime directory")
		return
	}
	entries, err := os.ReadDir(base)
	if errors.Is(err, os.ErrNotExist) {
		return
	}
	if err != nil {
		log.Printf("[Manager] dynamic flag sweep could not read its runtime directory")
		return
	}

	now := time.Now()
	for _, entry := range entries {
		instanceID, dynamicRuntime := dynamicFlagInstanceIDFromFileName(entry.Name())
		if !dynamicRuntime {
			instanceID, dynamicRuntime = compatibilityFlagInstanceIDFromFileName(entry.Name())
		}
		ok := instanceID != ""
		if !ok {
			continue
		}
		info, infoErr := entry.Info()
		if infoErr != nil || !info.Mode().IsRegular() {
			continue
		}
		if now.Sub(info.ModTime()) < dynamicFlagOrphanGrace {
			continue
		}
		removeEntry := func() {
			if err := os.Remove(filepath.Join(base, entry.Name())); err != nil && !errors.Is(err, os.ErrNotExist) {
				logDynamicFlagCleanupFailure(instanceID)
			}
		}

		data, dataErr := m.rdb.HGetAll(ctx, fmt.Sprintf("instance:%s", instanceID)).Result()
		if dataErr != nil {
			continue
		}
		if len(data) == 0 {
			if dynamicRuntime && strings.HasPrefix(instanceID, "inst_") {
				m.revokeDynamicFlag(ctx, instanceID, "", "reconcile_cleanup")
			}
			removeEntry()
			continue
		}
		if dynamicRuntime && !dynamicFlagEnabled(data) {
			removeEntry()
			continue
		}
		if isInstanceExpired(data) {
			if dynamicRuntime {
				m.cleanupDynamicFlagRuntime(instanceID, data, "instance_expired")
			} else {
				m.removeCompatibilityFlagFile(instanceID)
			}
			removeEntry()
			continue
		}
		if m.docker == nil || data["container_id"] == "" {
			continue
		}
		inspect, inspectErr := m.docker.ContainerInspect(ctx, data["container_id"])
		if errdefs.IsNotFound(inspectErr) || (inspectErr == nil && (inspect.State == nil || !inspect.State.Running)) {
			if dynamicRuntime {
				m.cleanupDynamicFlagRuntime(instanceID, data, "reconcile_cleanup")
			} else {
				m.removeCompatibilityFlagFile(instanceID)
			}
			removeEntry()
		}
	}
}

// 별도 helper를 두어 생명주기 로그에 비밀값이나 호스트 파일 경로가 실수로 추가되는
// 일을 막는다.
func logDynamicFlagCleanupFailure(instanceID string) {
	// instance ID는 플랫폼이 생성·검증하는 opaque ID다. OS error에는 호스트 경로가
	// 들어갈 수 있으므로 기록하지 않는다.
	log.Printf("[Manager] dynamic flag cleanup failed for instance=%s", instanceID)
}

func (m *Manager) issueDynamicFlag(
	ctx context.Context,
	request dynamicFlagIssueRequest,
	policy dynamicFlagPolicyResponse,
) (dynamicFlagIssueResponse, error) {
	if _, err := dynamicFlagPolicyRuntimePath(policy); err != nil {
		return dynamicFlagIssueResponse{}, err
	}
	if m.internalAPIKey == "" || request.ChallengeID <= 0 || request.InstanceID == "" || request.UserID == "" || request.EnvironmentRevision <= 0 || request.VerifiedImageID == "" || request.ExpiresAt.IsZero() {
		return dynamicFlagIssueResponse{}, &EnvironmentReadinessError{Code: "environment_unavailable"}
	}
	payload, err := json.Marshal(request)
	if err != nil {
		return dynamicFlagIssueResponse{}, &EnvironmentReadinessError{Code: "environment_unavailable"}
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, m.learnURL+dynamicFlagIssuePath, bytes.NewReader(payload))
	if err != nil {
		return dynamicFlagIssueResponse{}, &EnvironmentReadinessError{Code: "environment_unavailable"}
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Internal-Key", m.internalAPIKey)
	resp, err := m.httpClient.Do(req)
	if err != nil {
		return dynamicFlagIssueResponse{}, &EnvironmentReadinessError{Code: "environment_unavailable"}
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK && resp.StatusCode != http.StatusCreated {
		return dynamicFlagIssueResponse{}, &EnvironmentReadinessError{Code: "environment_unavailable"}
	}
	var issued dynamicFlagIssueResponse
	if err := json.NewDecoder(io.LimitReader(resp.Body, 64*1024)).Decode(&issued); err != nil {
		return dynamicFlagIssueResponse{}, &EnvironmentReadinessError{Code: "environment_unavailable"}
	}
	runtimePath, err := dynamicFlagPolicyRuntimePath(policy)
	if err != nil {
		return dynamicFlagIssueResponse{}, err
	}
	if issued.RuntimePath != runtimePath || issued.InstanceLocator == "" || issued.ExpiresAt.IsZero() || issued.ExpiresAt.After(request.ExpiresAt) || issued.ExpiresAt.Before(time.Now().UTC()) {
		return dynamicFlagIssueResponse{}, &dynamicFlagIssueError{err: &EnvironmentReadinessError{Code: "environment_unavailable"}, issued: true}
	}
	if err := validateDynamicFlagValue(issued.Flag); err != nil {
		return dynamicFlagIssueResponse{}, &dynamicFlagIssueError{err: err, issued: true}
	}
	return issued, nil
}

// revokeDynamicFlag는 정리 과정에서 멱등·best-effort로 동작한다. Learn을 사용할 수
// 없어도 호출자는 로컬 파일과 컨테이너를 제거하며, 이후 sweeper가 원격 lease를 재시도한다.
func (m *Manager) revokeDynamicFlag(ctx context.Context, instanceID, locator, reason string) {
	learnReason, ok := dynamicFlagRevocationReasons[reason]
	if !ok || m.internalAPIKey == "" || instanceID == "" {
		return
	}
	payload, err := json.Marshal(dynamicFlagRevokeRequest{InstanceID: instanceID, InstanceLocator: locator, Reason: learnReason})
	if err != nil {
		return
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, m.learnURL+dynamicFlagRevokePath, bytes.NewReader(payload))
	if err != nil {
		return
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Internal-Key", m.internalAPIKey)
	resp, err := m.httpClient.Do(req)
	if err != nil {
		return
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK && resp.StatusCode != http.StatusNoContent {
		return
	}
}

func dynamicFlagMount(source, runtimePath string) mount.Mount {
	return mount.Mount{
		Type:     mount.TypeBind,
		Source:   source,
		Target:   runtimePath,
		ReadOnly: true,
	}
}

func applyDynamicFlagRuntime(hostConfig *container.HostConfig, source, runtimePath string) {
	hostConfig.Mounts = append(hostConfig.Mounts, dynamicFlagMount(source, runtimePath))
}

// refreshDynamicFlagForExtension은 서버 lease를 회전하고 호스트 원본을 원자적으로
// 교체한 뒤 컨테이너를 재시작해 Docker가 파일 경로를 다시 bind하도록 한다. 파일 bind
// mount는 실행 중 기존 inode를 보존하므로 rename만 하면 Learn이 폐기한 이전 플래그가
// 컨테이너에 남는다.
func (m *Manager) refreshDynamicFlagForExtension(
	ctx context.Context,
	instanceID string,
	data map[string]string,
	requestedExpiry time.Time,
) (time.Time, error) {
	if !dynamicFlagEnabled(data) {
		return requestedExpiry, nil
	}
	challengeID, err := strconv.Atoi(data["challenge_id"])
	if err != nil || challengeID <= 0 {
		m.cleanupUnavailableInstance(ctx, instanceID, data, "environment_drift")
		return time.Time{}, &EnvironmentReadinessError{Code: "environment_verification_required"}
	}
	revision, err := strconv.Atoi(data["environment_revision"])
	if err != nil || revision <= 0 || data["environment_verified_image_id"] == "" {
		m.cleanupUnavailableInstance(ctx, instanceID, data, "environment_drift")
		return time.Time{}, &EnvironmentReadinessError{Code: "environment_verification_required"}
	}
	envInfo, err := m.fetchInstanceChallengeEnv(ctx, challengeID)
	if err != nil {
		m.cleanupUnavailableInstance(ctx, instanceID, data, "environment_drift")
		return time.Time{}, err
	}
	if _, err := validateCurrentInstanceEnvironment(data, envInfo); err != nil {
		m.cleanupUnavailableInstance(ctx, instanceID, data, "environment_drift")
		return time.Time{}, err
	}
	requestedExpiry, err = dynamicFlagRequestedExpiry(time.Now().UTC(), requestedExpiry, envInfo.DynamicFlag)
	if err != nil {
		m.cleanupUnavailableInstance(ctx, instanceID, data, "environment_drift")
		return time.Time{}, err
	}

	var competitionID *string
	if value := data["competition_id"]; value != "" {
		competitionID = &value
	}
	issued, issueErr := m.issueDynamicFlag(ctx, dynamicFlagIssueRequest{
		ChallengeID:         challengeID,
		InstanceID:          instanceID,
		UserID:              data["user_id"],
		CompetitionID:       competitionID,
		EnvironmentRevision: revision,
		VerifiedImageID:     data["environment_verified_image_id"],
		ExpiresAt:           requestedExpiry,
	}, envInfo.DynamicFlag)
	if issueErr != nil {
		var issuedAfterError *dynamicFlagIssueError
		if errors.As(issueErr, &issuedAfterError) && issuedAfterError.issued {
			m.cleanupUnavailableInstance(ctx, instanceID, data, "issuance_materialization_failed")
		}
		return time.Time{}, issueErr
	}
	if _, err := m.writeDynamicFlagFile(instanceID, issued.Flag); err != nil {
		issued.Flag = ""
		m.revokeDynamicFlag(context.Background(), instanceID, issued.InstanceLocator, "issuance_materialization_failed")
		m.cleanupUnavailableInstance(ctx, instanceID, data, "issuance_materialization_failed")
		return time.Time{}, err
	}
	issued.Flag = ""

	containerPort, err := strconv.Atoi(data["container_port"])
	if err != nil || containerPort <= 0 || data["container_id"] == "" {
		m.cleanupUnavailableInstance(ctx, instanceID, data, "environment_drift")
		return time.Time{}, &EnvironmentReadinessError{Code: "invalid_environment"}
	}
	if err := m.docker.ContainerRestart(ctx, data["container_id"], container.StopOptions{}); err != nil {
		m.cleanupUnavailableInstance(ctx, instanceID, data, "container_start_failed")
		return time.Time{}, &EnvironmentReadinessError{Code: "environment_unavailable"}
	}
	if err := m.waitForContainerReady(
		ctx,
		data["container_id"],
		containerPort,
		instanceID,
		true,
	); err != nil {
		m.cleanupUnavailableInstance(ctx, instanceID, data, "readiness_failed")
		return time.Time{}, err
	}
	return issued.ExpiresAt.UTC(), nil
}
