// Package auth는 env-orchestrator의 인증 미들웨어를 제공한다.
package auth

import (
	"crypto/subtle"
	"log"
	"net/http"
	"strings"
	"time"

	"github.com/gin-gonic/gin"
)

// Middleware는 플랫폼 API용 인증 미들웨어다. 전달된 JWT 또는 access_token
// 쿠키를 auth-service에서 검증한 뒤에만 X-User-ID 헤더를 설정하고, 쿠키 기반
// 상태 변경 요청에는 CSRF 검증을 적용한다. 외부 요청이 X-User-ID를 직접
// 지정하는 폴백은 허용하지 않는다.
func Middleware(authServiceURL string) gin.HandlerFunc {
	return middleware(authServiceURL, true)
}

// ProxyMiddleware는 사용자가 만든 문제 컨테이너의 HTTP 프록시용 인증
// 미들웨어다. 문제 앱은 Dockerfile과 소스만으로 제공되는 임의의 HTTP 앱이므로
// /claim 같은 POST 요청에 플랫폼의 ctf_csrf_token 헤더를 넣을 수 없다. 프록시
// 자체는 여전히 JWT/access_token을 검증하고 인스턴스 소유권을 확인하지만,
// 플랫폼 API의 CSRF 보호를 컨테이너 내부 앱 요청에 강제하지 않는다. access_token
// 쿠키는 SameSite 정책으로 교차 사이트 POST에 전송되지 않으며, 플랫폼 상태
// 변경 API는 계속 Middleware를 사용한다.
func ProxyMiddleware(authServiceURL string) gin.HandlerFunc {
	return middleware(authServiceURL, false)
}

func middleware(authServiceURL string, requireCSRF bool) gin.HandlerFunc {
	return func(c *gin.Context) {
		// 1) Authorization 헤더 또는 access_token 쿠키가 있으면 auth-service로 검증
		authHeader := c.GetHeader("Authorization")
		accessCookie, _ := c.Cookie("access_token")

		if authHeader != "" || accessCookie != "" {
			req, err := http.NewRequestWithContext(c.Request.Context(), "GET", authServiceURL+"/verify-token", nil)
			if err != nil {
				log.Printf("[Auth] 요청 생성 실패: %v\n", err)
				c.AbortWithStatusJSON(http.StatusInternalServerError, gin.H{"error": "인증 서비스 요청 실패"})
				return
			}
			if authHeader != "" {
				req.Header.Set("Authorization", authHeader)
			}
			if accessCookie != "" {
				req.AddCookie(&http.Cookie{Name: "access_token", Value: accessCookie})
			}

			client := &http.Client{Timeout: 5 * time.Second}
			resp, err := client.Do(req)
			if err != nil {
				log.Printf("[Auth] auth-service 호출 실패: %v\n", err)
				c.AbortWithStatusJSON(http.StatusServiceUnavailable, gin.H{"error": "인증 서비스에 연결할 수 없습니다"})
				return
			}
			defer resp.Body.Close()

			if resp.StatusCode == http.StatusOK {
				userID := resp.Header.Get("X-User-ID")
				if userID != "" {
					c.Request.Header.Set("X-User-ID", userID)
					c.Request.Header.Set("X-User-Role", resp.Header.Get("X-User-Role"))
					c.Request.Header.Set("X-User-Name", resp.Header.Get("X-User-Name"))
					if requireCSRF && !verifyCSRF(c) {
						return
					}
					c.Next()
					return
				}
			}
			// 토큰이 있었지만 검증 실패 → 401 (X-User-ID 폴백 불허)
			c.AbortWithStatusJSON(http.StatusUnauthorized, gin.H{"error": "유효하지 않은 토큰입니다"})
			return
		}

		c.AbortWithStatusJSON(http.StatusUnauthorized, gin.H{"error": "인증이 필요합니다"})
	}
}

// verifyCSRF는 쿠키 기반 상태 변경 요청에 double-submit CSRF 검증을 적용한다.
// Bearer 인증은 브라우저가 교차 출처에서 임의로 붙일 수 없으므로 Auth 서비스와 동일하게
// 예외로 둔다. GET 기반 WebSocket handshake와 프록시 조회는 안전 메서드로 처리한다.
func verifyCSRF(c *gin.Context) bool {
	if c.Request.Method == http.MethodGet || c.Request.Method == http.MethodHead || c.Request.Method == http.MethodOptions {
		return true
	}
	if strings.HasPrefix(c.GetHeader("Authorization"), "Bearer ") {
		return true
	}

	headerToken := c.GetHeader("X-CSRF-Token")
	if headerToken == "" {
		c.AbortWithStatusJSON(http.StatusForbidden, gin.H{"error": "CSRF 검증 실패"})
		return false
	}

	for _, cookie := range c.Request.Cookies() {
		if cookie.Name == "ctf_csrf_token" && subtle.ConstantTimeCompare([]byte(headerToken), []byte(cookie.Value)) == 1 {
			return true
		}
	}

	c.AbortWithStatusJSON(http.StatusForbidden, gin.H{"error": "CSRF 검증 실패"})
	return false
}
