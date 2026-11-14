package container

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestNewBuilderDisablesEnvironmentProxyForInternalBuildRequests(t *testing.T) {
	builder := NewBuilder(nil, "http://learn-service:8100", "", "")
	transport, ok := builder.httpClient.Transport.(*http.Transport)
	if !ok {
		t.Fatalf("transport type = %T, want *http.Transport", builder.httpClient.Transport)
	}
	if transport.Proxy != nil {
		t.Fatal("internal build transport must not use HTTP_PROXY/HTTPS_PROXY")
	}
}

func TestUpdateDockerImageReturnsFailureWithoutResponseBody(t *testing.T) {
	var method string
	var path string
	var internalKey string
	var payload struct {
		DockerImage    string `json:"docker_image"`
		SourceRevision int    `json:"source_revision"`
	}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		method = r.Method
		path = r.URL.Path
		internalKey = r.Header.Get("X-Internal-Key")
		if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
			t.Errorf("decode payload: %v", err)
		}
		w.WriteHeader(http.StatusConflict)
		_, _ = w.Write([]byte("private downstream detail"))
	}))
	defer server.Close()

	builder := &Builder{
		httpClient:     server.Client(),
		learnURL:       server.URL,
		internalAPIKey: "test-internal-key",
	}
	err := builder.updateDockerImage(context.Background(), 42, "ctf-challenge-42:latest", 7)
	if err == nil {
		t.Fatal("updateDockerImage() error = nil, want rejection")
	}
	if ReadinessErrorCode(err) != "stale_environment_revision" {
		t.Fatalf("error code = %q, want stale_environment_revision", ReadinessErrorCode(err))
	}
	if err.Error() == "private downstream detail" {
		t.Fatalf("error leaks downstream response body: %q", err)
	}
	if method != http.MethodPatch || path != "/internal/challenges/42/docker-image" {
		t.Fatalf("request = %s %s", method, path)
	}
	if internalKey != "test-internal-key" || payload.DockerImage != "ctf-challenge-42:latest" || payload.SourceRevision != 7 {
		t.Fatalf("internal key/payload = %q/%q/%d", internalKey, payload.DockerImage, payload.SourceRevision)
	}
}

func TestUpdateDockerImageRejectsMissingSourceRevision(t *testing.T) {
	builder := &Builder{}
	err := builder.updateDockerImage(context.Background(), 42, "ctf-challenge-42:latest", 0)
	if ReadinessErrorCode(err) != "invalid_environment" {
		t.Fatalf("error code = %q, want invalid_environment", ReadinessErrorCode(err))
	}
}

func TestMergeBuildFilesAllowsDynamicRootFlagPlaceholder(t *testing.T) {
	files := map[string][]byte{
		"Dockerfile": []byte("FROM busybox"),
		"flag.txt":   []byte("FLAG{uploader-secret-must-not-reach-the-image}"),
	}
	merged, err := mergeBuildFilesWithLegacyDockerfile(files, 7, "dynamic_instance", nil)
	if err != nil {
		t.Fatalf("mergeBuildFilesWithLegacyDockerfile() error = %v", err)
	}
	if string(merged["Dockerfile"]) != "FROM busybox" || string(merged["flag.txt"]) != dynamicBuildFlagPlaceholder {
		t.Fatalf("merged files = %#v", merged)
	}
	if string(merged["flag.txt"]) == "FLAG{uploader-secret-must-not-reach-the-image}" {
		t.Fatal("raw flag was retained in the build context")
	}
}

func TestMergeBuildFilesNormalizesRootFlagForEveryChallengeMode(t *testing.T) {
	for _, flagType := range []string{"static", "regex", ""} {
		t.Run(flagType, func(t *testing.T) {
			files := map[string][]byte{
				"Dockerfile":      []byte("FROM busybox"),
				"assets/flag.txt": []byte("must not retain uploader content"),
			}
			merged, err := mergeBuildFilesWithLegacyDockerfile(files, 7, flagType, nil)
			if err != nil {
				t.Fatalf("mergeBuildFilesWithLegacyDockerfile() error = %v", err)
			}
			if string(merged["assets/flag.txt"]) != dynamicBuildFlagPlaceholder {
				t.Fatalf("normalized flag = %q", merged["assets/flag.txt"])
			}
		})
	}
}

func TestMergeBuildFilesAddsLegacyDockerfileOnlyWhenNeeded(t *testing.T) {
	files := map[string][]byte{"app.py": []byte("print('ok')")}
	merged, err := mergeBuildFilesWithLegacyDockerfile(files, 7, "static", &dockerfileResponse{
		DockerfileContent:   "FROM python:3.12-alpine",
		EnvironmentRevision: 7,
	})
	if err != nil {
		t.Fatalf("mergeBuildFilesWithLegacyDockerfile() error = %v", err)
	}
	if string(merged["Dockerfile"]) != "FROM python:3.12-alpine" {
		t.Fatalf("fallback Dockerfile = %q", merged["Dockerfile"])
	}
}

func TestMergeBuildFilesFailsClosedOnMissingOrStaleFallback(t *testing.T) {
	files := map[string][]byte{"app.py": []byte("print('ok')")}
	if _, err := mergeBuildFilesWithLegacyDockerfile(files, 7, "static", nil); ReadinessErrorCode(err) != "invalid_environment" {
		t.Fatalf("missing fallback code = %q", ReadinessErrorCode(err))
	}
	if _, err := mergeBuildFilesWithLegacyDockerfile(files, 7, "static", &dockerfileResponse{
		DockerfileContent:   "FROM python:3.12-alpine",
		EnvironmentRevision: 8,
	}); ReadinessErrorCode(err) != "stale_environment_revision" {
		t.Fatalf("stale fallback code = %q", ReadinessErrorCode(err))
	}
}

func TestValidateDockerfileFlagHealthcheckKeepsGenericBuildCompatible(t *testing.T) {
	flagDependent := `FROM busybox
HEALTHCHECK CMD-SHELL test -s /flag.txt && wget -qO- http://127.0.0.1:8080/health`
	for _, flagType := range []string{"static", "regex", "dynamic_instance"} {
		if err := validateDockerfileFlagHealthcheck(flagDependent, flagType); err != nil {
			t.Fatalf("flag-dependent healthcheck rejected for %q: %v", flagType, err)
		}
	}
	gachaLike := `FROM python:3.12-alpine
ENV FLAG_PATH=/flag.txt
HEALTHCHECK --interval=30s CMD-SHELL FLAG=$$(cat $$FLAG_PATH) && test -n "$$FLAG"`
	if err := validateDockerfileFlagHealthcheck(gachaLike, "static"); err != nil {
		t.Fatalf("gacha-like healthcheck rejected in compatibility mode: %v", err)
	}
}

func TestMergeBuildFilesAddsCompatibilityFlagForLegacyHealthcheck(t *testing.T) {
	files := map[string][]byte{
		"Dockerfile": []byte("FROM busybox\nHEALTHCHECK CMD-SHELL test -s /flag.txt"),
	}
	merged, err := mergeBuildFilesWithLegacyDockerfile(files, 7, "static", nil)
	if err != nil {
		t.Fatalf("mergeBuildFilesWithLegacyDockerfile() error = %v", err)
	}
	if string(merged["flag.txt"]) != dynamicBuildFlagPlaceholder {
		t.Fatalf("compatibility flag = %q", merged["flag.txt"])
	}
}

func TestMergeBuildFilesUsesReadinessSentinelForGachaHealthcheck(t *testing.T) {
	files := map[string][]byte{
		"Dockerfile": []byte(`FROM python:3.12-slim
HEALTHCHECK CMD python -c "import json; d=json.load(open('/health')); raise SystemExit(0 if d.get('flag')=='ok' else 1)"`),
	}
	merged, err := mergeBuildFilesWithLegacyDockerfile(files, 7, "static", nil)
	if err != nil {
		t.Fatalf("mergeBuildFilesWithLegacyDockerfile() error = %v", err)
	}
	if string(merged["flag.txt"]) != compatibilityHealthcheckFlag {
		t.Fatalf("gacha compatibility flag = %q", merged["flag.txt"])
	}
}

func TestLegacyDockerfileReevaluatesUploadedFlagSentinel(t *testing.T) {
	files := map[string][]byte{
		"flag.txt": []byte(dynamicBuildFlagPlaceholder),
	}
	merged, err := mergeBuildFilesWithLegacyDockerfile(files, 7, "static", &dockerfileResponse{
		DockerfileContent: `FROM python:3.12-slim
HEALTHCHECK CMD python -c "import json; d=json.load(open('/health')); raise SystemExit(0 if d.get('flag')=='ok' else 1)"`,
		EnvironmentRevision: 7,
	})
	if err != nil {
		t.Fatalf("mergeBuildFilesWithLegacyDockerfile() error = %v", err)
	}
	if string(merged["flag.txt"]) != compatibilityHealthcheckFlag {
		t.Fatalf("legacy gacha compatibility flag = %q", merged["flag.txt"])
	}
}

func TestMergeBuildFilesSentinelWithoutFlagPathAcrossModes(t *testing.T) {
	for _, flagType := range []string{"static", "regex", "dynamic_instance"} {
		t.Run(flagType, func(t *testing.T) {
			files := map[string][]byte{
				"Dockerfile": []byte(`FROM python:3.12-slim
HEALTHCHECK CMD python -c "import json; d=json.load(open('/health')); raise SystemExit(0 if d.get('flag')=='ok' else 1)"`),
			}
			merged, err := mergeBuildFilesWithLegacyDockerfile(files, 7, flagType, nil)
			if err != nil {
				t.Fatalf("mergeBuildFilesWithLegacyDockerfile() error = %v", err)
			}
			if string(merged["flag.txt"]) != compatibilityHealthcheckFlag {
				t.Fatalf("sentinel compatibility file is missing for %q", flagType)
			}
		})
	}
}

func TestMergeBuildFilesOrdinaryJSONHealthcheckDoesNotAddFlagFile(t *testing.T) {
	files := map[string][]byte{
		"Dockerfile": []byte(`FROM python:3.12-slim
HEALTHCHECK CMD python -c "import json; d=json.load(open('/health')); raise SystemExit(0 if d.get('status')=='ok' else 1)"`),
	}
	merged, err := mergeBuildFilesWithLegacyDockerfile(files, 7, "static", nil)
	if err != nil {
		t.Fatalf("mergeBuildFilesWithLegacyDockerfile() error = %v", err)
	}
	if _, exists := merged["flag.txt"]; exists {
		t.Fatal("ordinary JSON healthcheck received an unrelated flag file")
	}
}

func TestMergeBuildFilesAddsCompatibilityFlagForBuildTimeFlagReference(t *testing.T) {
	for _, flagType := range []string{"static", "regex", "dynamic_instance"} {
		t.Run(flagType, func(t *testing.T) {
			files := map[string][]byte{
				"Dockerfile": []byte("FROM busybox\nCOPY flag.txt /flag.txt\nRUN test -s /flag.txt"),
			}
			merged, err := mergeBuildFilesWithLegacyDockerfile(files, 7, flagType, nil)
			if err != nil {
				t.Fatalf("mergeBuildFilesWithLegacyDockerfile() error = %v", err)
			}
			if string(merged["flag.txt"]) != dynamicBuildFlagPlaceholder {
				t.Fatalf("compatibility flag = %q", merged["flag.txt"])
			}
		})
	}
}

func TestValidateDockerfileFlagHealthcheckKeepsOrdinaryHealthcheckAvailable(t *testing.T) {
	ordinary := `FROM busybox
HEALTHCHECK CMD-SHELL wget -qO- http://127.0.0.1:8080/health`
	for _, flagType := range []string{"", "static", "regex", "dynamic_instance"} {
		if err := validateDockerfileFlagHealthcheck(ordinary, flagType); err != nil {
			t.Fatalf("ordinary healthcheck rejected for %q: %v", flagType, err)
		}
	}
}

func TestDockerfileHealthcheckFlagDetectionJoinsContinuedInstructions(t *testing.T) {
	dockerfile := "FROM busybox\nHEALTHCHECK --interval=30s \\\n  CMD-SHELL FLAG=$$(cat /flag.txt) && test -n \"$$FLAG\""
	if !dockerfileHealthcheckDependsOnFlag(dockerfile) {
		t.Fatal("continued flag-dependent healthcheck was not detected")
	}
}

func TestBuildImageFailsClosedWhenBuildContextLookupFails(t *testing.T) {
	buildFileRequests := 0
	dockerfileRequests := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/internal/challenges/42/build-files":
			buildFileRequests++
			w.WriteHeader(http.StatusServiceUnavailable)
		case "/internal/challenges/42/dockerfile":
			dockerfileRequests++
			w.WriteHeader(http.StatusOK)
		default:
			t.Errorf("unexpected request path: %s", r.URL.Path)
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	defer server.Close()

	builder := &Builder{httpClient: server.Client(), learnURL: server.URL}
	result, err := builder.BuildImage(context.Background(), 42)
	if result != nil {
		t.Fatalf("BuildImage() result = %#v, want nil", result)
	}
	if ReadinessErrorCode(err) != "build_context_unavailable" {
		t.Fatalf("error code = %q, want build_context_unavailable", ReadinessErrorCode(err))
	}
	if buildFileRequests != 1 || dockerfileRequests != 0 {
		t.Fatalf("requests build-files/dockerfile = %d/%d, want 1/0", buildFileRequests, dockerfileRequests)
	}
}
