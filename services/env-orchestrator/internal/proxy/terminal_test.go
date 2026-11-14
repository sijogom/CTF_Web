package proxy

import (
	"bytes"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/gorilla/websocket"
)

func TestBridgeTCPPreservesBidirectionalBytes(t *testing.T) {
	proxyConnCh := make(chan *websocket.Conn, 1)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, err := (&websocket.Upgrader{CheckOrigin: func(*http.Request) bool { return true }}).Upgrade(w, r, nil)
		if err != nil {
			t.Errorf("upgrade websocket: %v", err)
			return
		}
		proxyConnCh <- conn
	}))
	defer server.Close()

	browserConn, _, err := websocket.DefaultDialer.Dial("ws"+strings.TrimPrefix(server.URL, "http"), nil)
	if err != nil {
		t.Fatalf("dial websocket: %v", err)
	}
	defer browserConn.Close()

	proxyConn := <-proxyConnCh
	defer proxyConn.Close()
	targetConn, targetPeer := net.Pipe()
	defer targetPeer.Close()

	done := make(chan struct{})
	go func() {
		bridgeTCP(proxyConn, targetConn)
		close(done)
	}()

	input := []byte("whoami\n\x00\xff")
	if err := browserConn.WriteMessage(websocket.BinaryMessage, input); err != nil {
		t.Fatalf("write browser input: %v", err)
	}
	_ = targetPeer.SetReadDeadline(time.Now().Add(time.Second))
	gotInput := make([]byte, len(input))
	if _, err := targetPeer.Read(gotInput); err != nil {
		t.Fatalf("read target input: %v", err)
	}
	if !bytes.Equal(gotInput, input) {
		t.Fatalf("target input mismatch: got %v want %v", gotInput, input)
	}

	output := []byte("\x1b[32mready\x1b[0m\n\x00\xff")
	if _, err := targetPeer.Write(output); err != nil {
		t.Fatalf("write target output: %v", err)
	}
	_ = browserConn.SetReadDeadline(time.Now().Add(time.Second))
	messageType, gotOutput, err := browserConn.ReadMessage()
	if err != nil {
		t.Fatalf("read browser output: %v", err)
	}
	if messageType != websocket.BinaryMessage {
		t.Fatalf("message type = %d, want binary", messageType)
	}
	if !bytes.Equal(gotOutput, output) {
		t.Fatalf("browser output mismatch: got %v want %v", gotOutput, output)
	}

	_ = targetPeer.Close()
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("TCP bridge did not stop after target close")
	}
}
