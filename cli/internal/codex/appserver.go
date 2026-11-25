package codex

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os/exec"
	"strings"
	"sync"
	"syscall"
	"time"
)

// errUnsupported means this Codex predates the call. Only a "method not
// found" (-32601), or the "invalid request: unknown variant" (-32600) that
// codex-cli 0.172 sends for an unknown method, proves absence; any other
// error is a failure.
var errUnsupported = errors.New("codex app-server does not support this call")

const (
	jsonRPCInvalidRequest  = -32600
	jsonRPCMethodNotFound  = -32601
	maxRPCErrorMessageSize = 200
)

// appServer is one `codex app-server` session: newline-delimited JSON-RPC
// over the child's stdin and stdout. The child runs in its own process group
// so a timeout can kill it and anything it started.
type appServer struct {
	ctx    context.Context
	cmd    *exec.Cmd
	stdin  io.WriteCloser
	lines  *bufio.Scanner
	stderr *limitedWriter
	nextID int

	mu     sync.Mutex
	killed bool
}

// startAppServer starts `codex app-server` and runs the initialize
// handshake. ctx bounds the whole session: when it ends, the process group
// is killed, which unblocks any call in progress.
func startAppServer(ctx context.Context, codexPath, clientVersion string) (*appServer, error) {
	cmd := exec.Command(codexPath, "app-server")
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	stdin, err := cmd.StdinPipe()
	if err != nil {
		return nil, err
	}
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return nil, err
	}
	s := &appServer{ctx: ctx, cmd: cmd, stdin: stdin, stderr: &limitedWriter{max: 4096}}
	cmd.Stderr = s.stderr
	if err := cmd.Start(); err != nil {
		return nil, err
	}
	s.lines = bufio.NewScanner(stdout)
	s.lines.Buffer(make([]byte, 64*1024), 16*1024*1024)
	go func() {
		<-ctx.Done()
		s.kill()
	}()

	params := map[string]any{"clientInfo": map[string]any{"name": "paircli", "title": "Pair", "version": clientVersion}}
	if err := s.call("initialize", params, nil); err != nil {
		s.close()
		return nil, err
	}
	if err := s.send(map[string]any{"method": "initialized"}); err != nil {
		s.close()
		return nil, err
	}
	return s, nil
}

type rpcError struct {
	Code    int    `json:"code"`
	Message string `json:"message"`
}

// call sends one request and decodes the matching response's result into
// result (nil discards it). Notifications and other ids are skipped.
func (s *appServer) call(method string, params, result any) error {
	s.nextID++
	id := s.nextID
	msg := map[string]any{"id": id, "method": method}
	if params != nil {
		msg["params"] = params
	}
	if err := s.send(msg); err != nil {
		return s.exitError(method, err)
	}
	for s.lines.Scan() {
		var resp struct {
			ID     *int            `json:"id"`
			Method string          `json:"method"`
			Result json.RawMessage `json:"result"`
			Error  *rpcError       `json:"error"`
		}
		// Skip notifications and requests the server sends us.
		if json.Unmarshal(s.lines.Bytes(), &resp) != nil || resp.Method != "" || resp.ID == nil || *resp.ID != id {
			continue
		}
		if resp.Error != nil {
			if resp.Error.Code == jsonRPCMethodNotFound ||
				(resp.Error.Code == jsonRPCInvalidRequest && strings.Contains(resp.Error.Message, "unknown variant")) {
				return fmt.Errorf("%s: %w", method, errUnsupported)
			}
			msg := resp.Error.Message
			if len(msg) > maxRPCErrorMessageSize {
				msg = strings.ToValidUTF8(msg[:maxRPCErrorMessageSize], "") + "…"
			}
			return fmt.Errorf("codex app-server %s: %s", method, msg)
		}
		if result == nil {
			return nil
		}
		if err := json.Unmarshal(resp.Result, result); err != nil {
			return fmt.Errorf("codex app-server %s: decode result: %w", method, err)
		}
		return nil
	}
	return s.exitError(method, s.lines.Err())
}

func (s *appServer) send(msg map[string]any) error {
	data, err := json.Marshal(msg)
	if err != nil {
		return err
	}
	_, err = s.stdin.Write(append(data, '\n'))
	return err
}

// exitError explains a session that ended before answering.
func (s *appServer) exitError(method string, err error) error {
	detail := firstLine(s.stderr.trimmed(), maxRPCErrorMessageSize)
	switch {
	case len(detail) > 0:
		return fmt.Errorf("codex app-server exited during %s: %s", method, detail)
	case err != nil:
		return fmt.Errorf("codex app-server exited during %s: %w", method, err)
	}
	return fmt.Errorf("codex app-server exited during %s", method)
}

// close ends the session: stdin EOF asks app-server to exit, and the process
// group is killed if it has not exited within a short grace, or at once when
// the session's time limit has passed.
func (s *appServer) close() {
	_ = s.stdin.Close()
	done := make(chan struct{})
	go func() {
		_ = s.cmd.Wait()
		// Reaped: its pid (and group id) may be reused, so never signal it.
		s.mu.Lock()
		s.killed = true
		s.mu.Unlock()
		close(done)
	}()
	select {
	case <-done:
	case <-s.ctx.Done():
		s.kill()
		<-done
	case <-time.After(2 * time.Second):
		s.kill()
		<-done
	}
}

func (s *appServer) kill() {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.killed || s.cmd.Process == nil {
		return
	}
	s.killed = true
	_ = syscall.Kill(-s.cmd.Process.Pid, syscall.SIGKILL)
}

// limitedWriter keeps the first max bytes written to it.
type limitedWriter struct {
	buf bytes.Buffer
	max int
	mu  sync.Mutex
}

func (w *limitedWriter) trimmed() []byte {
	w.mu.Lock()
	defer w.mu.Unlock()
	return bytes.TrimSpace(append([]byte(nil), w.buf.Bytes()...))
}

func (w *limitedWriter) Write(p []byte) (int, error) {
	w.mu.Lock()
	defer w.mu.Unlock()
	if room := w.max - w.buf.Len(); room > 0 {
		if len(p) > room {
			w.buf.Write(p[:room])
		} else {
			w.buf.Write(p)
		}
	}
	return len(p), nil
}

// firstLine is the first non-empty line of b, cut to max bytes, so an
// outcome stays one readable line in setup.json.
func firstLine(b []byte, max int) string {
	for _, line := range strings.Split(string(b), "\n") {
		line = strings.TrimSpace(line)
		if line == "" {
			continue
		}
		if len(line) > max {
			line = strings.ToValidUTF8(line[:max], "") + "…"
		}
		return line
	}
	return ""
}
