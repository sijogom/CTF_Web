package auth

import (
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/gin-gonic/gin"
)

func TestProxyMiddlewareAllowsArbitraryChallengePostAfterJWTValidation(t *testing.T) {
	gin.SetMode(gin.TestMode)
	authServer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/verify-token" {
			t.Fatalf("auth path = %q, want /verify-token", r.URL.Path)
		}
		w.Header().Set("X-User-ID", "user-1")
		w.WriteHeader(http.StatusOK)
	}))
	defer authServer.Close()

	router := gin.New()
	router.Use(ProxyMiddleware(authServer.URL))
	router.POST("/env/proxy/:id/*path", func(c *gin.Context) {
		c.Status(http.StatusNoContent)
	})

	req := httptest.NewRequest(http.MethodPost, "/env/proxy/inst_0123456789abcdef0123456789abcdef/claim", nil)
	req.AddCookie(&http.Cookie{Name: "access_token", Value: "signed-token"})
	resp := httptest.NewRecorder()
	router.ServeHTTP(resp, req)

	if resp.Code != http.StatusNoContent {
		t.Fatalf("proxy POST status = %d, want %d", resp.Code, http.StatusNoContent)
	}
}

func TestPlatformMiddlewareKeepsCSRFFailureForStateChangingAPI(t *testing.T) {
	gin.SetMode(gin.TestMode)
	authServer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("X-User-ID", "user-1")
		w.WriteHeader(http.StatusOK)
	}))
	defer authServer.Close()

	router := gin.New()
	router.Use(Middleware(authServer.URL))
	router.POST("/instances/create", func(c *gin.Context) {
		c.Status(http.StatusNoContent)
	})

	req := httptest.NewRequest(http.MethodPost, "/instances/create", nil)
	req.AddCookie(&http.Cookie{Name: "access_token", Value: "signed-token"})
	resp := httptest.NewRecorder()
	router.ServeHTTP(resp, req)

	if resp.Code != http.StatusForbidden {
		t.Fatalf("platform POST status = %d, want %d", resp.Code, http.StatusForbidden)
	}
}

func TestBothMiddlewaresRejectMissingCredentials(t *testing.T) {
	gin.SetMode(gin.TestMode)
	for name, handler := range map[string]gin.HandlerFunc{
		"platform": Middleware("http://unused.invalid"),
		"proxy":    ProxyMiddleware("http://unused.invalid"),
	} {
		t.Run(name, func(t *testing.T) {
			router := gin.New()
			router.Use(handler)
			router.GET("/resource", func(c *gin.Context) { c.Status(http.StatusNoContent) })
		request := httptest.NewRequest(http.MethodGet, "/resource", nil)
		response := httptest.NewRecorder()
		router.ServeHTTP(response, request)
		if response.Code != http.StatusUnauthorized {
			t.Fatalf("missing credentials status = %d, want %d", response.Code, http.StatusUnauthorized)
		}
		})
	}
}
