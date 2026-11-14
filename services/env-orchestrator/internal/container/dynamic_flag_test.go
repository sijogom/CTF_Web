package container

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/ctf-platform/env-orchestrator/internal/security"
)

func dynamicFlagTestPolicy() dynamicFlagPolicyResponse {
	return dynamicFlagPolicyResponse{
		Enabled:        true,
		RuntimePath:    "/flag.txt",
		TTLSeconds:     1800,
		PolicyRevision: 1,
	}
}

func TestDynamicFlagPolicyOnlyPermitsTheV1RuntimePath(t *testing.T) {
	if runtimePath, err := dynamicFlagPolicyRuntimePath(dynamicFlagPolicyResponse{}); err != nil || runtimePath != "" {
		t.Fatalf("disabled policy = %q, %v", runtimePath, err)
	}
	if runtimePath, err := dynamicFlagPolicyRuntimePath(dynamicFlagTestPolicy()); err != nil || runtimePath != "/flag.txt" {
		t.Fatalf("enabled policy = %q, %v", runtimePath, err)
	}
	for _, invalid := range []dynamicFlagPolicyResponse{
		{Enabled: true, RuntimePath: "relative.flag", TTLSeconds: 1, PolicyRevision: 1},
		{Enabled: true, RuntimePath: "/other.flag", TTLSeconds: 1, PolicyRevision: 1},
		{Enabled: true, RuntimePath: "/flag.txt", TTLSeconds: 0, PolicyRevision: 1},
		{Enabled: true, RuntimePath: "/flag.txt", TTLSeconds: int(dynamicFlagMaxTTL/time.Second) + 1, PolicyRevision: 1},
		{Enabled: true, RuntimePath: "/flag.txt", TTLSeconds: 1, PolicyRevision: 0},
	} {
		if _, err := dynamicFlagPolicyRuntimePath(invalid); err == nil {
			t.Fatalf("invalid policy %#v was accepted", invalid)
		}
	}
}

func TestDynamicFlagRequestedExpiryIsClampedToPolicyTTL(t *testing.T) {
	now := time.Date(2030, 1, 1, 0, 0, 0, 0, time.UTC)
	policy := dynamicFlagTestPolicy()
	policy.TTLSeconds = 60

	clamped, err := dynamicFlagRequestedExpiry(now, now.Add(10*time.Minute), policy)
	if err != nil {
		t.Fatalf("dynamicFlagRequestedExpiry() error = %v", err)
	}
	if want := now.Add(time.Minute); !clamped.Equal(want) {
		t.Fatalf("clamped expiry = %s, want %s", clamped, want)
	}

	unchanged, err := dynamicFlagRequestedExpiry(now, now.Add(30*time.Second), policy)
	if err != nil {
		t.Fatalf("dynamicFlagRequestedExpiry() short lease error = %v", err)
	}
	if want := now.Add(30 * time.Second); !unchanged.Equal(want) {
		t.Fatalf("short lease = %s, want %s", unchanged, want)
	}
}

func TestDynamicFlagMountIsReadOnlyAndStaticHasNoMount(t *testing.T) {
	policy := dynamicFlagTestPolicy()
	if runtimePath, err := dynamicFlagPolicyRuntimePath(policy); err != nil || runtimePath == "" {
		t.Fatalf("policy did not validate: %q, %v", runtimePath, err)
	}
	config := security.DefaultPolicy().ToHostConfig()
	applyDynamicFlagRuntime(config, "/host/private/inst.flag", "/flag.txt")
	if len(config.Mounts) != 1 {
		t.Fatalf("mount count = %d, want 1", len(config.Mounts))
	}
	mounted := config.Mounts[0]
	if !mounted.ReadOnly || mounted.Target != "/flag.txt" || mounted.Source != "/host/private/inst.flag" {
		t.Fatalf("mount = %#v", mounted)
	}

	staticConfig := security.DefaultPolicy().ToHostConfig()
	if len(staticConfig.Mounts) != 0 {
		t.Fatalf("static/regex baseline unexpectedly has %d mounts", len(staticConfig.Mounts))
	}
}

func TestWriteDynamicFlagFileCreatesPrivateRegularFile(t *testing.T) {
	directory := t.TempDir()
	manager := &Manager{dynamicFlagDir: directory}
	instanceID := "inst_0123456789abcdef0123456789abcdef"
	filePath, err := manager.writeDynamicFlagFile(instanceID, "CTF{probe}")
	if err != nil {
		t.Fatalf("writeDynamicFlagFile() error = %v", err)
	}
	if filepath.Dir(filePath) != directory {
		t.Fatalf("file path escaped runtime directory: %q", filePath)
	}
	info, err := os.Stat(filePath)
	if err != nil {
		t.Fatalf("stat dynamic flag file: %v", err)
	}
	if !info.Mode().IsRegular() || info.Mode().Perm() != 0o444 {
		t.Fatalf("dynamic flag file mode = %v, want read-only regular file mode 0444", info.Mode())
	}
	directoryInfo, err := os.Stat(directory)
	if err != nil {
		t.Fatalf("stat dynamic flag runtime directory: %v", err)
	}
	if directoryInfo.Mode().Perm() != 0o700 {
		t.Fatalf("dynamic flag runtime directory mode = %v, want 0700", directoryInfo.Mode())
	}
	if _, err := manager.writeDynamicFlagFile(instanceID, "contains\nnewline"); err == nil {
		t.Fatal("newline-bearing dynamic flag was accepted")
	}
	manager.removeDynamicFlagFile(instanceID)
	if _, err := os.Stat(filePath); !os.IsNotExist(err) {
		t.Fatalf("dynamic flag file remained after cleanup: %v", err)
	}
	if _, err := (&Manager{dynamicFlagDir: "relative-runtime-dir"}).dynamicFlagHostPath(instanceID); err == nil {
		t.Fatal("relative dynamic flag runtime directory was accepted")
	}
	for _, unsafeID := range []string{"../" + instanceID, `..\\` + instanceID} {
		if _, err := manager.dynamicFlagHostPath(unsafeID); err == nil {
			t.Fatalf("unsafe dynamic flag instance ID was accepted: %q", unsafeID)
		}
	}
}

func TestCompatibilityFlagFileIsNonSecretAndUsesSeparateNamespace(t *testing.T) {
	directory := t.TempDir()
	manager := &Manager{dynamicFlagDir: directory}
	instanceID := "inst_0123456789abcdef0123456789abcdef"
	filePath, err := manager.writeCompatibilityFlagFile(instanceID)
	if err != nil {
		t.Fatalf("writeCompatibilityFlagFile() error = %v", err)
	}
	defer manager.removeCompatibilityFlagFile(instanceID)
	if filepath.Base(filePath) != "compat_"+instanceID+".flag" {
		t.Fatalf("compatibility file path = %q", filePath)
	}
	contents, err := os.ReadFile(filePath)
	if err != nil {
		t.Fatalf("read compatibility file: %v", err)
	}
	if string(contents) != "ok\n" {
		t.Fatalf("compatibility content = %q, want ok newline", contents)
	}
	if _, err := manager.dynamicFlagHostPath(instanceID); err != nil {
		t.Fatalf("dynamic path unexpectedly invalid: %v", err)
	}
	if _, ok := compatibilityFlagInstanceIDFromFileName(filepath.Base(filePath)); !ok {
		t.Fatal("compatibility file was not recognized by the orphan sweeper")
	}
}

func TestCleanupInstanceRuntimeRemovesUnrecordedDynamicFile(t *testing.T) {
	manager := &Manager{dynamicFlagDir: t.TempDir()}
	instanceID := "inst_0123456789abcdef0123456789abcdef"
	filePath, err := manager.writeDynamicFlagFile(instanceID, "CTF{probe}")
	if err != nil {
		t.Fatalf("writeDynamicFlagFile() error = %v", err)
	}
	manager.CleanupInstanceRuntime(instanceID, map[string]string{}, "reconcile_cleanup")
	if _, err := os.Stat(filePath); !os.IsNotExist(err) {
		t.Fatalf("unrecorded dynamic flag file remained after cleanup: %v", err)
	}
}

func TestIssueDynamicFlagSendsMetadataWithoutFlagAndDoesNotAllowPolicyMismatch(t *testing.T) {
	var received dynamicFlagIssueRequest
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		if request.Method != http.MethodPost || request.URL.Path != dynamicFlagIssuePath {
			t.Errorf("request = %s %s", request.Method, request.URL.Path)
		}
		if request.Header.Get("X-Internal-Key") != "test-key" {
			t.Errorf("missing internal key")
		}
		if err := json.NewDecoder(request.Body).Decode(&received); err != nil {
			t.Errorf("decode issue request: %v", err)
		}
		writer.Header().Set("Content-Type", "application/json")
		_, _ = writer.Write([]byte(`{"flag":"CTF{one-shot}","instance_locator":"opaque-locator","expires_at":"2030-01-01T00:00:00Z","runtime_path":"/flag.txt"}`))
	}))
	defer server.Close()

	manager := &Manager{httpClient: server.Client(), learnURL: server.URL, internalAPIKey: "test-key"}
	requestedExpiry := time.Date(2030, 1, 1, 0, 1, 0, 0, time.UTC)
	competitionID := "competition-1"
	issued, err := manager.issueDynamicFlag(context.Background(), dynamicFlagIssueRequest{
		ChallengeID:         42,
		InstanceID:          "inst_0123456789abcdef0123456789abcdef",
		UserID:              "user-1",
		CompetitionID:       &competitionID,
		EnvironmentRevision: 7,
		VerifiedImageID:     "sha256:verified",
		ExpiresAt:           requestedExpiry,
	}, dynamicFlagTestPolicy())
	if err != nil {
		t.Fatalf("issueDynamicFlag() error = %v", err)
	}
	if issued.Flag != "CTF{one-shot}" || issued.RuntimePath != "/flag.txt" {
		t.Fatalf("issued response = %#v", issued)
	}
	if received.ChallengeID != 42 || received.InstanceID == "" || received.EnvironmentRevision != 7 || received.VerifiedImageID != "sha256:verified" {
		t.Fatalf("received issue metadata = %#v", received)
	}
}

func TestDynamicFlagInstanceIDFromFileNameRejectsTraversal(t *testing.T) {
	valid := "inst_0123456789abcdef0123456789abcdef"
	verification := "verify_0123456789abcdef0123456789abcdef"
	validFiles := map[string]string{
		valid + ".flag":                   valid,
		"." + valid + ".tmp-random":      valid,
		verification + ".flag":            verification,
		"." + verification + ".tmp-random": verification,
	}
	for name, expectedID := range validFiles {
		if got, ok := dynamicFlagInstanceIDFromFileName(name); !ok || got != expectedID {
			t.Fatalf("file name %q parsed as %q, %t", name, got, ok)
		}
	}
	for _, name := range []string{"../" + valid + ".flag", "inst_short.flag", valid + ".other", ".tmp-random"} {
		if _, ok := dynamicFlagInstanceIDFromFileName(name); ok {
			t.Fatalf("unsafe file name %q was accepted", name)
		}
	}
}
