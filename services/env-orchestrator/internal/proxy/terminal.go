// Package proxy provides terminal WebSocket proxying for challenge instances.
package proxy

import (
	"log"
	"net"
	"net/http"
	"net/url"
	"os"
	"strconv"
	"time"

	"github.com/gorilla/websocket"
)

const (
	// wsMaxMessageSize prevents an untrusted challenge or browser from
	// allocating an unbounded message buffer in the proxy.
	wsMaxMessageSize       = 1 << 20
	terminalTCPDialTimeout = 10 * time.Second
	terminalTCPReadBuffer  = 32 * 1024
)

// The challenge network is private. Never route a challenge WebSocket through
// HTTP(S)_PROXY inherited from the environment.
var challengeWebSocketDialer = websocket.Dialer{
	Proxy:            nil,
	HandshakeTimeout: 10 * time.Second,
}

var upgrader = websocket.Upgrader{
	CheckOrigin: func(r *http.Request) bool {
		origin := r.Header.Get("Origin")
		if origin == "" {
			return false
		}
		frontendPort := os.Getenv("FRONTEND_PORT")
		if frontendPort == "" {
			frontendPort = "3100"
		}
		allowedOrigins := []string{
			"http://localhost:" + frontendPort,
			"http://localhost",
			"https://localhost",
		}
		if domain := os.Getenv("SITE_DOMAIN"); domain != "" {
			allowedOrigins = append(allowedOrigins, "https://"+domain)
		}
		for _, allowed := range allowedOrigins {
			if origin == allowed {
				return true
			}
		}
		log.Printf("[WebSocket] rejected origin: %s", origin)
		return false
	},
}

// TerminalProxy adapts a challenge terminal to the browser WebSocket.
type TerminalProxy struct {
	resolveTarget func(instanceID string) (host string, port int, err error)
}

func NewTerminalProxy(resolver func(string) (string, int, error)) *TerminalProxy {
	return &TerminalProxy{resolveTarget: resolver}
}

// HandleWebSocket handles /ws/terminal/{instance_id}. A challenge may expose
// either a WebSocket endpoint or a raw TCP/ANSI service on its declared port;
// both are supported without requiring a challenge-specific image exception.
func (tp *TerminalProxy) HandleWebSocket(w http.ResponseWriter, r *http.Request, instanceID string) {
	clientConn, err := upgrader.Upgrade(w, r, nil)
	if err != nil {
		log.Printf("[TerminalProxy] websocket upgrade failed: %v", err)
		return
	}
	defer clientConn.Close()

	host, port, err := tp.resolveTarget(instanceID)
	if err != nil {
		log.Printf("[TerminalProxy] target resolution failed: %v", err)
		_ = clientConn.WriteMessage(websocket.TextMessage, []byte("컨테이너에 연결할 수 없습니다."))
		return
	}

	targetURL := url.URL{Scheme: "ws", Host: net.JoinHostPort(host, strconv.Itoa(port)), Path: "/"}
	targetConn, _, websocketErr := challengeWebSocketDialer.Dial(targetURL.String(), nil)
	if websocketErr == nil {
		defer targetConn.Close()
		bridgeWebSocket(clientConn, targetConn)
		log.Printf("[TerminalProxy] websocket session ended: %s", instanceID)
		return
	}

	// HTTP and raw TCP challenge services commonly reject a WebSocket
	// handshake. Retry the same port as a byte stream before reporting a
	// connection failure. This is what makes Dockerfile+flag.txt TCP fixtures
	// usable instead of returning the generic 502/connection message.
	tcpConn, tcpErr := net.DialTimeout("tcp", net.JoinHostPort(host, strconv.Itoa(port)), terminalTCPDialTimeout)
	if tcpErr != nil {
		log.Printf("[TerminalProxy] target websocket/tcp connection failed: ws=%v tcp=%v", websocketErr, tcpErr)
		_ = clientConn.WriteMessage(websocket.TextMessage, []byte("컨테이너에 연결할 수 없습니다."))
		return
	}
	defer tcpConn.Close()
	bridgeTCP(clientConn, tcpConn)
	log.Printf("[TerminalProxy] tcp session ended: %s", instanceID)
}

func bridgeWebSocket(clientConn, targetConn *websocket.Conn) {
	clientConn.SetReadLimit(wsMaxMessageSize)
	targetConn.SetReadLimit(wsMaxMessageSize)
	errCh := make(chan error, 2)

	go func() {
		for {
			msgType, msg, err := clientConn.ReadMessage()
			if err != nil {
				errCh <- err
				return
			}
			if err := targetConn.WriteMessage(msgType, msg); err != nil {
				errCh <- err
				return
			}
		}
	}()

	go func() {
		for {
			msgType, msg, err := targetConn.ReadMessage()
			if err != nil {
				errCh <- err
				return
			}
			if err := clientConn.WriteMessage(msgType, msg); err != nil {
				errCh <- err
				return
			}
		}
	}()

	<-errCh
	_ = clientConn.Close()
	_ = targetConn.Close()
}

// bridgeTCP preserves the challenge byte stream in binary WebSocket frames.
// The browser terminal decodes these frames as UTF-8 with replacement for
// display while user input is forwarded byte-for-byte.
func bridgeTCP(clientConn *websocket.Conn, targetConn net.Conn) {
	clientConn.SetReadLimit(wsMaxMessageSize)
	tcpErrCh := make(chan error, 2)

	go bridgeTCPClientToTarget(clientConn, targetConn, tcpErrCh)
	go bridgeTCPTargetToClient(clientConn, targetConn, tcpErrCh)

	<-tcpErrCh
	_ = clientConn.Close()
	_ = targetConn.Close()
}

func bridgeTCPClientToTarget(clientConn *websocket.Conn, targetConn net.Conn, errCh chan<- error) {
	for {
		msgType, msg, err := clientConn.ReadMessage()
		if err != nil {
			errCh <- err
			return
		}
		if msgType != websocket.TextMessage && msgType != websocket.BinaryMessage {
			continue
		}
		if _, err := targetConn.Write(msg); err != nil {
			errCh <- err
			return
		}
	}
}

func bridgeTCPTargetToClient(clientConn *websocket.Conn, targetConn net.Conn, errCh chan<- error) {
	buf := make([]byte, terminalTCPReadBuffer)
	for {
		n, err := targetConn.Read(buf)
		if n > 0 {
			payload := append([]byte(nil), buf[:n]...)
			if writeErr := clientConn.WriteMessage(websocket.BinaryMessage, payload); writeErr != nil {
				errCh <- writeErr
				return
			}
		}
		if err != nil {
			errCh <- err
			return
		}
	}
}
