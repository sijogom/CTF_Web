package proxy

import (
	"fmt"
	"log"
	"net/http"
	"net/url"

	"github.com/gorilla/websocket"
)

// VNCProxy는 noVNC → 컨테이너 VNC 간 WebSocket 프록시이다.
type VNCProxy struct {
	resolveTarget func(instanceID string) (host string, port int, err error)
}

// NewVNCProxy는 새로운 VNC 프록시를 생성한다.
func NewVNCProxy(resolver func(string) (string, int, error)) *VNCProxy {
	return &VNCProxy{resolveTarget: resolver}
}

// HandleWebSocket은 noVNC WebSocket 연결을 처리한다.
// 경로: /ws/vnc/{instance_id}
func (vp *VNCProxy) HandleWebSocket(w http.ResponseWriter, r *http.Request, instanceID string) {
	clientConn, err := upgrader.Upgrade(w, r, nil)
	if err != nil {
		log.Printf("[VNCProxy] 업그레이드 실패: %v\n", err)
		return
	}
	defer clientConn.Close()

	host, port, err := vp.resolveTarget(instanceID)
	if err != nil {
		log.Printf("[VNCProxy] 대상 해석 실패: %v\n", err)
		clientConn.WriteMessage(websocket.TextMessage, []byte(fmt.Sprintf("오류: %v", err)))
		return
	}

	// noVNC는 보통 VNC 포트 + 1 (websockify)
	vncPort := port + 1
	targetURL := url.URL{Scheme: "ws", Host: fmt.Sprintf("%s:%d", host, vncPort), Path: "/websockify"}
	targetConn, _, err := challengeWebSocketDialer.Dial(targetURL.String(), nil)
	if err != nil {
		log.Printf("[VNCProxy] 컨테이너 VNC 연결 실패: %v\n", err)
		clientConn.WriteMessage(websocket.TextMessage, []byte("VNC 세션에 연결할 수 없습니다."))
		return
	}
	defer targetConn.Close()

	// 메시지 크기 제한 — OOM 방지
	clientConn.SetReadLimit(wsMaxMessageSize)
	targetConn.SetReadLimit(wsMaxMessageSize)

	// 양방향 프록시 (바이너리 메시지 — VNC 프로토콜)
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
	log.Printf("[VNCProxy] 세션 종료: %s\n", instanceID)
}
