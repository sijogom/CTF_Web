package container

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"regexp"
	"sort"
	"strconv"
	"strings"

	"github.com/docker/docker/api/types/container"
	"github.com/docker/docker/api/types/filters"
	"github.com/docker/docker/errdefs"
	"github.com/docker/go-connections/nat"
	"github.com/redis/go-redis/v9"
)

var (
	ErrCleanupChallengeExists = errors.New("cleanup challenge still exists")
	ErrCleanupTargetRejected  = errors.New("cleanup target rejected")
	cleanupJobIDPattern       = regexp.MustCompile(`^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`)
	cleanupContainerIDPattern = regexp.MustCompile(`^[0-9a-f]{64}$`)
)

type ChallengeResourceCleanupResult struct {
	ChallengeID    int    `json:"challenge_id"`
	JobID          string `json:"job_id"`
	Status         string `json:"status"`
	ImageReference string `json:"image_reference"`
}

type challengeCleanupProof struct {
	ChallengeID    int    `json:"challenge_id"`
	JobID          string `json:"job_id"`
	Deleted        bool   `json:"deleted"`
	ImageReference string `json:"image_reference"`
}

// The journal survives Docker removal and a later Redis failure. It contains
// only the exact identities observed before removal, not a reusable wildcard.
type challengeCleanupInstance struct {
	ChallengeID  int               `json:"challenge_id"`
	InstanceID   string            `json:"instance_id"`
	ContainerID  string            `json:"container_id"`
	UserID       string            `json:"user_id,omitempty"`
	Ports        []int             `json:"ports,omitempty"`
	Data         map[string]string `json:"data,omitempty"`
	Orphan       bool              `json:"orphan,omitempty"`
	Verification bool              `json:"verification,omitempty"`
}

func validCleanupInstanceID(instanceID string) bool {
	parsed, ok := dynamicFlagInstanceIDFromFileName(instanceID + ".flag")
	return ok && parsed == instanceID && ValidPortOwner(instanceID)
}

func validCleanupUser(userID string) bool {
	return userID != "" && len(userID) <= 128 && !strings.ContainsAny(userID, "\r\n\x00")
}

func (m *Manager) fetchChallengeCleanupProof(ctx context.Context, challengeID int, jobID string) (*challengeCleanupProof, error) {
	if m.learnURL == "" || m.internalAPIKey == "" || m.httpClient == nil {
		return nil, fmt.Errorf("cleanup proof boundary is unavailable")
	}
	endpoint := fmt.Sprintf("%s/internal/challenges/%d/cleanup-target?job_id=%s", strings.TrimRight(m.learnURL, "/"), challengeID, url.QueryEscape(jobID))
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, endpoint, nil)
	if err != nil {
		return nil, fmt.Errorf("cleanup proof request failed")
	}
	req.Header.Set("X-Internal-Key", m.internalAPIKey)
	response, err := m.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("cleanup proof lookup failed")
	}
	defer response.Body.Close()
	if response.StatusCode == http.StatusConflict {
		var rejected struct {
			Code   string `json:"code"`
			Detail struct {
				Code string `json:"code"`
			} `json:"detail"`
		}
		if json.NewDecoder(io.LimitReader(response.Body, 4096)).Decode(&rejected) == nil &&
			(rejected.Code == "cleanup_challenge_exists" || rejected.Detail.Code == "cleanup_challenge_exists") {
			return nil, ErrCleanupChallengeExists
		}
		return nil, ErrCleanupTargetRejected
	}
	if response.StatusCode == http.StatusNotFound || response.StatusCode == http.StatusBadRequest || response.StatusCode == http.StatusUnprocessableEntity {
		return nil, ErrCleanupTargetRejected
	}
	if response.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("cleanup proof boundary is unavailable")
	}
	var proof challengeCleanupProof
	decoder := json.NewDecoder(io.LimitReader(response.Body, 4096))
	if err := decoder.Decode(&proof); err != nil {
		return nil, ErrCleanupTargetRejected
	}
	var trailing interface{}
	if decoder.Decode(&trailing) != io.EOF || proof.JobID != jobID || proof.ChallengeID != challengeID || !proof.Deleted {
		return nil, ErrCleanupTargetRejected
	}
	expected := fmt.Sprintf("ctf-challenge-%d:latest", challengeID)
	if proof.ImageReference != "" && proof.ImageReference != expected {
		return nil, ErrCleanupTargetRejected
	}
	return &proof, nil
}

func cleanupContainerLabels(challengeID int, instanceID string, labels map[string]string, name string) (string, bool, error) {
	if !validCleanupInstanceID(instanceID) || name != "/ctf-"+instanceID ||
		labels["ctf-challenge"] != "true" || labels["challenge-id"] != strconv.Itoa(challengeID) ||
		labels["instance-id"] != instanceID || labels["com.docker.compose.project"] != "" || labels["com.docker.compose.service"] != "" {
		return "", false, ErrCleanupTargetRejected
	}
	verification := strings.HasPrefix(instanceID, "verify_")
	if verification {
		if labels["verification"] != "true" || labels["user-id"] != "" {
			return "", false, ErrCleanupTargetRejected
		}
		return "", true, nil
	}
	userID := labels["user-id"]
	if labels["verification"] != "" || !validCleanupUser(userID) {
		return "", false, ErrCleanupTargetRejected
	}
	return userID, false, nil
}

func cleanupObservedPorts(bindings nat.PortMap) ([]int, error) {
	seen := map[int]bool{}
	for _, binding := range bindings {
		for _, host := range binding {
			if host.HostPort == "" || host.HostPort == "0" {
				continue
			}
			port, err := strconv.Atoi(host.HostPort)
			if err != nil || port < 1 || port > 65535 {
				return nil, ErrCleanupTargetRejected
			}
			seen[port] = true
		}
	}
	return sortedCleanupPorts(seen), nil
}

func sortedCleanupPorts(seen map[int]bool) []int {
	ports := make([]int, 0, len(seen))
	for port := range seen {
		ports = append(ports, port)
	}
	sort.Ints(ports)
	return ports
}

func validateCleanupMetadata(challengeID int, instanceID string, data map[string]string) error {
	if !validCleanupInstanceID(instanceID) || !strings.HasPrefix(instanceID, "inst_") ||
		data["challenge_id"] != strconv.Itoa(challengeID) || !cleanupContainerIDPattern.MatchString(data["container_id"]) || !validCleanupUser(data["user_id"]) {
		return ErrCleanupTargetRejected
	}
	port, err := strconv.Atoi(data["port"])
	if err != nil || port < 1 || port > 65535 {
		return ErrCleanupTargetRejected
	}
	return nil
}

func validateCleanupJournal(challengeID int, field string, entry challengeCleanupInstance) error {
	if entry.ChallengeID != challengeID || entry.InstanceID != field || !validCleanupInstanceID(field) || !cleanupContainerIDPattern.MatchString(entry.ContainerID) {
		return ErrCleanupTargetRejected
	}
	if entry.Verification {
		if !strings.HasPrefix(field, "verify_") || entry.UserID != "" || len(entry.Ports) != 0 || len(entry.Data) != 0 {
			return ErrCleanupTargetRejected
		}
		return nil
	}
	if err := validateCleanupMetadata(challengeID, field, entry.Data); err != nil {
		return err
	}
	if entry.Data["container_id"] != entry.ContainerID || entry.Data["user_id"] != entry.UserID {
		return ErrCleanupTargetRejected
	}
	for _, port := range entry.Ports {
		if port < 1 || port > 65535 {
			return ErrCleanupTargetRejected
		}
	}
	return nil
}

func mergeCleanupInstance(target map[string]challengeCleanupInstance, entry challengeCleanupInstance) error {
	if previous, exists := target[entry.InstanceID]; exists {
		if previous.ChallengeID != entry.ChallengeID || previous.ContainerID != entry.ContainerID || previous.UserID != entry.UserID || previous.Verification != entry.Verification {
			return ErrCleanupTargetRejected
		}
		if !entry.Verification && previous.Data["port"] != entry.Data["port"] {
			return ErrCleanupTargetRejected
		}
		seen := map[int]bool{}
		for _, port := range append(previous.Ports, entry.Ports...) {
			seen[port] = true
		}
		entry.Ports = sortedCleanupPorts(seen)
		// Preserve the original runtime metadata when a journal is the only
		// surviving recovery record after an uncertain Docker response.
		if previous.Data["dynamic_flag_enabled"] != "" && entry.Data["dynamic_flag_enabled"] == "" {
			entry.Data["dynamic_flag_enabled"] = previous.Data["dynamic_flag_enabled"]
		}
		entry.Orphan = previous.Orphan || entry.Orphan
	}
	target[entry.InstanceID] = entry
	return nil
}

func (m *Manager) collectCleanupInstances(ctx context.Context, challengeID int, journalKey string) (map[string]challengeCleanupInstance, error) {
	entries := make(map[string]challengeCleanupInstance)
	journal := map[string]string{}
	if journalKey != "" {
		var err error
		journal, err = m.rdb.HGetAll(ctx, journalKey).Result()
		if err != nil {
			return nil, fmt.Errorf("cleanup journal lookup failed")
		}
	}
	for field, value := range journal {
		var entry challengeCleanupInstance
		if json.Unmarshal([]byte(value), &entry) != nil || validateCleanupJournal(challengeID, field, entry) != nil {
			return nil, ErrCleanupTargetRejected
		}
		entries[field] = entry
	}

	filter := filters.NewArgs()
	filter.Add("label", "ctf-challenge=true")
	filter.Add("label", "challenge-id="+strconv.Itoa(challengeID))
	containers, err := m.docker.ContainerList(ctx, container.ListOptions{All: true, Filters: filter})
	if err != nil {
		return nil, fmt.Errorf("cleanup container lookup failed")
	}
	for _, observed := range containers {
		// Verify the filter result too; a name pattern or challenge-id alone
		// must never grant deletion of a shared host resource.
		if observed.Labels["ctf-challenge"] != "true" || observed.Labels["challenge-id"] != strconv.Itoa(challengeID) {
			continue
		}
		instanceID := observed.Labels["instance-id"]
		if len(observed.Names) != 1 {
			return nil, ErrCleanupTargetRejected
		}
		userID, verification, err := cleanupContainerLabels(challengeID, instanceID, observed.Labels, observed.Names[0])
		if err != nil || !cleanupContainerIDPattern.MatchString(observed.ID) {
			return nil, ErrCleanupTargetRejected
		}
		entry := challengeCleanupInstance{ChallengeID: challengeID, InstanceID: instanceID, ContainerID: observed.ID, UserID: userID, Verification: verification, Orphan: true}
		seenPorts := map[int]bool{}
		for _, port := range observed.Ports {
			if port.PublicPort > 0 {
				seenPorts[int(port.PublicPort)] = true
			}
		}
		inspection, inspectErr := m.docker.ContainerInspect(ctx, observed.ID)
		if inspectErr != nil && !errdefs.IsNotFound(inspectErr) {
			return nil, fmt.Errorf("cleanup container identity lookup failed")
		}
		if inspectErr == nil {
			if inspection.ContainerJSONBase == nil || inspection.ID != observed.ID || inspection.Config == nil {
				return nil, ErrCleanupTargetRejected
			}
			currentUser, currentVerification, err := cleanupContainerLabels(challengeID, instanceID, inspection.Config.Labels, inspection.Name)
			if err != nil || currentUser != userID || currentVerification != verification {
				return nil, ErrCleanupTargetRejected
			}
			if inspection.HostConfig != nil {
				ports, err := cleanupObservedPorts(inspection.HostConfig.PortBindings)
				if err != nil {
					return nil, err
				}
				for _, port := range ports {
					seenPorts[port] = true
				}
			}
		}
		entry.Ports = sortedCleanupPorts(seenPorts)
		if verification {
			if len(entry.Ports) > 0 {
				return nil, ErrCleanupTargetRejected
			}
		} else {
			data, err := m.rdb.HGetAll(ctx, "instance:"+instanceID).Result()
			if err != nil {
				return nil, fmt.Errorf("cleanup instance metadata lookup failed")
			}
			if len(data) == 0 {
				if len(entry.Ports) != 1 {
					return nil, ErrCleanupTargetRejected
				}
				data = map[string]string{"challenge_id": strconv.Itoa(challengeID), "container_id": observed.ID, "user_id": userID, "port": strconv.Itoa(entry.Ports[0])}
			} else {
				entry.Orphan = false
			}
			if validateCleanupMetadata(challengeID, instanceID, data) != nil || data["container_id"] != observed.ID || data["user_id"] != userID {
				return nil, ErrCleanupTargetRejected
			}
			entry.Data = data
		}
		if err := mergeCleanupInstance(entries, entry); err != nil {
			return nil, err
		}
	}

	var cursor uint64
	for {
		keys, next, err := m.rdb.Scan(ctx, cursor, "instance:*", 100).Result()
		if err != nil {
			return nil, fmt.Errorf("cleanup metadata scan failed")
		}
		for _, key := range keys {
			data, err := m.rdb.HGetAll(ctx, key).Result()
			if err != nil {
				return nil, fmt.Errorf("cleanup metadata lookup failed")
			}
			if data["challenge_id"] != strconv.Itoa(challengeID) {
				continue
			}
			instanceID := strings.TrimPrefix(key, "instance:")
			if err := validateCleanupMetadata(challengeID, instanceID, data); err != nil {
				return nil, err
			}
			entry := challengeCleanupInstance{ChallengeID: challengeID, InstanceID: instanceID, ContainerID: data["container_id"], UserID: data["user_id"], Data: data}
			if err := mergeCleanupInstance(entries, entry); err != nil {
				return nil, err
			}
		}
		if next == 0 {
			break
		}
		cursor = next
	}
	return entries, nil
}

func (m *Manager) confirmCleanupContainer(ctx context.Context, entry challengeCleanupInstance) (bool, error) {
	if !cleanupContainerIDPattern.MatchString(entry.ContainerID) {
		return false, ErrCleanupTargetRejected
	}
	inspection, err := m.docker.ContainerInspect(ctx, entry.ContainerID)
	if errdefs.IsNotFound(err) {
		return true, nil
	}
	if err != nil {
		return false, fmt.Errorf("cleanup container state is uncertain")
	}
	if inspection.ContainerJSONBase == nil || inspection.Config == nil || inspection.ID != entry.ContainerID {
		return false, ErrCleanupTargetRejected
	}
	userID, verification, err := cleanupContainerLabels(entry.ChallengeID, entry.InstanceID, inspection.Config.Labels, inspection.Name)
	if err != nil || userID != entry.UserID || verification != entry.Verification {
		return false, ErrCleanupTargetRejected
	}
	return false, nil
}

func (m *Manager) confirmCleanupRuntimeAbsent(instanceID string) error {
	for _, compatibility := range []bool{false, true} {
		var path string
		var err error
		if compatibility {
			path, err = m.compatibilityFlagHostPath(instanceID)
		} else {
			path, err = m.dynamicFlagHostPath(instanceID)
		}
		if err != nil {
			return fmt.Errorf("cleanup runtime identity is invalid")
		}
		if _, err := os.Lstat(path); !errors.Is(err, os.ErrNotExist) {
			return fmt.Errorf("cleanup runtime removal is unconfirmed")
		}
	}
	return nil
}

var restoreCleanupMetadataScript = redis.NewScript(`
local kind = redis.call("TYPE", KEYS[1]).ok
if kind ~= "none" and kind ~= "hash" then return -1 end
local fields = {"challenge_id", "container_id", "user_id", "port"}
if kind == "hash" then
  for index, field in ipairs(fields) do
    if (redis.call("HGET", KEYS[1], field) or "") ~= ARGV[index] then return -1 end
  end
  return 0
end
for index, field in ipairs(fields) do redis.call("HSET", KEYS[1], field, ARGV[index]) end
return 1
`)

// A legacy orphan can have an ownerless port reservation. Restore only the
// journal's exact, absent instance hash so the existing legacy owner-CAS rule
// can finalize it; never replace a live or changed hash.
func (m *Manager) restoreCleanupMetadata(ctx context.Context, entry challengeCleanupInstance) error {
	if !entry.Orphan || entry.Verification {
		return nil
	}
	result, err := restoreCleanupMetadataScript.Run(ctx, m.rdb, []string{"instance:" + entry.InstanceID},
		entry.Data["challenge_id"], entry.ContainerID, entry.UserID, entry.Data["port"]).Int64()
	if err != nil {
		return fmt.Errorf("cleanup recovery metadata persistence failed")
	}
	if result < 0 {
		return ErrCleanupTargetRejected
	}
	return nil
}

func (m *Manager) removeCleanupInstance(ctx context.Context, entry challengeCleanupInstance) error {
	absent, err := m.confirmCleanupContainer(ctx, entry)
	if err != nil {
		return err
	}
	if !entry.Verification {
		current, err := m.rdb.HGetAll(ctx, "instance:"+entry.InstanceID).Result()
		if err != nil {
			return fmt.Errorf("cleanup current metadata lookup failed")
		}
		if len(current) > 0 && (validateCleanupMetadata(entry.ChallengeID, entry.InstanceID, current) != nil ||
			current["container_id"] != entry.ContainerID || current["user_id"] != entry.UserID || current["port"] != entry.Data["port"]) {
			return ErrCleanupTargetRejected
		}
	}
	if !absent {
		removeErr := m.docker.ContainerRemove(ctx, entry.ContainerID, container.RemoveOptions{Force: true})
		if removeErr != nil && !errdefs.IsNotFound(removeErr) {
			// A timed-out DELETE may already have succeeded. Only a current
			// NotFound confirms that outcome; otherwise the journal stays.
			confirmed, inspectErr := m.confirmCleanupContainer(ctx, entry)
			if inspectErr != nil || !confirmed {
				return fmt.Errorf("cleanup container removal is unconfirmed")
			}
		}
	}
	data := entry.Data
	if entry.Orphan || entry.Verification {
		data = nil
	}
	m.CleanupInstanceRuntime(entry.InstanceID, data, "reconcile_cleanup")
	if err := m.confirmCleanupRuntimeAbsent(entry.InstanceID); err != nil {
		return err
	}
	if !entry.Verification {
		if err := FinalizeRemovedInstance(ctx, m.rdb, m.portAllocator, entry.InstanceID, entry.Data, entry.Ports...); err != nil {
			return fmt.Errorf("cleanup instance finalization failed: %w", err)
		}
	}
	return nil
}

// CleanupDeletedChallengeResources accepts only Learn's committed deletion
// proof. No registered challenge, wildcard image, volume or network is removed.
func (m *Manager) CleanupDeletedChallengeResources(ctx context.Context, challengeID int, jobID string) (ChallengeResourceCleanupResult, error) {
	if challengeID <= 0 || !cleanupJobIDPattern.MatchString(jobID) {
		return ChallengeResourceCleanupResult{}, ErrCleanupTargetRejected
	}
	endOperation, err := acquireChallengeOperation(ctx, challengeID, true)
	if err != nil {
		return ChallengeResourceCleanupResult{}, err
	}
	defer endOperation()
	proof, err := m.fetchChallengeCleanupProof(ctx, challengeID, jobID)
	if err != nil {
		return ChallengeResourceCleanupResult{}, err
	}
	journalKey := "challenge_cleanup:" + jobID + ":instances"
	entries, err := m.collectCleanupInstances(ctx, challengeID, journalKey)
	if err != nil {
		return ChallengeResourceCleanupResult{}, err
	}
	ids := make([]string, 0, len(entries))
	for instanceID, entry := range entries {
		encoded, err := json.Marshal(entry)
		if err != nil {
			return ChallengeResourceCleanupResult{}, fmt.Errorf("cleanup journal encoding failed")
		}
		if err := m.rdb.HSet(ctx, journalKey, instanceID, string(encoded)).Err(); err != nil {
			return ChallengeResourceCleanupResult{}, fmt.Errorf("cleanup journal persistence failed")
		}
		ids = append(ids, instanceID)
	}
	sort.Strings(ids)
	for _, instanceID := range ids {
		if err := m.restoreCleanupMetadata(ctx, entries[instanceID]); err != nil {
			return ChallengeResourceCleanupResult{}, err
		}
		if err := m.removeCleanupInstance(ctx, entries[instanceID]); err != nil {
			return ChallengeResourceCleanupResult{}, err
		}
	}
	status := "absent"
	if len(entries) > 0 {
		status = "removed"
	}
	imageToRemove := proof.ImageReference
	if imageToRemove == "" {
		// A build that started before deletion can finish after Learn has
		// committed an empty-image job. Only new builder ownership labels
		// authorize that exact late tag; arbitrary external images stay.
		exact := fmt.Sprintf("ctf-challenge-%d:latest", challengeID)
		inspection, _, inspectErr := m.docker.ImageInspectWithRaw(ctx, exact)
		if inspectErr != nil && !errdefs.IsNotFound(inspectErr) {
			return ChallengeResourceCleanupResult{}, fmt.Errorf("cleanup late build lookup is unconfirmed")
		}
		if inspectErr == nil && inspection.Config != nil && inspection.Config.Labels["ctf-platform-built"] == "true" &&
			inspection.Config.Labels["ctf-platform-challenge-id"] == strconv.Itoa(challengeID) {
			imageToRemove = exact
		}
	}
	if imageToRemove != "" {
		imageResult, err := m.CleanupChallengeImage(ctx, challengeID)
		if err != nil {
			return ChallengeResourceCleanupResult{}, err
		}
		if imageResult.Status == "removed" {
			status = "removed"
		}
	}
	// Re-observe both systems after all destructive work, including retries
	// whose Docker result was previously lost. Keep the journal on any error.
	remaining, err := m.collectCleanupInstances(ctx, challengeID, "")
	if err != nil || len(remaining) > 0 {
		return ChallengeResourceCleanupResult{}, fmt.Errorf("cleanup resources remain or are unconfirmed")
	}
	for _, instanceID := range ids {
		entry := entries[instanceID]
		absent, err := m.confirmCleanupContainer(ctx, entry)
		if err != nil || !absent {
			return ChallengeResourceCleanupResult{}, fmt.Errorf("cleanup container absence is unconfirmed")
		}
		if !entry.Verification {
			member, err := m.rdb.SIsMember(ctx, "user_instances:"+entry.UserID, instanceID).Result()
			if err != nil || member {
				return ChallengeResourceCleanupResult{}, fmt.Errorf("cleanup user reference remains or is unconfirmed")
			}
		}
	}
	if imageToRemove != "" {
		_, _, err := m.docker.ImageInspectWithRaw(ctx, imageToRemove)
		if !errdefs.IsNotFound(err) {
			return ChallengeResourceCleanupResult{}, fmt.Errorf("cleanup image absence is unconfirmed")
		}
	}
	// Re-read the journal before removing it so a failed or altered recovery
	// record can never be acknowledged as a completed cleanup.
	journal, err := m.rdb.HGetAll(ctx, journalKey).Result()
	if err != nil || len(journal) != len(entries) {
		return ChallengeResourceCleanupResult{}, fmt.Errorf("cleanup journal completion is unconfirmed")
	}
	for field, value := range journal {
		var entry challengeCleanupInstance
		if json.Unmarshal([]byte(value), &entry) != nil || validateCleanupJournal(challengeID, field, entry) != nil {
			return ChallengeResourceCleanupResult{}, ErrCleanupTargetRejected
		}
		original, exists := entries[field]
		if !exists || entry.ContainerID != original.ContainerID || entry.UserID != original.UserID {
			return ChallengeResourceCleanupResult{}, ErrCleanupTargetRejected
		}
	}
	if err := m.rdb.Del(ctx, journalKey).Err(); err != nil {
		return ChallengeResourceCleanupResult{}, fmt.Errorf("cleanup journal completion failed")
	}
	return ChallengeResourceCleanupResult{ChallengeID: challengeID, JobID: jobID, Status: status, ImageReference: proof.ImageReference}, nil
}
