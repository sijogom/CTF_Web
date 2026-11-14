package container

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestChallengeEnvironmentPurposeBoundaryAndRevision(t *testing.T) {
	var purposes []string
	var internalKey string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		purposes = append(purposes, r.URL.Query().Get("purpose"))
		internalKey = r.Header.Get("X-Internal-Key")
		if r.URL.Path != "/internal/challenges/42/env" {
			t.Errorf("path = %q", r.URL.Path)
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"env_type":"image","docker_image":"ctf-challenge-42:latest","container_port":8080,"environment_revision":7,"environment_ready":true,"environment_verified_image_id":"sha256:abc123"}`))
	}))
	defer server.Close()

	manager := &Manager{
		httpClient:     server.Client(),
		learnURL:       server.URL,
		internalAPIKey: "test-internal-key",
	}

	instanceEnv, err := manager.fetchInstanceChallengeEnv(context.Background(), 42)
	if err != nil {
		t.Fatalf("fetchInstanceChallengeEnv() error = %v", err)
	}
	verificationEnv, err := manager.fetchVerificationChallengeEnv(context.Background(), 42)
	if err != nil {
		t.Fatalf("fetchVerificationChallengeEnv() error = %v", err)
	}

	if len(purposes) != 2 || purposes[0] != "instance" || purposes[1] != "verification" {
		t.Fatalf("purposes = %#v, want [instance verification]", purposes)
	}
	if internalKey != "test-internal-key" {
		t.Fatalf("X-Internal-Key = %q", internalKey)
	}
	if instanceEnv.EnvironmentRevision != 7 || verificationEnv.EnvironmentRevision != 7 {
		t.Fatalf("environment revisions = %d, %d; want 7", instanceEnv.EnvironmentRevision, verificationEnv.EnvironmentRevision)
	}
	if !instanceEnv.EnvironmentReady || instanceEnv.EnvironmentVerifiedImageID != "sha256:abc123" {
		t.Fatalf("instance verification identity = ready:%t image:%q", instanceEnv.EnvironmentReady, instanceEnv.EnvironmentVerifiedImageID)
	}
}

func TestRecordEnvironmentVerificationSendsOnlyAuditMetadata(t *testing.T) {
	var method string
	var path string
	var contentType string
	var internalKey string
	var payload map[string]json.RawMessage
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		method = r.Method
		path = r.URL.Path
		contentType = r.Header.Get("Content-Type")
		internalKey = r.Header.Get("X-Internal-Key")
		if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
			t.Errorf("decode payload: %v", err)
		}
		w.WriteHeader(http.StatusCreated)
	}))
	defer server.Close()

	manager := &Manager{
		httpClient:     server.Client(),
		learnURL:       server.URL,
		internalAPIKey: "test-internal-key",
	}
	imageID := "sha256:abc123"
	if err := manager.recordEnvironmentVerification(context.Background(), 42, environmentVerificationRecord{
		Revision:       7,
		Status:         "ready",
		FailureCode:    nil,
		ImageReference: "ctf-challenge-42:latest",
		ImageID:        &imageID,
		VerifiedBy:     "5fc2c167-3a8b-45f2-89d1-4900d5445f51",
	}); err != nil {
		t.Fatalf("recordEnvironmentVerification() error = %v", err)
	}

	if method != http.MethodPost || path != "/internal/challenges/42/environment-verifications" {
		t.Fatalf("request = %s %s", method, path)
	}
	if contentType != "application/json" || internalKey != "test-internal-key" {
		t.Fatalf("headers = Content-Type %q, X-Internal-Key %q", contentType, internalKey)
	}
	if string(payload["revision"]) != "7" || string(payload["status"]) != `"ready"` {
		t.Fatalf("payload revision/status = %s/%s", payload["revision"], payload["status"])
	}
	if string(payload["failure_code"]) != "null" {
		t.Fatalf("ready failure_code = %s, want null", payload["failure_code"])
	}
	for _, forbidden := range []string{"checked_at", "container_id", "private_ip", "docker_logs", "secret", "flag"} {
		if _, ok := payload[forbidden]; ok {
			t.Fatalf("payload unexpectedly contains %q", forbidden)
		}
	}
}

func TestFailedVerificationUsesNullImageID(t *testing.T) {
	var payload map[string]json.RawMessage
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
			t.Errorf("decode payload: %v", err)
		}
		w.WriteHeader(http.StatusCreated)
	}))
	defer server.Close()

	manager := &Manager{
		httpClient:     server.Client(),
		learnURL:       server.URL,
		internalAPIKey: "test-internal-key",
	}
	imageID := "sha256:stale"
	if err := manager.recordEnvironmentVerification(context.Background(), 42, environmentVerificationRecord{
		Revision:       7,
		Status:         "failed",
		ImageReference: "ctf-challenge-42:latest",
		ImageID:        &imageID,
		VerifiedBy:     "5fc2c167-3a8b-45f2-89d1-4900d5445f51",
	}); err != nil {
		t.Fatalf("recordEnvironmentVerification() error = %v", err)
	}
	if string(payload["image_id"]) != "null" {
		t.Fatalf("failed image_id = %s, want null", payload["image_id"])
	}
}

func TestReadyVerificationRejectsMissingImageID(t *testing.T) {
	manager := &Manager{}
	err := manager.recordEnvironmentVerification(context.Background(), 42, environmentVerificationRecord{
		Revision:       7,
		Status:         "ready",
		ImageReference: "ctf-challenge-42:latest",
		VerifiedBy:     "5fc2c167-3a8b-45f2-89d1-4900d5445f51",
	})
	if err == nil {
		t.Fatal("recordEnvironmentVerification() error = nil, want missing image identity rejection")
	}
}

func TestFetchInstanceEnvironmentNormalizesDownstreamDetail(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusConflict)
		_, _ = w.Write([]byte(`{"detail":"private downstream configuration detail"}`))
	}))
	defer server.Close()

	manager := &Manager{httpClient: server.Client(), learnURL: server.URL}
	_, err := manager.fetchInstanceChallengeEnv(context.Background(), 42)
	if err == nil {
		t.Fatal("fetchInstanceChallengeEnv() error = nil, want normalized readiness error")
	}
	if ReadinessErrorCode(err) != "environment_verification_required" {
		t.Fatalf("error code = %q", ReadinessErrorCode(err))
	}
	if err.Error() == "private downstream configuration detail" {
		t.Fatalf("error leaked downstream detail: %q", err)
	}
}

func TestCurrentInstanceEnvironmentRequiresMatchingRevisionAndImage(t *testing.T) {
	data := map[string]string{
		"environment_revision":          "7",
		"environment_verified_image_id": "sha256:verified",
	}
	matching := &challengeEnvResponse{
		EnvironmentReady:           true,
		EnvironmentRevision:        7,
		EnvironmentVerifiedImageID: "sha256:verified",
	}
	if imageID, err := validateCurrentInstanceEnvironment(data, matching); err != nil || imageID != "sha256:verified" {
		t.Fatalf("matching environment = %q, %v", imageID, err)
	}
	for _, current := range []*challengeEnvResponse{
		{EnvironmentReady: false, EnvironmentRevision: 7, EnvironmentVerifiedImageID: "sha256:verified"},
		{EnvironmentReady: true, EnvironmentRevision: 8, EnvironmentVerifiedImageID: "sha256:verified"},
		{EnvironmentReady: true, EnvironmentRevision: 7, EnvironmentVerifiedImageID: "sha256:changed"},
	} {
		if _, err := validateCurrentInstanceEnvironment(data, current); ReadinessErrorCode(err) != "environment_verification_required" {
			t.Fatalf("mismatch error code = %q", ReadinessErrorCode(err))
		}
	}
	if _, _, err := instanceEnvironmentMetadata(map[string]string{}); ReadinessErrorCode(err) != "environment_verification_required" {
		t.Fatalf("legacy metadata error code = %q", ReadinessErrorCode(err))
	}
}

func TestVerificationFailureCodeUsesLearnAllowlist(t *testing.T) {
	if code := verificationFailureCode(&EnvironmentReadinessError{Code: "start_timeout"}); code == nil || *code != "start_timeout" {
		t.Fatalf("start_timeout failure code = %v", code)
	}
	if code := verificationFailureCode(&EnvironmentReadinessError{Code: "port_unreachable"}); code == nil || *code != "port_unreachable" {
		t.Fatalf("port_unreachable failure code = %v", code)
	}
	if code := verificationFailureCode(&EnvironmentReadinessError{Code: "verification_record_failed"}); code == nil || *code != "verification_record_failed" {
		t.Fatalf("verification_record_failed failure code = %v", code)
	}
	if code := verificationFailureCode(http.ErrHandlerTimeout); code == nil || *code != "verification_execution_failed" {
		t.Fatalf("untyped failure code = %v, want verification_execution_failed", code)
	}
	if code := verificationFailureCode(&EnvironmentReadinessError{Code: "unexpected_internal_code"}); code == nil || *code != "verification_execution_failed" {
		t.Fatalf("unknown readiness failure code = %v, want verification_execution_failed", code)
	}
}
