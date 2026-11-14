package proxy

import (
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

type unavailableResolverError struct{}

func (unavailableResolverError) Error() string { return "environment unavailable" }
func (unavailableResolverError) ServiceUnavailable() bool {
	return true
}

func TestChallengeTransportBypassesEnvironmentProxy(t *testing.T) {
	transport, ok := newChallengeTransport().(*http.Transport)
	if !ok {
		t.Fatal("challenge transport must be an http.Transport")
	}
	if transport.Proxy != nil {
		t.Fatal("challenge transport must not use environment proxy settings")
	}
}

func TestHandleProxyReturnsServiceUnavailableForKnownUnavailableEnvironment(t *testing.T) {
	proxy := NewHTTPProxy(func(string) (string, int, error) {
		return "", 0, unavailableResolverError{}
	})
	request := httptest.NewRequest(http.MethodGet, "/env/proxy/inst_123/", nil)
	response := httptest.NewRecorder()

	proxy.HandleProxy(response, request, "inst_123", "")

	if response.Code != http.StatusServiceUnavailable {
		t.Fatalf("status = %d, want %d", response.Code, http.StatusServiceUnavailable)
	}
	var body map[string]string
	if err := json.NewDecoder(response.Body).Decode(&body); err != nil {
		t.Fatalf("response JSON decode: %v", err)
	}
	if body["code"] != "environment_unavailable" {
		t.Fatalf("code = %q, want environment_unavailable", body["code"])
	}
}

func TestHandleProxyReturnsNotFoundForMissingInstance(t *testing.T) {
	missing := errors.New("instance missing")
	proxy := NewHTTPProxy(func(string) (string, int, error) {
		return "", 0, missing
	})
	request := httptest.NewRequest(http.MethodGet, "/env/proxy/inst_123/", nil)
	response := httptest.NewRecorder()

	proxy.HandleProxy(response, request, "inst_123", "")

	if response.Code != http.StatusNotFound {
		t.Fatalf("status = %d, want %d", response.Code, http.StatusNotFound)
	}
}

func TestPrepareChallengeRequestStripsPlatformCredentialsAndKeepsNamespacedSession(t *testing.T) {
	instanceID := "inst_123"
	request := httptest.NewRequest(http.MethodGet, "/env/proxy/inst_123/", nil)
	request.Header.Set("Authorization", "Bearer platform-token")
	request.Header.Set("X-User-ID", "platform-user")
	request.Header.Set("X-Forwarded-For", "127.0.0.1")
	request.Header.Set("Cookie", "access_token=platform; ctfenv_inst_123_session=challenge")

	prepareChallengeRequest(request, instanceID)

	if request.Header.Get("Authorization") != "" || request.Header.Get("X-User-ID") != "" {
		t.Fatal("platform authentication headers must not reach challenge containers")
	}
	if request.Header.Get("Cookie") != "session=challenge" {
		t.Fatalf("Cookie = %q, want namespaced challenge session only", request.Header.Get("Cookie"))
	}
	if forwarded, exists := request.Header["X-Forwarded-For"]; !exists || forwarded != nil {
		t.Fatal("X-Forwarded-For must be explicitly suppressed for challenge containers")
	}
}

func TestNamespaceChallengeResponseCookiesScopesCookieToInstance(t *testing.T) {
	instanceID := "inst_123"
	prefix := "/ctf/env/proxy/" + instanceID
	response := &http.Response{Header: make(http.Header)}
	response.Header.Add("Set-Cookie", "session=challenge; Path=/; HttpOnly")

	namespaceChallengeResponseCookies(response, instanceID, prefix)

	setCookie := response.Header.Get("Set-Cookie")
	if !strings.Contains(setCookie, "ctfenv_inst_123_session=challenge") {
		t.Fatalf("Set-Cookie = %q, want namespaced cookie", setCookie)
	}
	if !strings.Contains(setCookie, "Path=/ctf/env/proxy/inst_123/") {
		t.Fatalf("Set-Cookie = %q, want instance path", setCookie)
	}
}
