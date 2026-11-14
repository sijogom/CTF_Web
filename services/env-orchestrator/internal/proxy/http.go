// Package proxy는 챌린지 컨테이너로의 HTTP/WebSocket 프록시를 제공한다.
package proxy

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"net/http/httputil"
	"net/url"
	"os"
	"regexp"
	"strconv"
	"strings"
	"time"
)

// basePath는 서브패스 배포 시 프록시 URL prefix에 추가된다 (예: "/ctf").
var basePath = os.Getenv("BASE_PATH")

// maxHTMLRewriteSize는 HTML 응답 rewrite 최대 크기 (10MB).
// 초과 시 rewrite를 건너뛰고 원본 응답을 그대로 전달한다.
const maxHTMLRewriteSize = 10 << 20

const environmentCookieNamePrefix = "ctfenv_"

// absPathRe는 HTML 속성의 절대 경로를 매칭한다.
// action, href, src, srcset, data, poster, formaction 대상.
// "/X" (X가 /가 아닌 문자)만 매칭 → "//" (protocol-relative URL)은 자동 제외.
var absPathRe = regexp.MustCompile(`((?:action|href|src|srcset|data|poster|formaction)\s*=\s*["'])/([^/])`)

// srcsetCommaRe는 srcset 내 콤마 구분 절대 경로를 매칭한다.
// 예: "..., /img2.jpg 2x" → "..., /env/proxy/{id}/img2.jpg 2x"
var srcsetCommaRe = regexp.MustCompile(`(,\s*)/([^/\s"'>])`)

// jsFetchRe는 JavaScript의 fetch/XMLHttpRequest 절대 경로를 매칭한다.
// fetch("/api/...") 또는 fetch('/api/...') 패턴 대상.
var jsFetchRe = regexp.MustCompile(`(fetch\s*\(\s*["'])/([^/"'])`)

// jsURLRe는 JS 내 URL 할당/비교에서 절대 경로를 매칭한다.
// url = "/path", url: "/path", open("GET","/path" 등
var jsURLRe = regexp.MustCompile(`((?:url|URL|href|src|action|endpoint|path)\s*[:=]\s*["'])/([^/"'])`)

// scriptInlineRe는 <script> 태그 내부의 절대 경로를 매칭한다 (인라인 스크립트).
// HTML 내에서 JavaScript fetch 호출을 감지.
var scriptInlineRe = regexp.MustCompile(`((?:fetch|XMLHttpRequest|\.open)\s*\(\s*["'])/([^/"'])`)

// headTagRe는 <head> 태그를 대소문자 무관 + 속성 포함으로 매칭한다.
	var headTagRe = regexp.MustCompile(`(?i)<head(\s[^>]*)?>`)

// jsSafeReplacer는 prefix를 JS 문자열 리터럴에 안전하게 삽입하기 위한 이스케이퍼.
var jsSafeReplacer = strings.NewReplacer(
	`\`, `\\`, `"`, `\"`, `'`, `\'`,
	`<`, `\x3c`, `>`, `\x3e`,
	"\n", `\n`, "\r", `\r`,
)

// HTTPProxy는 인스턴스 ID 기반으로 컨테이너 HTTP 서비스에 대한 리버스 프록시이다.
// DreamHack 방식 — URL만 알면 인증 없이 접근 가능.
type HTTPProxy struct {
	resolveTarget func(instanceID string) (host string, port int, err error)
	transport     http.RoundTripper
}

// NewHTTPProxy는 새로운 HTTP 프록시를 생성한다.
func NewHTTPProxy(resolver func(string) (string, int, error)) *HTTPProxy {
	return &HTTPProxy{
		resolveTarget: resolver,
		transport:     newChallengeTransport(),
	}
}

// newChallengeTransport는 챌린지 전용 Docker 네트워크 대상을 환경 HTTP 프록시로
// 우회시키지 않는다. 사설 컨테이너 IP는 Env-orchestrator에서 직접 연결해야 한다.
func newChallengeTransport() http.RoundTripper {
	baseTransport, ok := http.DefaultTransport.(*http.Transport)
	if !ok {
		return &http.Transport{Proxy: nil}
	}
	base := baseTransport.Clone()
	base.Proxy = nil
	return base
}

func challengeCookiePrefix(instanceID string) string {
	return environmentCookieNamePrefix + instanceID + "_"
}

// prepareChallengeRequest removes platform credentials before a request reaches
// an untrusted challenge container. Challenge cookies are namespaced per instance
// and unwrapped only on this internal hop, so normal challenge sessions still work.
func prepareChallengeRequest(req *http.Request, instanceID string) {
	req.Header.Del("Authorization")
	req.Header.Del("Proxy-Authorization")
	req.Header.Del("X-User-ID")
	req.Header.Del("X-User-Role")
	req.Header.Del("X-User-Name")
	req.Header.Del("X-CSRF-Token")
	req.Header.Del("X-Real-IP")
	req.Header.Del("X-Forwarded-Host")
	req.Header.Del("X-Forwarded-Proto")
	// A nil value tells httputil.ReverseProxy not to append the client IP later.
	req.Header["X-Forwarded-For"] = nil

	prefix := challengeCookiePrefix(instanceID)
	challengeCookies := make([]string, 0)
	for _, cookie := range req.Cookies() {
		if !strings.HasPrefix(cookie.Name, prefix) {
			continue
		}
		originalName := strings.TrimPrefix(cookie.Name, prefix)
		if originalName == "" {
			continue
		}
		challengeCookie := (&http.Cookie{Name: originalName, Value: cookie.Value}).String()
		if challengeCookie != "" {
			challengeCookies = append(challengeCookies, challengeCookie)
		}
	}
	if len(challengeCookies) == 0 {
		req.Header.Del("Cookie")
		return
	}
	req.Header.Set("Cookie", strings.Join(challengeCookies, "; "))
}

// namespaceChallengeResponseCookies confines a challenge's cookies to its own
// proxy path and prevents it from overwriting platform cookies at the site root.
func namespaceChallengeResponseCookies(resp *http.Response, instanceID, pathPrefix string) {
	if len(resp.Header.Values("Set-Cookie")) == 0 {
		return
	}
	cookies := resp.Cookies()
	resp.Header.Del("Set-Cookie")
	prefix := challengeCookiePrefix(instanceID)
	for _, cookie := range cookies {
		if cookie.Name == "" {
			continue
		}
		cookie.Name = prefix + cookie.Name
		cookie.Path = pathPrefix + "/"
		cookie.Domain = ""
		resp.Header.Add("Set-Cookie", cookie.String())
	}
}

// retryTransport는 컨테이너 cold start 시 연결 실패를 자동 재시도하는 Transport이다.
// 안전한 메서드(GET, HEAD, OPTIONS)만 재시도하여 부작용 방지.
type retryTransport struct {
	base       http.RoundTripper
	maxRetries int
	delay      time.Duration
}

func (t *retryTransport) RoundTrip(req *http.Request) (*http.Response, error) {
	canRetry := req.Method == http.MethodGet ||
		req.Method == http.MethodHead ||
		req.Method == http.MethodOptions

	maxAttempts := 1
	if canRetry {
		maxAttempts = t.maxRetries + 1
	}

	var resp *http.Response
	var err error
	for i := 0; i < maxAttempts; i++ {
		resp, err = t.base.RoundTrip(req)
		if err == nil {
			return resp, nil
		}
		// 재시도 전 이전 응답 body 닫기 (리소스 누수 방지)
		if resp != nil && resp.Body != nil {
			resp.Body.Close()
		}
		if !isConnectionError(err) {
			return resp, err
		}
		if i < maxAttempts-1 {
			log.Printf("[HTTPProxy] 연결 재시도 %d/%d (대기: %v)\n", i+1, t.maxRetries, t.delay)
			time.Sleep(t.delay)
		}
	}
	return resp, err
}

// isConnectionError는 네트워크 연결 오류 (refused, reset, timeout)인지 판별한다.
func isConnectionError(err error) bool {
	if err == nil {
		return false
	}
	var opErr *net.OpError
	if errors.As(err, &opErr) {
		return true
	}
	errStr := err.Error()
	return strings.Contains(errStr, "connection refused") ||
		strings.Contains(errStr, "connection reset") ||
		strings.Contains(errStr, "i/o timeout") ||
		strings.Contains(errStr, "EOF")
}

// HandleProxy는 인스턴스 ID에 해당하는 컨테이너로 HTTP 요청을 전달한다.
// 경로: /env/proxy/:id/*path
// HTML 응답의 절대 경로를 프록시 접두사로 변환하여 컨테이너 내부 링크가 정상 작동하도록 한다.
func (hp *HTTPProxy) HandleProxy(w http.ResponseWriter, r *http.Request, instanceID, proxyPath string) {
	host, port, err := hp.resolveTarget(instanceID)
	if err != nil {
		status := http.StatusNotFound
		code := "instance_not_found"
		var unavailable interface{ ServiceUnavailable() bool }
		if errors.As(err, &unavailable) && unavailable.ServiceUnavailable() {
			status = http.StatusServiceUnavailable
			code = "environment_unavailable"
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(status)
		json.NewEncoder(w).Encode(map[string]string{"error": err.Error(), "code": code})
		return
	}

	target := &url.URL{
		Scheme: "http",
		Host:   fmt.Sprintf("%s:%d", host, port),
	}

	prefix := basePath + "/env/proxy/" + instanceID

	proxy := &httputil.ReverseProxy{
		Director: func(req *http.Request) {
			req.URL.Scheme = target.Scheme
			req.URL.Host = target.Host
			req.URL.Path = "/" + proxyPath
			req.URL.RawQuery = r.URL.RawQuery
			req.Host = target.Host
			prepareChallengeRequest(req, instanceID)
			// HTML 응답 rewrite를 위해 압축 비활성화
			req.Header.Del("Accept-Encoding")
		},
		ModifyResponse: func(resp *http.Response) error {
			namespaceChallengeResponseCookies(resp, instanceID, prefix)
			// Location 헤더 rewrite (리다이렉트 경로 보정)
			if loc := resp.Header.Get("Location"); loc != "" &&
				strings.HasPrefix(loc, "/") && !strings.HasPrefix(loc, "//") {
				resp.Header.Set("Location", prefix+loc)
			}

			// 챌린지 컨테이너의 CSP가 인라인 스크립트를 차단하지 않도록 제거
			resp.Header.Del("Content-Security-Policy")
			resp.Header.Del("Content-Security-Policy-Report-Only")

			ct := resp.Header.Get("Content-Type")
			isHTML := strings.Contains(ct, "text/html")
			isJS := strings.Contains(ct, "javascript") || strings.Contains(ct, "ecmascript")

			// HTML 또는 JavaScript 응답만 body rewrite
			if !isHTML && !isJS {
				return nil
			}

			// OOM 방지: 최대 크기 제한 (초과 시 rewrite 건너뜀)
			limited := io.LimitReader(resp.Body, maxHTMLRewriteSize+1)
			body, err := io.ReadAll(limited)
			if err != nil {
				resp.Body.Close()
				return err
			}
			if len(body) > maxHTMLRewriteSize {
				// 10MB 초과 → 읽은 부분 + 나머지를 이어붙여 원본 전달
				origBody := resp.Body
				resp.Body = &bodyWithCloser{
					Reader: io.MultiReader(bytes.NewReader(body), origBody),
					close:  origBody.Close,
				}
				resp.Header.Del("Content-Length") // 크기 불확실 → chunked 전환
				resp.ContentLength = -1
				return nil
			}
			resp.Body.Close()

			var modified []byte

			if isHTML {
				// 1. HTML 속성의 절대 경로를 프록시 접두사로 변환
				modified = absPathRe.ReplaceAll(body, []byte("${1}"+prefix+"/${2}"))
				modified = srcsetCommaRe.ReplaceAll(modified, []byte("${1}"+prefix+"/${2}"))
				modified = scriptInlineRe.ReplaceAll(modified, []byte("${1}"+prefix+"/${2}"))

				// 2. fetch/XMLHttpRequest monkey-patch 주입
				// 난독화된 JS에서 런타임에 "/api/..." 등 절대 경로를 사용하는 경우,
				// 정적 regex로는 잡을 수 없으므로 런타임 인터셉트가 필요.
				// IMPORTANT: regex rewrite 이후에 주입해야 함 (스크립트 내 fetch 참조가 rewrite되지 않도록)
				safePrefix := jsSafeReplacer.Replace(prefix)
				// R(u): 절대 경로에 프록시 접두사 추가 (이미 접두사가 있으면 스킵)
				patchScript := `<script>(function(){` +
					`var P="` + safePrefix + `";` +
					`function R(u){return typeof u==="string"&&u.charAt(0)==="/"&&u.charAt(1)!=="/"&&u.indexOf(P)!==0?P+u:u;}` +
					// fetch monkey-patch
					`var oF=window.fetch;` +
					`window.fetch=function(u,o){return oF.call(this,R(u),o);};` +
					// XMLHttpRequest monkey-patch
					`var oX=XMLHttpRequest.prototype.open;` +
					`XMLHttpRequest.prototype.open=function(m,u){` +
					`return oX.apply(this,[m,R(u)].concat([].slice.call(arguments,2)));` +
					`};` +
					// window.location 리다이렉트 대응
					`var oAssign=window.location.assign.bind(window.location);` +
					`var oReplace=window.location.replace.bind(window.location);` +
					`window.location.assign=function(u){return oAssign(R(u));};` +
					`window.location.replace=function(u){return oReplace(R(u));};` +
					// History API 대응
					`var oPush=history.pushState.bind(history);` +
					`var oRep=history.replaceState.bind(history);` +
					`history.pushState=function(s,t,u){return oPush(s,t,R(u));};` +
					`history.replaceState=function(s,t,u){return oRep(s,t,R(u));};` +
					`})()</script>` +
					// <base href> 태그 주입 — 상대 경로를 프록시 접두사 기준으로 해석
					`<base href="` + safePrefix + `/">`
				// <head> 태그 바로 뒤에 주입 (대소문자/속성 무관)
				loc := headTagRe.FindIndex(modified)
				if loc != nil {
					insertPos := loc[1]
					patched := make([]byte, 0, len(modified)+len(patchScript))
					patched = append(patched, modified[:insertPos]...)
					patched = append(patched, []byte(patchScript)...)
					patched = append(patched, modified[insertPos:]...)
					modified = patched
				}
			} else {
				// JavaScript 파일: fetch/URL 절대 경로 보정
				modified = jsFetchRe.ReplaceAll(body, []byte("${1}"+prefix+"/${2}"))
				modified = jsURLRe.ReplaceAll(modified, []byte("${1}"+prefix+"/${2}"))
			}

			resp.Body = io.NopCloser(bytes.NewReader(modified))
			resp.ContentLength = int64(len(modified))
			resp.Header.Set("Content-Length", strconv.Itoa(len(modified)))
			return nil
		},
		Transport: &retryTransport{
			base:       hp.transport,
			maxRetries: 5,           // 최대 5회 재시도
			delay:      time.Second, // 재시도 간격 1초
		},
		// WebSocket 업그레이드도 httputil.ReverseProxy가 자동 처리
		FlushInterval: -1,
		ErrorHandler: func(w http.ResponseWriter, r *http.Request, err error) {
			log.Printf("[HTTPProxy] 프록시 오류 (%s): %v\n", instanceID, err)
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusBadGateway)
			json.NewEncoder(w).Encode(map[string]string{
				"error": "실습 환경에 연결할 수 없습니다. 잠시 후 다시 시도하세요.",
				"code":  "environment_connection_failed",
			})
		},
	}

	proxy.ServeHTTP(w, r)
}

// bodyWithCloser는 io.Reader를 래핑하여 Close 시 원본 body를 닫는 io.ReadCloser이다.
type bodyWithCloser struct {
	io.Reader
	close func() error
}

func (b *bodyWithCloser) Close() error { return b.close() }
