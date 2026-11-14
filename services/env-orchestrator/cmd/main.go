// Package main은 라이브 환경 오케스트레이터의 엔트리포인트이다.
package main

import (
	"context"
	"crypto/subtle"
	"errors"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/signal"
	"regexp"
	"strconv"
	"syscall"
	"time"

	dockerclient "github.com/docker/docker/client"
	"github.com/gin-gonic/gin"
	"github.com/redis/go-redis/v9"

	"github.com/ctf-platform/env-orchestrator/internal/auth"
	"github.com/ctf-platform/env-orchestrator/internal/container"
	"github.com/ctf-platform/env-orchestrator/internal/lifecycle"
	"github.com/ctf-platform/env-orchestrator/internal/proxy"
)

// instanceIDRe는 유효한 인스턴스 ID 형식을 정의한다 (inst_ + 32자리 hex).
// 프록시 라우트에서 XSS 방어를 위해 입력 검증에 사용.
var instanceIDRe = regexp.MustCompile(`^inst_[0-9a-f]{32}$`)

// createInstanceErrorResponse is the public error boundary for environment
// creation. It intentionally never serializes wrapped Learn, Docker, Redis, or
// authorization error text to a participant.
func createInstanceErrorResponse(err error) (int, gin.H) {
	var capacityErr *container.GlobalCapacityError
	if errors.As(err, &capacityErr) {
		return http.StatusTooManyRequests, gin.H{
			"error": "현재 실습 환경 수가 허용 한도에 도달했습니다. 잠시 후 다시 시도하세요.",
			"code":  "global_capacity_reached",
			"limit": capacityErr.Limit,
		}
	}

	var readinessErr *container.EnvironmentReadinessError
	if errors.As(err, &readinessErr) {
		code := container.PublicEnvironmentErrorCode(readinessErr.Code)
		return http.StatusServiceUnavailable, gin.H{
			"error": container.PublicEnvironmentErrorMessage(code),
			"code":  code,
		}
	}

	return http.StatusConflict, gin.H{
		"error": "실습 환경을 시작할 수 없습니다. 요청 조건을 확인한 뒤 다시 시도하세요.",
		"code":  "instance_creation_rejected",
	}
}

func validInternalAPIKey(c *gin.Context, expected string) bool {
	provided := c.GetHeader("X-Internal-Key")
	return expected != "" && provided != "" && subtle.ConstantTimeCompare([]byte(provided), []byte(expected)) == 1
}

func main() {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	// 환경변수 로드
	redisURL := getEnv("REDIS_URL", "redis://:changeme@redis:6379/2")
	portMin := getEnvInt("CHALLENGE_PORT_MIN", 10000)
	portMax := getEnvInt("CHALLENGE_PORT_MAX", 60000)
	ttlMinutes := getEnvInt("CONTAINER_TTL_MINUTES", 30)
	maxPerUser := getEnvInt("CONTAINER_MAX_PER_USER", 2)
	maxContainers := getEnvInt("CONTAINER_MAX_GLOBAL", 50)
	reconcileMin := getEnvInt("RECONCILE_INTERVAL_MINUTES", 5)
	learnURL := getEnv("LEARN_SERVICE_URL", "http://127.0.0.1:8100")
	authURL := getEnv("AUTH_SERVICE_URL", "http://127.0.0.1:8020")
	challengeNet := getEnv("CHALLENGE_NETWORK", "ctf-challenges")
	internalAPIKey := getEnv("INTERNAL_API_KEY", "")
	readinessTimeoutSeconds := getEnvInt("CONTAINER_READINESS_TIMEOUT_SECONDS", 45)
	dynamicFlagRuntimeDir := getEnv("DYNAMIC_FLAG_RUNTIME_DIR", "")
	dynamicFlagSweepSeconds := getEnvInt("DYNAMIC_FLAG_SWEEP_INTERVAL_SECONDS", 30)
	if dynamicFlagSweepSeconds < 15 {
		dynamicFlagSweepSeconds = 15
	}

	// Redis 연결 (env-orchestrator 전용 — DB 2)
	// depends_on이 없으므로 Redis가 아직 준비 안 됐을 수 있음 → 재시도
	opt, err := redis.ParseURL(redisURL)
	if err != nil {
		log.Fatalf("Redis URL 파싱 실패: %v", err)
	}
	rdb := redis.NewClient(opt)
	for i := 0; i < 30; i++ {
		if err := rdb.Ping(ctx).Err(); err == nil {
			break
		}
		if i == 29 {
			log.Fatalf("Redis 연결 실패 (30회 재시도 초과)")
		}
		log.Printf("[Main] Redis 대기 중... (%d/30)", i+1)
		time.Sleep(2 * time.Second)
	}
	log.Println("[Main] Redis 연결 성공")

	// Auth Redis 연결 (DB 0 — admin:settings 동적 읽기용)
	authRedisOpt := *opt // 기본 설정 복사
	authRedisOpt.DB = 0  // auth 서비스가 사용하는 DB
	authRdb := redis.NewClient(&authRedisOpt)
	if err := authRdb.Ping(ctx).Err(); err != nil {
		log.Printf("[Main] Auth Redis 연결 실패 (admin 설정 동적 갱신 불가, 기본값 사용): %v", err)
		authRdb = nil
	} else {
		log.Println("[Main] Auth Redis (DB 0) 연결 성공 — admin:settings 동적 읽기 활성")
	}

	// Docker 클라이언트 초기화
	docker, err := dockerclient.NewClientWithOpts(
		dockerclient.FromEnv,
		dockerclient.WithAPIVersionNegotiation(),
	)
	if err != nil {
		log.Fatalf("Docker 클라이언트 초기화 실패: %v", err)
	}
	defer docker.Close()

	// Docker 연결 확인
	pingResp, err := docker.Ping(ctx)
	if err != nil {
		log.Fatalf("Docker 연결 실패: %v", err)
	}
	log.Printf("[Main] Docker 연결 성공 (API: %s)\n", pingResp.APIVersion)

	// 서비스 초기화
	portAllocator := container.NewPortAllocator(rdb, portMin, portMax)
	manager := container.NewManager(
		portAllocator,
		rdb,
		authRdb,
		docker,
		learnURL,
		authURL,
		challengeNet,
		ttlMinutes,
		maxPerUser,
		maxContainers,
		internalAPIKey,
		time.Duration(readinessTimeoutSeconds)*time.Second,
	)
	manager.SetDynamicFlagRuntimeDir(dynamicFlagRuntimeDir)
	manager.SweepDynamicFlagRuntime(ctx)
	go func() {
		ticker := time.NewTicker(time.Duration(dynamicFlagSweepSeconds) * time.Second)
		defer ticker.Stop()
		for {
			select {
			case <-ticker.C:
				manager.SweepDynamicFlagRuntime(ctx)
			case <-ctx.Done():
				return
			}
		}
	}()
	minioRewriteHost := getEnv("MINIO_REWRITE_HOST", "")
	builder := container.NewBuilder(docker, learnURL, internalAPIKey, minioRewriteHost)

	// TTL 감시자 시작
	ttlWatcher := lifecycle.NewTTLWatcher(
		rdb,
		docker,
		portAllocator,
		manager.CleanupInstanceRuntime,
		30*time.Second,
	)
	ttlWatcher.Start(ctx)

	// 정합성 검증기 시작
	reconciler := lifecycle.NewReconciler(
		rdb,
		docker,
		manager.CleanupInstanceRuntime,
		time.Duration(reconcileMin)*time.Minute,
		portAllocator,
	)
	reconciler.Start(ctx)

	// 프록시는 매 요청마다 Docker 실행 상태와 전용 네트워크 대상을 다시 확인한다.
	// Redis에 남은 레코드나 호스트 포트 폴백을 실제 연결 가능 상태로 취급하지 않는다.
	resolveTarget := func(instanceID string) (string, int, error) {
		return manager.ResolveInstanceTarget(context.Background(), instanceID)
	}
	termProxy := proxy.NewTerminalProxy(resolveTarget)
	vncProxy := proxy.NewVNCProxy(resolveTarget)
	httpProxy := proxy.NewHTTPProxy(resolveTarget)

	// Gin 라우터 설정
	r := gin.Default()
	// 인증 미들웨어는 bearer/cookie를 auth-service에서 검증한 뒤에만
	// 신뢰할 수 있는 X-User-* 요청 컨텍스트를 설정한다.
	authMW := auth.Middleware(authURL)

	// HTTP 프록시 — 인증과 소유권(대회 인스턴스는 현재 대회 접근 조건) 확인 필수
	proxyRoutes := r.Group("/env/proxy", auth.ProxyMiddleware(authURL))
	proxyRoutes.Any("/:id/*path", func(c *gin.Context) {
		instanceID := c.Param("id")
		// 인스턴스 ID 형식 검증 — XSS 방어 (prefix가 <script>에 삽입되므로)
		if !instanceIDRe.MatchString(instanceID) {
			c.JSON(http.StatusBadRequest, gin.H{"error": "잘못된 인스턴스 ID 형식입니다"})
			return
		}
		userID := c.GetHeader("X-User-ID")
		if err := manager.RequireInstanceAccess(c.Request.Context(), instanceID, userID); err != nil {
			c.JSON(http.StatusForbidden, gin.H{"error": "해당 인스턴스에 접근할 수 없습니다"})
			return
		}
		proxyPath := c.Param("path")
		// Gin은 *path에 선행 "/" 포함 → 제거
		if len(proxyPath) > 0 && proxyPath[0] == '/' {
			proxyPath = proxyPath[1:]
		}
		httpProxy.HandleProxy(c.Writer, c.Request, instanceID, proxyPath)
	})

	// 헬스체크
	r.GET("/health", func(c *gin.Context) {
		reqCtx := c.Request.Context()
		portCount, _ := portAllocator.AllocatedCount(reqCtx)

		// 실제 활성 인스턴스 수 (instance:* 키 개수)
		var instanceCount int64
		var cursor uint64
		for {
			keys, next, err := rdb.Scan(reqCtx, cursor, "instance:*", 100).Result()
			if err != nil {
				break
			}
			instanceCount += int64(len(keys))
			cursor = next
			if cursor == 0 {
				break
			}
		}

		c.JSON(http.StatusOK, gin.H{
			"status":           "ok",
			"service":          "env-orchestrator",
			"allocated_ports":  portCount,
			"active_instances": instanceCount,
		})
	})

	// Learn 관리자 삭제가 DB 행을 지우기 전에 호출하는 내부 image 정리 경계다.
	// 일반 사용자·Caddy 경로에는 노출하지 않고, 공유 내부 키 없이는 항상 거부한다.
	r.POST("/internal/challenges/:id/cleanup-image", func(c *gin.Context) {
		if !validInternalAPIKey(c, internalAPIKey) {
			c.JSON(http.StatusUnauthorized, gin.H{"error": "내부 인증이 필요합니다"})
			return
		}

		challengeID := 0
		if _, err := fmt.Sscanf(c.Param("id"), "%d", &challengeID); err != nil || challengeID <= 0 {
			c.JSON(http.StatusBadRequest, gin.H{"error": "유효하지 않은 챌린지 ID입니다"})
			return
		}

		result, err := manager.CleanupChallengeImage(c.Request.Context(), challengeID)
		if err != nil {
			if errors.Is(err, container.ErrChallengeImageInUse) {
				c.JSON(http.StatusConflict, gin.H{"error": "문제 환경 컨테이너를 먼저 종료해야 합니다", "code": "challenge_image_in_use"})
				return
			}
			log.Printf("[Main] 문제 image 정리 실패 (challenge=%d): %v", challengeID, err)
			c.JSON(http.StatusServiceUnavailable, gin.H{"error": "문제 image를 정리할 수 없습니다", "code": "challenge_image_cleanup_failed"})
			return
		}
		c.JSON(http.StatusOK, result)
	})

	// Resource cleanup is authorized only by Learn's committed deletion job.
	// It is separate from the legacy image-only endpoint for compatibility.
	r.POST("/internal/challenges/:id/cleanup-resources", func(c *gin.Context) {
		if !validInternalAPIKey(c, internalAPIKey) {
			c.JSON(http.StatusUnauthorized, gin.H{"error": "내부 인증이 필요합니다"})
			return
		}
		challengeID, err := strconv.Atoi(c.Param("id"))
		if err != nil || challengeID <= 0 {
			c.JSON(http.StatusBadRequest, gin.H{"error": "유효하지 않은 챌린지 ID입니다", "code": "cleanup_target_rejected"})
			return
		}
		var request struct {
			JobID string `json:"job_id" binding:"required"`
		}
		if err := c.ShouldBindJSON(&request); err != nil {
			c.JSON(http.StatusBadRequest, gin.H{"error": "정리 작업 ID가 필요합니다", "code": "cleanup_target_rejected"})
			return
		}
		result, err := manager.CleanupDeletedChallengeResources(c.Request.Context(), challengeID, request.JobID)
		if err != nil {
			if errors.Is(err, container.ErrCleanupChallengeExists) {
				c.JSON(http.StatusConflict, gin.H{"error": "현재 존재하는 문제는 정리할 수 없습니다", "code": "cleanup_challenge_exists"})
				return
			}
			if errors.Is(err, container.ErrCleanupTargetRejected) {
				c.JSON(http.StatusConflict, gin.H{"error": "삭제 정리 대상을 확인할 수 없습니다", "code": "cleanup_target_rejected"})
				return
			}
			log.Printf("[Main] deleted challenge resource cleanup incomplete (challenge=%d)", challengeID)
			c.JSON(http.StatusServiceUnavailable, gin.H{"error": "문제 리소스 정리를 확인하지 못했습니다", "code": "env_orchestrator_unavailable"})
			return
		}
		c.JSON(http.StatusOK, result)
	})

	// REST API — 인증 필수
	api := r.Group("/instances", authMW)
	{
		// 컨테이너 생성
		api.POST("/create", func(c *gin.Context) {
			// X-User-ID 헤더에서 사용자 ID 추출 (Caddy forward_auth가 주입)
			userID := c.GetHeader("X-User-ID")
			if userID == "" {
				c.JSON(http.StatusUnauthorized, gin.H{"error": "인증이 필요합니다"})
				return
			}

			var body struct {
				ChallengeID   int    `json:"challenge_id" binding:"required"`
				CompetitionID string `json:"competition_id"`
			}
			if err := c.ShouldBindJSON(&body); err != nil {
				c.JSON(http.StatusBadRequest, gin.H{
					"error": "실습 환경 생성 요청 형식이 올바르지 않습니다.",
					"code":  "invalid_create_request",
				})
				return
			}

			req := container.CreateRequest{
				UserID:        userID,
				ChallengeID:   body.ChallengeID,
				CompetitionID: body.CompetitionID,
			}

			instance, err := manager.CreateInstance(c.Request.Context(), req)
			if err != nil {
				status, response := createInstanceErrorResponse(err)
				c.JSON(status, response)
				return
			}
			c.JSON(http.StatusCreated, instance)
		})

		// 내 인스턴스 목록 (좀비 참조 자동 정리)
		api.GET("", func(c *gin.Context) {
			userID := c.GetHeader("X-User-ID")
			if userID == "" {
				c.JSON(http.StatusUnauthorized, gin.H{"error": "인증이 필요합니다"})
				return
			}

			userKey := fmt.Sprintf("user_instances:%s", userID)
			instanceIDs, err := rdb.SMembers(c.Request.Context(), userKey).Result()
			if err != nil {
				c.JSON(http.StatusInternalServerError, gin.H{"error": "조회 실패"})
				return
			}

			var instances []map[string]string
			for _, id := range instanceIDs {
				data, err := rdb.HGetAll(c.Request.Context(), fmt.Sprintf("instance:%s", id)).Result()
				if err == nil && len(data) > 0 {
					data["id"] = id
					instances = append(instances, data)
				} else {
					// 좀비 참조 정리 — instance 키가 만료되었으므로 셋에서 제거
					rdb.SRem(c.Request.Context(), userKey, id)
				}
			}
			if instances == nil {
				instances = []map[string]string{}
			}
			c.JSON(http.StatusOK, instances)
		})

		// 컨테이너 정지
		api.POST("/:id/stop", func(c *gin.Context) {
			instanceID := c.Param("id")
			userID := c.GetHeader("X-User-ID")
			if userID == "" {
				c.JSON(http.StatusUnauthorized, gin.H{"error": "인증이 필요합니다"})
				return
			}

			if err := manager.StopInstance(c.Request.Context(), instanceID, userID); err != nil {
				c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
				return
			}
			c.JSON(http.StatusOK, gin.H{"message": "인스턴스가 정지되었습니다"})
		})

		// 시간 연장
		api.POST("/:id/extend", func(c *gin.Context) {
			instanceID := c.Param("id")
			userID := c.GetHeader("X-User-ID")
			if userID == "" {
				c.JSON(http.StatusUnauthorized, gin.H{"error": "인증이 필요합니다"})
				return
			}

			extension, err := manager.ExtendInstance(c.Request.Context(), instanceID, userID)
			if err != nil {
				c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
				return
			}
			c.JSON(http.StatusOK, gin.H{
				"message":          "시간이 연장되었습니다",
				"expires_at":       extension.ExpiresAt.UTC().Format(time.RFC3339),
				"extend_count":     extension.ExtendCount,
				"max_extend_count": extension.MaxExtendCount,
			})
		})

		// 접속 정보 (소유자만 접근 가능)
		api.GET("/:id/connect", func(c *gin.Context) {
			instanceID := c.Param("id")
			userID := c.GetHeader("X-User-ID")
			reqCtx := c.Request.Context()
			if err := manager.RequireInstanceAccess(reqCtx, instanceID, userID); err != nil {
				c.JSON(http.StatusForbidden, gin.H{"error": "해당 인스턴스에 대한 권한이 없습니다"})
				return
			}
			data, err := rdb.HGetAll(reqCtx, fmt.Sprintf("instance:%s", instanceID)).Result()
			if err != nil || len(data) == 0 {
				c.JSON(http.StatusNotFound, gin.H{"error": "인스턴스를 찾을 수 없습니다"})
				return
			}
			c.JSON(http.StatusOK, gin.H{
				"port":        data["port"],
				"status":      data["status"],
				"ws_terminal": fmt.Sprintf("/ws/terminal/%s", instanceID),
				"ws_vnc":      fmt.Sprintf("/ws/vnc/%s", instanceID),
			})
		})
	}

	// 관리자 API — 인증 + 역할 확인
	admin := r.Group("/admin", authMW)
	{
		// 전체 활성 인스턴스 목록
		admin.GET("/instances", func(c *gin.Context) {
			role := c.GetHeader("X-User-Role")
			if role != "admin" && role != "superadmin" {
				c.JSON(http.StatusForbidden, gin.H{"error": "관리자 권한이 필요합니다"})
				return
			}

			reqCtx := c.Request.Context()
			// Redis에서 모든 instance:* 키 스캔
			var allInstances []map[string]interface{}
			var cursor uint64
			for {
				keys, nextCursor, err := rdb.Scan(reqCtx, cursor, "instance:*", 100).Result()
				if err != nil {
					break
				}
				for _, key := range keys {
					data, err := rdb.HGetAll(reqCtx, key).Result()
					if err != nil || len(data) == 0 {
						continue
					}
					inst := make(map[string]interface{})
					for k, v := range data {
						inst[k] = v
					}
					// key에서 인스턴스 ID 추출
					inst["id"] = key[len("instance:"):]
					allInstances = append(allInstances, inst)
				}
				cursor = nextCursor
				if cursor == 0 {
					break
				}
			}

			if allInstances == nil {
				allInstances = []map[string]interface{}{}
			}
			c.JSON(http.StatusOK, allInstances)
		})

		// 챌린지 이미지 빌드 (Dockerfile → Docker 이미지)
		admin.POST("/challenges/:id/build", func(c *gin.Context) {
			role := c.GetHeader("X-User-Role")
			if role != "admin" && role != "superadmin" {
				c.JSON(http.StatusForbidden, gin.H{"error": "관리자 권한이 필요합니다"})
				return
			}

			challengeID := 0
			if _, err := fmt.Sscanf(c.Param("id"), "%d", &challengeID); err != nil || challengeID <= 0 {
				c.JSON(http.StatusBadRequest, gin.H{"error": "유효하지 않은 챌린지 ID입니다"})
				return
			}

			result, err := builder.BuildImage(c.Request.Context(), challengeID)
			if err != nil {
				status := http.StatusInternalServerError
				resp := gin.H{"error": "문제 이미지를 빌드하지 못했습니다. 빌드 로그를 확인하세요."}
				var readinessErr *container.EnvironmentReadinessError
				if errors.As(err, &readinessErr) {
					status = http.StatusUnprocessableEntity
					resp["error"] = container.PublicEnvironmentErrorMessage(container.PublicEnvironmentErrorCode(readinessErr.Code))
					resp["code"] = readinessErr.Code
				}
				if result != nil {
					resp["log"] = result.Log
					resp["image_name"] = result.ImageName
				}
				c.JSON(status, resp)
				return
			}

			// 이미지 빌드만 성공한 상태를 사용자 환경으로 노출하지 않는다.
			// 빌드 직후 동일한 revision/image를 전용 네트워크에서 검증하고,
			// 검증 실패는 공개·인스턴스 생성 전에 명시적인 readiness 오류로
			// 반환한다. 기존의 수동 verify-environment endpoint는 재시도 경로로
			// 계속 유지한다.
			verifiedBy := c.GetHeader("X-User-ID")
			if verifiedBy == "" {
				c.JSON(http.StatusUnprocessableEntity, gin.H{
					"error":      "문제 환경 검증 주체를 확인할 수 없습니다.",
					"code":       "verification_actor_required",
					"image_name": result.ImageName,
					"log":        result.Log,
				})
				return
			}
			verification, verificationErr := manager.VerifyChallengeEnvironment(
				c.Request.Context(),
				challengeID,
				verifiedBy,
			)
			if verificationErr != nil {
				status := http.StatusInternalServerError
				code := "verification_failed"
				var readinessErr *container.EnvironmentReadinessError
				if errors.As(verificationErr, &readinessErr) {
					status = http.StatusUnprocessableEntity
					code = readinessErr.Code
					if readinessErr.Code == "verification_in_progress" {
						status = http.StatusConflict
					}
				}
				c.JSON(status, gin.H{
					"error":      "이미지는 빌드됐지만 실행 준비 검증에 실패했습니다. 환경 설정과 컨테이너 로그를 확인하세요.",
					"code":       code,
					"image_name": result.ImageName,
					"log":        result.Log,
				})
				return
			}
			c.JSON(http.StatusOK, gin.H{
				"message":      "이미지 빌드 및 환경 검증 성공",
				"image_name":   result.ImageName,
				"log":          result.Log,
				"verification": verification,
			})
		})

		// 문제 환경 검증 — 실제 공개·배정 전에 전용 네트워크에서 준비 상태를 확인한다.
		admin.POST("/challenges/:id/verify-environment", func(c *gin.Context) {
			role := c.GetHeader("X-User-Role")
			if role != "admin" && role != "superadmin" {
				c.JSON(http.StatusForbidden, gin.H{"error": "관리자 권한이 필요합니다"})
				return
			}

			challengeID := 0
			if _, err := fmt.Sscanf(c.Param("id"), "%d", &challengeID); err != nil || challengeID <= 0 {
				c.JSON(http.StatusBadRequest, gin.H{"error": "유효하지 않은 챌린지 ID입니다"})
				return
			}

			verifiedBy := c.GetHeader("X-User-ID")
			result, err := manager.VerifyChallengeEnvironment(c.Request.Context(), challengeID, verifiedBy)
			if err != nil {
				status := http.StatusInternalServerError
				code := "verification_failed"
				var readinessErr *container.EnvironmentReadinessError
				if errors.As(err, &readinessErr) {
					status = http.StatusUnprocessableEntity
					code = readinessErr.Code
					if readinessErr.Code == "verification_in_progress" {
						status = http.StatusConflict
					}
				}
				// The response already exposes the allowed failure code. Keep raw Docker
				// and downstream errors out of request logs.
				err = errors.New("details withheld")
				log.Printf("[Admin] 문제 환경 검증 실패 (challenge=%d, code=%s): %v", challengeID, code, err)
				c.JSON(status, gin.H{
					"error": "문제 환경 검증에 실패했습니다. 이미지, 포트, 리스닝 주소, HEALTHCHECK를 확인하세요.",
					"code":  code,
				})
				return
			}

			c.JSON(http.StatusOK, result)
		})

		// 관리자 강제 종료 (소유자 검증 없음)
		admin.POST("/instances/:id/force-stop", func(c *gin.Context) {
			role := c.GetHeader("X-User-Role")
			if role != "admin" && role != "superadmin" {
				c.JSON(http.StatusForbidden, gin.H{"error": "관리자 권한이 필요합니다"})
				return
			}

			instanceID := c.Param("id")
			instanceKey := fmt.Sprintf("instance:%s", instanceID)
			reqCtx := c.Request.Context()

			data, err := rdb.HGetAll(reqCtx, instanceKey).Result()
			if err != nil || len(data) == 0 {
				c.JSON(http.StatusNotFound, gin.H{"error": "인스턴스를 찾을 수 없습니다"})
				return
			}

			// 소유자와 관계없이 강제 종료
			ownerID := data["user_id"]
			if err := manager.StopInstance(reqCtx, instanceID, ownerID); err != nil {
				c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
				return
			}
			c.JSON(http.StatusOK, gin.H{"message": "인스턴스가 강제 종료되었습니다"})
		})
	}

	// WebSocket 엔드포인트 — 인증 + 소유권 검증 필수
	ws := r.Group("/ws", authMW)
	{
		ws.GET("/terminal/:id", func(c *gin.Context) {
			instanceID := c.Param("id")
			userID := c.GetHeader("X-User-ID")
			if err := manager.RequireInstanceAccess(c.Request.Context(), instanceID, userID); err != nil {
				c.JSON(http.StatusForbidden, gin.H{"error": "해당 인스턴스에 대한 권한이 없습니다"})
				return
			}
			termProxy.HandleWebSocket(c.Writer, c.Request, instanceID)
		})

		ws.GET("/vnc/:id", func(c *gin.Context) {
			instanceID := c.Param("id")
			userID := c.GetHeader("X-User-ID")
			if err := manager.RequireInstanceAccess(c.Request.Context(), instanceID, userID); err != nil {
				c.JSON(http.StatusForbidden, gin.H{"error": "해당 인스턴스에 대한 권한이 없습니다"})
				return
			}
			vncProxy.HandleWebSocket(c.Writer, c.Request, instanceID)
		})
	}

	// host network 배포에서는 Caddy만 공개 진입점이어야 한다. base Compose는
	// 컨테이너 네트워크에서 접근해야 하므로 기본값을 유지하고, host-network
	// Compose override가 loopback 주소를 명시한다.
	listenAddr := getEnv("LISTEN_ADDR", ":8200")

	// 서버 시작
	srv := &http.Server{
		Addr:              listenAddr,
		Handler:           r,
		ReadHeaderTimeout: 10 * time.Second,
		ReadTimeout:       30 * time.Second,
		WriteTimeout:      120 * time.Second,
		IdleTimeout:       60 * time.Second,
	}

	go func() {
		log.Printf("[Main] 서버 시작: %s", listenAddr)
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatalf("서버 오류: %v", err)
		}
	}()

	// Graceful shutdown
	quit := make(chan os.Signal, 1)
	signal.Notify(quit, syscall.SIGINT, syscall.SIGTERM)
	<-quit

	log.Println("[Main] 종료 시작...")
	ttlWatcher.Stop()
	reconciler.Stop()

	shutdownCtx, shutdownCancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer shutdownCancel()
	if err := srv.Shutdown(shutdownCtx); err != nil {
		log.Fatalf("서버 종료 실패: %v", err)
	}

	rdb.Close()
	if authRdb != nil {
		authRdb.Close()
	}
	log.Println("[Main] 종료 완료")
}

func getEnv(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

func getEnvInt(key string, fallback int) int {
	if v := os.Getenv(key); v != "" {
		if i, err := strconv.Atoi(v); err == nil {
			return i
		}
	}
	return fallback
}
