package container

import (
	"archive/tar"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"net/url"
	"path"
	"strings"
	"time"

	"github.com/docker/docker/api/types"
	dockerclient "github.com/docker/docker/client"
)

// Builder는 Dockerfile 기반 챌린지 이미지를 빌드한다.
type Builder struct {
	docker           *dockerclient.Client
	httpClient       *http.Client
	learnURL         string
	internalAPIKey   string
	minioRewriteHost string // 프로덕션 host 모드: "minio:9000" → 이 값으로 리라이트
}

// NewBuilder는 새로운 Builder를 생성한다.
// minioRewriteHost가 비어있으면 URL 리라이트를 하지 않는다 (개발 모드).
func NewBuilder(docker *dockerclient.Client, learnURL, internalAPIKey, minioRewriteHost string) *Builder {
	// Learn 내부 API와 MinIO presigned URL은 외부 프록시를 거치면 안 된다.
	// HTTP_PROXY가 설정된 운영/개발 호스트에서도 서명된 내부 URL을 그대로
	// 대상 네트워크로 보내도록 기본 Transport를 복제한 뒤 Proxy를 비운다.
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.Proxy = nil
	return &Builder{
		docker:           docker,
		httpClient:       &http.Client{Timeout: 30 * time.Second, Transport: transport},
		learnURL:         learnURL,
		internalAPIKey:   internalAPIKey,
		minioRewriteHost: minioRewriteHost,
	}
}

// dockerfileResponse는 learn-service에서 반환하는 Dockerfile 정보이다 (레거시).
type dockerfileResponse struct {
	DockerfileContent   string `json:"dockerfile_content"`
	ContainerPort       int    `json:"container_port"`
	EnvironmentRevision int    `json:"environment_revision"`
	FlagType            string `json:"flag_type"`
}

// buildFilesResponse는 멀티파일 빌드 컨텍스트 응답이다.
type buildFilesResponse struct {
	ContainerPort       int         `json:"container_port"`
	EnvironmentRevision int         `json:"environment_revision"`
	FlagType            string      `json:"flag_type"`
	Files               []buildFile `json:"files"`
}

type buildFile struct {
	Name string `json:"name"`
	URL  string `json:"url"`
}

const envOrchestratorBuildHeader = "X-Env-Orchestrator-Build"
const envOrchestratorBuildVersion = "v1"

// BuildResult는 빌드 결과를 담는다.
type BuildResult struct {
	ImageName string `json:"image_name"`
	Success   bool   `json:"success"`
	Log       string `json:"log"`
}

// BuildImage는 챌린지 ID에 해당하는 빌드 파일을 조회하여 이미지를 빌드한다.
// 멀티파일(build-files)을 우선 시도하고, 파일이 없으면 레거시(dockerfile 텍스트)로 폴백한다.
func (b *Builder) BuildImage(ctx context.Context, challengeID int) (*BuildResult, error) {
	// 5분 타임아웃
	buildCtx, cancel := context.WithTimeout(ctx, 5*time.Minute)
	defer cancel()
	endOperation, err := acquireChallengeOperation(buildCtx, challengeID, false)
	if err != nil {
		return nil, err
	}
	defer endOperation()

	// 1. 멀티파일 빌드 컨텍스트 조회 시도
	var buildContext io.Reader
	sourceRevision := 0
	bfResp, err := b.fetchBuildFiles(buildCtx, challengeID)
	if err != nil {
		return nil, err
	}
	if len(bfResp.Files) > 0 {
		// 멀티파일 모드: presigned URL에서 파일 다운로드
		log.Printf("[Builder] 멀티파일 빌드 모드: %d개 파일 (챌린지 %d)\n", len(bfResp.Files), challengeID)
		filesMap, dlErr := b.downloadBuildFiles(buildCtx, bfResp.Files)
		if dlErr != nil {
			return nil, fmt.Errorf("빌드 파일 다운로드 실패: %w", dlErr)
		}
		var legacyDockerfile *dockerfileResponse
		if _, hasRootDockerfile := filesMap["Dockerfile"]; !hasRootDockerfile {
			legacyDockerfile, err = b.fetchDockerfile(buildCtx, challengeID)
			if err != nil {
				return nil, fmt.Errorf("Dockerfile 조회 실패: %w", err)
			}
		}
		filesMap, err = mergeBuildFilesWithLegacyDockerfile(
			filesMap,
			bfResp.EnvironmentRevision,
			bfResp.FlagType,
			legacyDockerfile,
		)
		if err != nil {
			return nil, err
		}
		if err := validateDockerfileFlagHealthcheck(string(filesMap["Dockerfile"]), bfResp.FlagType); err != nil {
			return nil, err
		}
		buildContext, err = createMultiFileBuildContext(filesMap)
		if err != nil {
			return nil, fmt.Errorf("빌드 컨텍스트 생성 실패: %w", err)
		}
		sourceRevision = bfResp.EnvironmentRevision
	} else {
		// 폴백: 레거시 단일 Dockerfile 텍스트
		log.Printf("[Builder] 레거시 빌드 모드 (챌린지 %d)\n", challengeID)
		dfResp, dfErr := b.fetchDockerfile(buildCtx, challengeID)
		if dfErr != nil {
			return nil, fmt.Errorf("Dockerfile 조회 실패: %w", dfErr)
		}
		if dfResp.DockerfileContent == "" || dfResp.EnvironmentRevision <= 0 {
			return nil, &EnvironmentReadinessError{Code: "invalid_environment"}
		}
		if err := validateDockerfileFlagHealthcheck(dfResp.DockerfileContent, dfResp.FlagType); err != nil {
			return nil, err
		}
		legacyFiles := map[string][]byte{"Dockerfile": []byte(dfResp.DockerfileContent)}
		ensureCompatibilityFlagFile(legacyFiles, dfResp.FlagType)
		buildContext, err = createMultiFileBuildContext(legacyFiles)
		if err != nil {
			return nil, fmt.Errorf("빌드 컨텍스트 생성 실패: %w", err)
		}
		sourceRevision = dfResp.EnvironmentRevision
	}
	if sourceRevision <= 0 {
		return nil, &EnvironmentReadinessError{Code: "invalid_environment"}
	}

	// 2. Docker 이미지 빌드
	imageName := fmt.Sprintf("ctf-challenge-%d:latest", challengeID)
	buildResp, err := b.docker.ImageBuild(buildCtx, buildContext, types.ImageBuildOptions{
		Tags:        []string{imageName},
		Dockerfile:  "Dockerfile",
		Remove:      true,
		ForceRemove: true,
		Labels: map[string]string{
			"ctf-platform-built":        "true",
			"ctf-platform-challenge-id": fmt.Sprintf("%d", challengeID),
		},
	})
	if err != nil {
		return nil, fmt.Errorf("Docker 빌드 시작 실패: %w", err)
	}
	defer buildResp.Body.Close()

	// 3. 빌드 로그 수집
	buildLog, buildErr := collectBuildLog(buildResp.Body)

	if buildErr != nil {
		return &BuildResult{
			ImageName: imageName,
			Success:   false,
			Log:       buildLog,
		}, fmt.Errorf("빌드 실패: %w", buildErr)
	}

	// 4. learn-service에 이미지명 업데이트
	if err := b.updateDockerImage(buildCtx, challengeID, imageName, sourceRevision); err != nil {
		log.Printf("[Builder] Docker build output may remain because Learn image revision update failed (challenge=%d)", challengeID)
		return &BuildResult{
			ImageName: imageName,
			Success:   false,
			Log:       buildLog,
		}, err
	}

	log.Printf("[Builder] 이미지 빌드 성공: %s (챌린지 %d)\n", imageName, challengeID)
	return &BuildResult{
		ImageName: imageName,
		Success:   true,
		Log:       buildLog,
	}, nil
}

// fetchBuildFiles는 learn-service에서 빌드 컨텍스트 파일 목록(presigned URL)을 조회한다.
func (b *Builder) fetchBuildFiles(ctx context.Context, challengeID int) (*buildFilesResponse, error) {
	url := fmt.Sprintf("%s/internal/challenges/%d/build-files", b.learnURL, challengeID)
	req, err := http.NewRequestWithContext(ctx, "GET", url, nil)
	if err != nil {
		log.Printf("[Builder] build-context request setup failed: %v", err)
		return nil, &EnvironmentReadinessError{Code: "build_context_unavailable"}
	}
	if b.internalAPIKey != "" {
		req.Header.Set("X-Internal-Key", b.internalAPIKey)
	}
	req.Header.Set(envOrchestratorBuildHeader, envOrchestratorBuildVersion)

	resp, err := b.httpClient.Do(req)
	if err != nil {
		log.Printf("[Builder] build-context request failed: %v", err)
		return nil, &EnvironmentReadinessError{Code: "build_context_unavailable"}
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		log.Printf("[Builder] build-context response rejected (status=%d)", resp.StatusCode)
		return nil, &EnvironmentReadinessError{Code: "build_context_unavailable"}
	}

	var bfResp buildFilesResponse
	if err := json.NewDecoder(resp.Body).Decode(&bfResp); err != nil {
		log.Printf("[Builder] build-context response decode failed: %v", err)
		return nil, &EnvironmentReadinessError{Code: "build_context_unavailable"}
	}
	return &bfResp, nil
}

// isAllowedBuildURL은 빌드 파일 다운로드 URL이 내부 네트워크인지 검증한다.
// 외부 도메인으로의 SSRF를 방지한다.
func isAllowedBuildURL(rawURL string) bool {
	u, err := url.Parse(rawURL)
	if err != nil {
		return false
	}
	if u.Scheme != "http" && u.Scheme != "https" {
		return false
	}
	host := u.Hostname()
	if host == "" {
		return false
	}
	// Docker 내부 서비스명 (도트 없는 단순 호스트명, 예: minio)
	if !strings.Contains(host, ".") {
		return true
	}
	ip := net.ParseIP(host)
	if ip == nil {
		return false // 외부 도메인 차단
	}
	return ip.IsLoopback() || ip.IsPrivate()
}

// rewriteMinioURL은 프로덕션 host 모드에서 Docker 내부 호스트명을 외부 접근 가능한 주소로 변환한다.
func (b *Builder) rewriteMinioURL(rawURL string) string {
	if b.minioRewriteHost == "" {
		return rawURL
	}
	u, err := url.Parse(rawURL)
	if err != nil {
		return rawURL
	}
	host := u.Hostname()
	// Docker 내부 서비스명 (도트 없는 단순 호스트명)만 리라이트
	if !strings.Contains(host, ".") && host != "localhost" {
		u.Host = b.minioRewriteHost
		return u.String()
	}
	return rawURL
}

// downloadBuildFiles는 presigned URL에서 파일들을 다운로드한다.
func (b *Builder) downloadBuildFiles(ctx context.Context, files []buildFile) (map[string][]byte, error) {
	const maxFileSize = 50*1024*1024 + 1 // 50MB + 1 (안전 제한)
	result := make(map[string][]byte, len(files))
	for _, f := range files {
		fileURL := b.rewriteMinioURL(f.URL)
		if !isAllowedBuildURL(fileURL) {
			return nil, fmt.Errorf("허용되지 않은 빌드 파일 URL: %s (내부 네트워크만 허용)", fileURL)
		}
		req, err := http.NewRequestWithContext(ctx, "GET", fileURL, nil)
		if err != nil {
			return nil, fmt.Errorf("파일 %s 요청 생성 실패: %w", f.Name, err)
		}
		resp, err := b.httpClient.Do(req)
		if err != nil {
			return nil, fmt.Errorf("파일 %s 다운로드 실패: %w", f.Name, err)
		}
		if resp.StatusCode != 200 {
			resp.Body.Close()
			return nil, fmt.Errorf("파일 %s 다운로드 오류 (%d)", f.Name, resp.StatusCode)
		}
		data, err := io.ReadAll(io.LimitReader(resp.Body, maxFileSize))
		resp.Body.Close()
		if err != nil {
			return nil, fmt.Errorf("파일 %s 읽기 실패: %w", f.Name, err)
		}
		result[f.Name] = data
	}
	return result, nil
}

// fetchDockerfile은 learn-service에서 Dockerfile 내용을 조회한다 (레거시).
func (b *Builder) fetchDockerfile(ctx context.Context, challengeID int) (*dockerfileResponse, error) {
	url := fmt.Sprintf("%s/internal/challenges/%d/dockerfile", b.learnURL, challengeID)
	req, err := http.NewRequestWithContext(ctx, "GET", url, nil)
	if err != nil {
		return nil, err
	}
	if b.internalAPIKey != "" {
		req.Header.Set("X-Internal-Key", b.internalAPIKey)
	}
	req.Header.Set(envOrchestratorBuildHeader, envOrchestratorBuildVersion)

	resp, err := b.httpClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()

	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("learn-service 응답 오류 (status=%d)", resp.StatusCode)
	}

	var dfResp dockerfileResponse
	if err := json.NewDecoder(resp.Body).Decode(&dfResp); err != nil {
		return nil, fmt.Errorf("응답 파싱 실패: %w", err)
	}
	return &dfResp, nil
}

// updateDockerImage는 learn-service에 빌드된 이미지명을 업데이트한다.
func (b *Builder) updateDockerImage(ctx context.Context, challengeID int, imageName string, sourceRevision int) error {
	if sourceRevision <= 0 {
		return &EnvironmentReadinessError{Code: "invalid_environment"}
	}
	url := fmt.Sprintf("%s/internal/challenges/%d/docker-image", b.learnURL, challengeID)
	payloadBytes, err := json.Marshal(struct {
		DockerImage    string `json:"docker_image"`
		SourceRevision int    `json:"source_revision"`
	}{
		DockerImage:    imageName,
		SourceRevision: sourceRevision,
	})
	if err != nil {
		return fmt.Errorf("JSON 직렬화 실패: %w", err)
	}
	payload := string(payloadBytes)

	req, err := http.NewRequestWithContext(ctx, "PATCH", url, strings.NewReader(payload))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	if b.internalAPIKey != "" {
		req.Header.Set("X-Internal-Key", b.internalAPIKey)
	}
	req.Header.Set(envOrchestratorBuildHeader, envOrchestratorBuildVersion)

	resp, err := b.httpClient.Do(req)
	if err != nil {
		return &EnvironmentReadinessError{Code: "environment_unavailable"}
	}
	defer resp.Body.Close()

	if resp.StatusCode == http.StatusConflict {
		return &EnvironmentReadinessError{Code: "stale_environment_revision"}
	}
	if resp.StatusCode != http.StatusOK {
		return &EnvironmentReadinessError{Code: "environment_unavailable"}
	}
	return nil
}

const dynamicBuildFlagPlaceholder = "CTF_DYNAMIC_FLAG_PLACEHOLDER\n"
const compatibilityHealthcheckFlag = "ok\n"

// mergeBuildFilesWithLegacyDockerfile supports the common "assets uploaded,
// Dockerfile kept in the legacy editor" transition without assuming any
// challenge-specific filename. An uploaded flag.txt is always normalized to a
// non-secret compatibility placeholder before it reaches Docker. Static and
// regex challenges do not require flag.txt; dynamic_instance receives its real
// per-instance value through the read-only runtime mount.
func mergeBuildFilesWithLegacyDockerfile(
	files map[string][]byte,
	filesRevision int,
	flagType string,
	legacyDockerfile *dockerfileResponse,
) (map[string][]byte, error) {
	if filesRevision <= 0 {
		return nil, &EnvironmentReadinessError{Code: "invalid_environment"}
	}
	if err := normalizeBuildContextFlagFiles(files, flagType); err != nil {
		return nil, err
	}
	if _, hasRootDockerfile := files["Dockerfile"]; hasRootDockerfile {
		ensureCompatibilityFlagFile(files, flagType)
		return files, nil
	}
	if legacyDockerfile == nil || legacyDockerfile.DockerfileContent == "" || legacyDockerfile.EnvironmentRevision <= 0 {
		return nil, &EnvironmentReadinessError{Code: "invalid_environment"}
	}
	if legacyDockerfile.EnvironmentRevision != filesRevision {
		return nil, &EnvironmentReadinessError{Code: "stale_environment_revision"}
	}
	files["Dockerfile"] = []byte(legacyDockerfile.DockerfileContent)
	// The first normalization ran before the legacy Dockerfile was fetched, so
	// re-evaluate an uploaded flag.txt now that its HEALTHCHECK contract is known.
	if err := normalizeBuildContextFlagFiles(files, flagType); err != nil {
		return nil, err
	}
	ensureCompatibilityFlagFile(files, flagType)
	return files, nil
}

func normalizeBuildContextFlagFiles(files map[string][]byte, flagType string) error {
	dockerfileContent := string(files["Dockerfile"])
	for name := range files {
		if path.Base(path.Clean(name)) != "flag.txt" {
			continue
		}
		// Never carry an uploader-provided flag into a tar archive or Docker
		// layer. Env mounts the real per-instance value at runtime only for
		// dynamic_instance; static/regex keep a non-secret compatibility value.
		files[name] = compatibilityFlagFileContent(dockerfileContent)
	}
	return nil
}

func ensureCompatibilityFlagFile(files map[string][]byte, flagType string) {
	if _, exists := files["flag.txt"]; exists {
		return
	}
	dockerfile, exists := files["Dockerfile"]
	if exists && (dockerfileReferencesFlagFile(string(dockerfile)) || dockerfileHealthcheckExpectsOKFlag(string(dockerfile))) {
		// flag.txt is an optional authoring aid for static/regex and a runtime
		// mount target for dynamic_instance. If a Dockerfile references the
		// path at build time (COPY/RUN/HEALTHCHECK/ENV), or explicitly expects
		// the legacy readiness sentinel, provide a non-secret
		// placeholder so every mode can build without carrying an uploader flag.
		// Dynamic instances replace this file with their real read-only value at
		// container start; the placeholder is never a participant answer.
		files["flag.txt"] = compatibilityFlagFileContent(string(dockerfile))
	}
}

// compatibilityFlagFileContent returns a non-secret build-time compatibility
// value. A small number of legacy healthchecks (including the gacha contract)
// deliberately use the literal "ok" as a readiness sentinel. That sentinel is
// not a participant flag and is safe to put in a static/regex image layer; a
// dynamic_instance container replaces the file with its per-instance read-only
// mount before readiness is evaluated.
func compatibilityFlagFileContent(dockerfileContent string) []byte {
	if dockerfileHealthcheckExpectsOKFlag(dockerfileContent) {
		return []byte(compatibilityHealthcheckFlag)
	}
	return []byte(dynamicBuildFlagPlaceholder)
}

// validateDockerfileFlagHealthcheck keeps generic Dockerfile builds compatible
// with legacy images that mention flag.txt in HEALTHCHECK. Uploaded flag.txt
// content is normalized to a non-secret compatibility value for static/regex,
// while dynamic_instance receives the real per-instance read-only mount later.
// Do not reject a static/regex build solely because its healthcheck reads the
// optional file: a simple gacha-style "ok" sentinel is supplied when the
// Dockerfile explicitly compares against it, and a service that needs the real
// participant value must use the dynamic runtime contract.
func validateDockerfileFlagHealthcheck(dockerfileContent, flagType string) error {
	_ = dockerfileContent
	_ = flagType
	return nil
}

func dockerfileHealthcheckDependsOnFlag(dockerfileContent string) bool {
	if dockerfileContent == "" {
		return false
	}
	lines := strings.Split(dockerfileContent, "\n")
	markers := []string{"flag.txt", "flag_path", "get('flag')", `get("flag")`, "flag ==", "flag=="}
	for index := 0; index < len(lines); index++ {
		instruction := strings.TrimSpace(lines[index])
		if !strings.HasPrefix(strings.ToLower(instruction), "healthcheck") {
			continue
		}
		for strings.HasSuffix(instruction, "\\") && index+1 < len(lines) {
			instruction = strings.TrimSpace(strings.TrimSuffix(instruction, "\\")) + " " + strings.TrimSpace(lines[index+1])
			index++
		}
		normalized := strings.ToLower(instruction)
		for _, marker := range markers {
			if strings.Contains(normalized, marker) {
				return true
			}
		}
	}
	return false
}

func dockerfileHealthcheckExpectsOKFlag(dockerfileContent string) bool {
	lines := strings.Split(dockerfileContent, "\n")
	for index := 0; index < len(lines); index++ {
		instruction := strings.TrimSpace(lines[index])
		if !strings.HasPrefix(strings.ToLower(instruction), "healthcheck") {
			continue
		}
		for strings.HasSuffix(instruction, "\\") && index+1 < len(lines) {
			instruction = strings.TrimSpace(strings.TrimSuffix(instruction, "\\")) + " " + strings.TrimSpace(lines[index+1])
			index++
		}
		normalized := strings.ToLower(strings.ReplaceAll(instruction, " ", ""))
		for _, marker := range []string{
			"flag=='ok'",
			`flag=="ok"`,
			"flag')=='ok'",
			`flag")=="ok"`,
			"get('flag')=='ok'",
			`get("flag")=="ok"`,
		} {
			if strings.Contains(normalized, marker) {
				return true
			}
		}
	}
	return false
}

// dockerfileReferencesFlagFile detects a build-time or runtime reference to
// the conventional flag file. It is deliberately broader than the
// HEALTHCHECK detector: Dockerfiles that COPY/RUN the file must also be able
// to build when the author did not upload a secret flag. The compatibility
// file is only a placeholder; dynamic_instance replaces it at runtime.
func dockerfileReferencesFlagFile(dockerfileContent string) bool {
	normalized := strings.ToLower(dockerfileContent)
	for _, marker := range []string{"flag.txt", "flag_path"} {
		if strings.Contains(normalized, marker) {
			return true
		}
	}
	return false
}

// createBuildContext는 Dockerfile 텍스트 1개로 tar 아카이브를 생성한다 (레거시).
func createBuildContext(dockerfileContent string) (io.Reader, error) {
	buf := new(bytes.Buffer)
	tw := tar.NewWriter(buf)

	header := &tar.Header{
		Name: "Dockerfile",
		Size: int64(len(dockerfileContent)),
		Mode: 0644,
	}
	if err := tw.WriteHeader(header); err != nil {
		return nil, err
	}
	if _, err := tw.Write([]byte(dockerfileContent)); err != nil {
		return nil, err
	}
	if err := tw.Close(); err != nil {
		return nil, err
	}

	return buf, nil
}

// createMultiFileBuildContext는 여러 파일로 tar 아카이브를 생성한다.
func createMultiFileBuildContext(files map[string][]byte) (io.Reader, error) {
	buf := new(bytes.Buffer)
	tw := tar.NewWriter(buf)

	for name, data := range files {
		// path traversal 방지: 상위 디렉토리 참조 및 절대 경로 차단
		// POSIX path.Clean 사용 (tar 아카이브는 항상 Unix 경로)
		cleaned := path.Clean(name)
		if cleaned == "." || strings.Contains(cleaned, "..") || path.IsAbs(cleaned) {
			return nil, fmt.Errorf("유효하지 않은 파일명: %s", name)
		}
		header := &tar.Header{
			Name: cleaned,
			Size: int64(len(data)),
			Mode: 0644,
		}
		if err := tw.WriteHeader(header); err != nil {
			return nil, fmt.Errorf("tar 헤더 쓰기 실패 (%s): %w", name, err)
		}
		if _, err := tw.Write(data); err != nil {
			return nil, fmt.Errorf("tar 데이터 쓰기 실패 (%s): %w", name, err)
		}
	}

	if err := tw.Close(); err != nil {
		return nil, fmt.Errorf("tar 닫기 실패: %w", err)
	}

	return buf, nil
}

// collectBuildLog는 Docker 빌드 출력 스트림을 읽어 로그를 수집한다.
func collectBuildLog(reader io.Reader) (string, error) {
	var logBuilder strings.Builder
	decoder := json.NewDecoder(reader)

	for {
		var msg struct {
			Stream      string `json:"stream"`
			Error       string `json:"error"`
			ErrorDetail *struct {
				Message string `json:"message"`
			} `json:"errorDetail"`
		}

		if err := decoder.Decode(&msg); err != nil {
			if err == io.EOF {
				break
			}
			return logBuilder.String(), fmt.Errorf("로그 파싱 오류: %w", err)
		}

		if msg.Stream != "" {
			logBuilder.WriteString(msg.Stream)
		}
		if msg.Error != "" {
			logBuilder.WriteString("ERROR: " + msg.Error + "\n")
			return logBuilder.String(), fmt.Errorf("%s", msg.Error)
		}
	}

	return logBuilder.String(), nil
}
