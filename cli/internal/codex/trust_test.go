package codex

import (
	"bufio"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// The test binary doubles as a fake `codex app-server` when this variable
// names its state file (see fakeAppServer).
const fakeStateEnv = "PAIRCLI_FAKE_CODEX_STATE"

func TestMain(m *testing.M) {
	if path := os.Getenv(fakeStateEnv); path != "" && len(os.Args) > 1 && os.Args[1] == "app-server" {
		fakeAppServer(path)
		os.Exit(0)
	}
	os.Exit(m.Run())
}

// fakeState is the fake app-server's world, persisted between sessions so
// that one session's grant is visible to the next.
type fakeState struct {
	Hooks        []map[string]any `json:"hooks"`
	Writes       []any            `json:"writes"`
	Hang         bool             `json:"hang"`
	IgnoreWrites bool             `json:"ignore_writes"`
	// Unknown names methods this fake answers the way codex-cli 0.172
	// answers a method it does not have.
	Unknown []string `json:"unknown"`
}

// fakeAppServer speaks the JSON-RPC subset GrantTrust uses: initialize,
// hooks/list and config/batchWrite.
func fakeAppServer(path string) {
	load := func() fakeState {
		var st fakeState
		data, _ := os.ReadFile(path)
		_ = json.Unmarshal(data, &st)
		return st
	}
	save := func(st fakeState) {
		data, _ := json.Marshal(st)
		_ = os.WriteFile(path, data, 0o600)
	}
	in := bufio.NewScanner(os.Stdin)
	in.Buffer(make([]byte, 1<<20), 1<<24)
	for in.Scan() {
		var req struct {
			ID     *int            `json:"id"`
			Method string          `json:"method"`
			Params json.RawMessage `json:"params"`
		}
		if json.Unmarshal(in.Bytes(), &req) != nil || req.ID == nil {
			continue
		}
		st := load()
		if st.Hang && req.Method != "initialize" {
			time.Sleep(time.Hour)
		}
		unknown := false
		for _, m := range st.Unknown {
			unknown = unknown || m == req.Method
		}
		if unknown {
			msg := "Invalid request: unknown variant `" + req.Method + "`, expected one of `initialize`, " + strings.Repeat("`x`, ", 500)
			out, _ := json.Marshal(map[string]any{"id": *req.ID, "error": map[string]any{"code": -32600, "message": msg}})
			fmt.Println(string(out))
			continue
		}
		// A server-to-client request reusing the id must not read as the answer.
		fmt.Printf(`{"id":%d,"method":"item/tool/requestUserInput","params":{}}`+"\n", *req.ID)
		var result any = map[string]any{}
		switch req.Method {
		case "hooks/list":
			result = map[string]any{"data": []any{map[string]any{"cwd": "/", "hooks": st.Hooks, "warnings": []any{}, "errors": []any{}}}}
		case "config/batchWrite":
			var p struct {
				Edits []struct {
					KeyPath string                     `json:"keyPath"`
					Value   map[string]json.RawMessage `json:"value"`
				} `json:"edits"`
			}
			_ = json.Unmarshal(req.Params, &p)
			st.Writes = append(st.Writes, json.RawMessage(req.Params))
			if !st.IgnoreWrites {
				for _, e := range p.Edits {
					if e.KeyPath == "hooks.state" {
						for key := range e.Value {
							setHook(st.Hooks, key, "trustStatus", "trusted")
						}
					} else if strings.HasPrefix(e.KeyPath, "hooks.state.") {
						key := strings.Trim(strings.TrimPrefix(e.KeyPath, "hooks.state."), `"`)
						setHook(st.Hooks, key, "trustStatus", "untrusted")
					}
				}
			}
			save(st)
			result = map[string]any{"status": "ok"}
		}
		out, _ := json.Marshal(map[string]any{"id": *req.ID, "result": result})
		fmt.Println(string(out))
	}
}

func setHook(hooks []map[string]any, key, field string, value any) {
	for _, h := range hooks {
		if h["key"] == key {
			h[field] = value
		}
	}
}

// trustFixture installs Pair's hooks in a temp CODEX_HOME and returns a fake
// app-server state that lists them the way Codex 0.172 does, all untrusted.
type trustFixture struct {
	t     *testing.T
	home  string
	state string
}

func newTrustFixture(t *testing.T) *trustFixture {
	t.Helper()
	home := t.TempDir()
	codexHomeIn(t, filepath.Join(home, ".codex"))
	if err := InstallHooks(); err != nil {
		t.Fatal(err)
	}
	f := &trustFixture{t: t, home: home, state: filepath.Join(t.TempDir(), "fake.json")}
	t.Setenv(fakeStateEnv, f.state)

	var doc struct {
		Hooks map[string][]struct {
			Matcher *string `json:"matcher"`
			Hooks   []struct {
				Command string `json:"command"`
				Timeout int64  `json:"timeout"`
			} `json:"hooks"`
		} `json:"hooks"`
	}
	data, err := os.ReadFile(HooksPath())
	if err != nil || json.Unmarshal(data, &doc) != nil {
		t.Fatalf("read hooks.json: %v", err)
	}
	var hooks []map[string]any
	for event, groups := range doc.Hooks {
		label := eventSnakeCase[event]
		for g, group := range groups {
			for i, h := range group.Hooks {
				sum := sha256.Sum256([]byte(fmt.Sprint(label, h.Command, h.Timeout, group.Matcher)))
				hooks = append(hooks, map[string]any{
					"key": fmt.Sprintf("%s:%s:%d:%d", HooksPath(), label, g, i), "eventName": label,
					"handlerType": "command", "command": h.Command, "matcher": group.Matcher,
					"timeoutSec": codexTimeout(label, h.Timeout), "sourcePath": HooksPath(), "source": "user",
					"pluginId": nil, "isManaged": false, "enabled": true,
					"currentHash": "sha256:" + hex.EncodeToString(sum[:]), "trustStatus": "untrusted",
				})
			}
		}
	}
	f.save(fakeState{Hooks: hooks})
	return f
}

func (f *trustFixture) load() fakeState {
	f.t.Helper()
	var st fakeState
	data, err := os.ReadFile(f.state)
	if err != nil || json.Unmarshal(data, &st) != nil {
		f.t.Fatalf("read fake state: %v", err)
	}
	return st
}

func (f *trustFixture) save(st fakeState) {
	f.t.Helper()
	data, _ := json.Marshal(st)
	if err := os.WriteFile(f.state, data, 0o600); err != nil {
		f.t.Fatal(err)
	}
}

// edit changes the listed hook for one event label.
func (f *trustFixture) edit(label string, change func(h map[string]any)) {
	f.t.Helper()
	st := f.load()
	for _, h := range st.Hooks {
		if h["eventName"] == label {
			change(h)
		}
	}
	f.save(st)
}

func (f *trustFixture) grant(explicit bool, record TrustRecord) GrantResult {
	f.t.Helper()
	exe, err := os.Executable()
	if err != nil {
		f.t.Fatal(err)
	}
	return GrantTrust(context.Background(), GrantOptions{Codex: exe, Home: f.home, Explicit: explicit, Record: record, Timeout: 10 * time.Second})
}

// grantedKeys lists the keys every recorded batchWrite upserted.
func (f *trustFixture) grantedKeys() []string {
	var keys []string
	for _, w := range f.load().Writes {
		data, _ := json.Marshal(w)
		var p struct {
			Edits []struct {
				Value map[string]any `json:"value"`
			} `json:"edits"`
		}
		_ = json.Unmarshal(data, &p)
		for _, e := range p.Edits {
			for k := range e.Value {
				keys = append(keys, keyLabel(k))
			}
		}
	}
	return keys
}

func hasResultNote(r GrantResult, note string) bool { return hasNote(r.Notes, note) }

func TestGrantTrust_GrantsEveryPairHookAndVerifies(t *testing.T) {
	f := newTrustFixture(t)
	r := f.grant(false, nil)
	if r.Outcome != "trusted" || !r.Trusted || r.Granted != len(hookEvents) {
		t.Fatalf("result = %+v, want all %d granted", r, len(hookEvents))
	}
	if len(r.Record) != len(hookEvents) {
		t.Errorf("record = %v, want one memo per event", r.Record)
	}

	// A second run finds everything trusted and writes nothing.
	again := f.grant(false, r.Record)
	if again.Outcome != "trusted" || again.Granted != 0 || len(f.load().Writes) != 1 {
		t.Errorf("re-run = %+v, writes = %d, want no new write", again, len(f.load().Writes))
	}
}

func TestGrantTrust_RevokedHookIsNotGrantedAgainByMaintain(t *testing.T) {
	f := newTrustFixture(t)
	first := f.grant(false, nil)
	// The user revokes Stop in /hooks: same hash, no longer trusted.
	f.edit("stop", func(h map[string]any) { h["trustStatus"] = "untrusted" })
	f.save(fakeState{Hooks: f.load().Hooks})

	r := f.grant(false, first.Record)
	if r.Trusted || !hasResultNote(r, "revoked in /hooks: stop") || len(f.grantedKeys()) != 0 {
		t.Fatalf("result = %+v, grants = %v, want stop refused as revoked", r, f.grantedKeys())
	}
	var stopKey string
	for k, m := range r.Record {
		if keyLabel(k) == "stop" {
			stopKey = k
			if !m.Revoked {
				t.Errorf("stop memo = %+v, want revoked", m)
			}
		}
	}

	// Even after Pair's entry changes (a new hash), a revoked key stays revoked.
	f.edit("stop", func(h map[string]any) { h["currentHash"] = "sha256:new" })
	if r := f.grant(false, r.Record); !hasResultNote(r, "revoked in /hooks: stop") || len(f.grantedKeys()) != 0 {
		t.Errorf("after a hash change: %+v, grants = %v, want still revoked", r, f.grantedKeys())
	}

	// An explicit `paircli init` is the user asking again.
	explicit := f.grant(true, r.Record)
	if !explicit.Trusted || explicit.Record[stopKey].Revoked {
		t.Errorf("explicit init = %+v, want stop granted and no longer revoked", explicit)
	}
}

func TestGrantTrust_ChangedPairEntryIsGrantedAgain(t *testing.T) {
	f := newTrustFixture(t)
	first := f.grant(false, nil)
	// A Pair update changes the entry: Codex reports a new hash, "modified".
	f.edit("stop", func(h map[string]any) { h["currentHash"], h["trustStatus"] = "sha256:new", "modified" })
	f.save(fakeState{Hooks: f.load().Hooks})

	r := f.grant(false, first.Record)
	if !r.Trusted || r.Granted != 1 || strings.Join(f.grantedKeys(), ",") != "stop" {
		t.Errorf("result = %+v, grants = %v, want stop granted again", r, f.grantedKeys())
	}
}

func TestGrantTrust_NeverGrantsADisabledHook(t *testing.T) {
	f := newTrustFixture(t)
	f.edit("stop", func(h map[string]any) { h["enabled"] = false })
	r := f.grant(true, nil)
	if r.Trusted || !hasResultNote(r, "disabled in Codex: stop") {
		t.Errorf("result = %+v, want stop refused as disabled", r)
	}
	for _, k := range f.grantedKeys() {
		if k == "stop" {
			t.Errorf("granted a disabled hook: %v", f.grantedKeys())
		}
	}
}

func TestGrantTrust_OnlyGrantsExactlyWhatPairWrote(t *testing.T) {
	changed, notListed := "changed outside Pair: post_tool_use", "not listed by this Codex: post_tool_use"
	cases := map[string]struct {
		change func(h map[string]any)
		note   string
	}{
		"plugin-sourced": {func(h map[string]any) { h["pluginId"] = "pair@pair" }, changed},
		"managed":        {func(h map[string]any) { h["isManaged"] = true }, changed},
		"edited timeout": {func(h map[string]any) { h["timeoutSec"] = 30 }, changed},
		"edited matcher": {func(h map[string]any) { h["matcher"] = ".*" }, changed},
		"edited command": {func(h map[string]any) { h["command"] = "/x/paircli hook codex PostToolUse --debug" }, changed},
		"other handler":  {func(h map[string]any) { h["handlerType"] = "prompt" }, changed},
		// Codex lists the event in Pair's file, but not at Pair's position.
		"other position": {func(h map[string]any) { h["key"] = strings.Replace(h["key"].(string), ":0:0", ":5:0", 1) }, changed},
		// Codex lists nothing for the event in Pair's file: reported, never granted.
		"other source": {func(h map[string]any) { h["sourcePath"] = "/elsewhere/hooks.json" }, notListed},
	}
	for name, c := range cases {
		t.Run(name, func(t *testing.T) {
			f := newTrustFixture(t)
			f.edit("post_tool_use", c.change)
			r := f.grant(true, nil)
			for _, k := range f.grantedKeys() {
				if k == "post_tool_use" {
					t.Fatalf("granted a hook that is not Pair's: %+v", r)
				}
			}
			if !hasResultNote(r, c.note) || r.Granted != len(hookEvents)-1 {
				t.Errorf("result = %+v, want %q and the other %d events granted", r, c.note, len(hookEvents)-1)
			}
			if c.note == changed && r.Trusted {
				t.Errorf("result = %+v, want not trusted", r)
			}
		})
	}
}

func TestGrantTrust_EventThisCodexDoesNotListIsReportedByName(t *testing.T) {
	f := newTrustFixture(t)
	st := f.load()
	var kept []map[string]any
	for _, h := range st.Hooks {
		if h["eventName"] != "interrupt" {
			kept = append(kept, h)
		}
	}
	f.save(fakeState{Hooks: kept})

	r := f.grant(false, nil)
	if !r.Trusted || r.Granted != len(hookEvents)-1 || !hasResultNote(r, "not listed by this Codex: interrupt") {
		t.Errorf("result = %+v, want interrupt named and the rest granted", r)
	}
}

// Codex listing none of Pair's entries (it reads another file, or spells the
// path differently) must never read as approved.
func TestGrantTrust_CodexListingNoneOfPairsHooksIsAFailure(t *testing.T) {
	f := newTrustFixture(t)
	st := f.load()
	for _, h := range st.Hooks {
		h["sourcePath"] = "/elsewhere/hooks.json"
	}
	f.save(st)
	r := f.grant(true, nil)
	if r.Trusted || !strings.HasPrefix(r.Outcome, "failed: codex lists none of Pair's hooks") || len(f.grantedKeys()) != 0 {
		t.Errorf("result = %+v, grants = %v, want a failure and no write", r, f.grantedKeys())
	}
	exe, _ := os.Executable()
	if _, _, ok := PairHookTrust(context.Background(), exe, f.home, "test", 5*time.Second); ok {
		t.Error("PairHookTrust ok with none of Pair's hooks listed, want a fallback")
	}
}

func TestGrantTrust_TwoMatchesForOneEventAreRefused(t *testing.T) {
	f := newTrustFixture(t)
	// A second, identical Pair entry in Stop's group (hand-copied, say), and
	// Codex listing both.
	data, _ := os.ReadFile(HooksPath())
	var doc map[string]any
	_ = json.Unmarshal(data, &doc)
	group := doc["hooks"].(map[string]any)["Stop"].([]any)[0].(map[string]any)
	group["hooks"] = append(group["hooks"].([]any), group["hooks"].([]any)[0])
	out, _ := json.Marshal(doc)
	writeFile(t, HooksPath(), string(out))
	st := f.load()
	for _, h := range st.Hooks {
		if h["eventName"] == "stop" {
			twin := map[string]any{}
			for k, v := range h {
				twin[k] = v
			}
			twin["key"] = strings.TrimSuffix(h["key"].(string), ":0") + ":1"
			st.Hooks = append(st.Hooks, twin)
			break
		}
	}
	f.save(st)

	r := f.grant(false, nil)
	if r.Trusted || !hasResultNote(r, "ambiguous: stop") {
		t.Errorf("result = %+v, want stop refused as ambiguous", r)
	}
	for _, k := range f.grantedKeys() {
		if k == "stop" {
			t.Errorf("granted an ambiguous event: %v", f.grantedKeys())
		}
	}
}

func TestGrantTrust_VerifyFailsWhenTheGrantDoesNotStick(t *testing.T) {
	f := newTrustFixture(t)
	st := f.load()
	st.IgnoreWrites = true
	f.save(st)
	r := f.grant(false, nil)
	if r.Trusted || !strings.HasPrefix(r.Outcome, "verify-failed: ") {
		t.Errorf("result = %+v, want verify-failed", r)
	}
}

func TestGrantTrust_OldCodexWithoutHashesIsUnsupported(t *testing.T) {
	f := newTrustFixture(t)
	st := f.load()
	for _, h := range st.Hooks {
		delete(h, "currentHash")
		delete(h, "trustStatus")
	}
	f.save(st)
	if r := f.grant(false, nil); r.Outcome != "unsupported" || r.Trusted || len(f.grantedKeys()) != 0 {
		t.Errorf("result = %+v, want unsupported and no write", r)
	}
}

func TestGrantTrust_CodexWithoutTheTrustCallsIsUnsupported(t *testing.T) {
	for _, method := range []string{"hooks/list", "config/batchWrite"} {
		t.Run(method, func(t *testing.T) {
			f := newTrustFixture(t)
			st := f.load()
			st.Unknown = []string{method}
			f.save(st)
			if r := f.grant(false, nil); r.Outcome != "unsupported" || r.Trusted {
				t.Errorf("result = %+v, want unsupported", r)
			}
			exe, _ := os.Executable()
			if _, _, ok := PairHookTrust(context.Background(), exe, f.home, "test", 5*time.Second); ok && method == "hooks/list" {
				t.Error("PairHookTrust ok without hooks/list, want a fallback")
			}
		})
	}
}

func TestGrantTrust_HungAppServerIsKilledAtTheTimeLimit(t *testing.T) {
	f := newTrustFixture(t)
	st := f.load()
	st.Hang = true
	f.save(st)
	exe, _ := os.Executable()
	start := time.Now()
	r := GrantTrust(context.Background(), GrantOptions{Codex: exe, Home: f.home, Timeout: 500 * time.Millisecond})
	if r.Outcome != "timeout" || r.Trusted {
		t.Errorf("result = %+v, want timeout", r)
	}
	if elapsed := time.Since(start); elapsed > 5*time.Second {
		t.Errorf("GrantTrust took %v after a 500ms limit", elapsed)
	}
}

func TestGrantTrust_MissingBinaryIsAFailureOutcome(t *testing.T) {
	newTrustFixture(t)
	r := GrantTrust(context.Background(), GrantOptions{Codex: filepath.Join(t.TempDir(), "codex"), Home: t.TempDir()})
	if !strings.HasPrefix(r.Outcome, "failed: ") || r.Trusted {
		t.Errorf("result = %+v, want a failed outcome", r)
	}
}

func TestRevokeTrust_DeletesTheRecordedKeys(t *testing.T) {
	f := newTrustFixture(t)
	r := f.grant(false, nil)
	var keys []string
	for k := range r.Record {
		keys = append(keys, k)
	}
	exe, _ := os.Executable()
	if err := RevokeTrust(context.Background(), exe, "test", keys, 5*time.Second); err != nil {
		t.Fatal(err)
	}
	for _, h := range f.load().Hooks {
		if h["trustStatus"] != "untrusted" {
			t.Errorf("%v still %v", h["key"], h["trustStatus"])
		}
	}
}

func TestPairHookTrust_CountsThroughHooksList(t *testing.T) {
	f := newTrustFixture(t)
	exe, _ := os.Executable()
	if trusted, total, ok := PairHookTrust(context.Background(), exe, f.home, "test", 5*time.Second); !ok || trusted != 0 || total != len(hookEvents) {
		t.Errorf("before = %d/%d ok=%v", trusted, total, ok)
	}
	f.grant(false, nil)
	if trusted, total, ok := PairHookTrust(context.Background(), exe, f.home, "test", 5*time.Second); !ok || trusted != total || total != len(hookEvents) {
		t.Errorf("after = %d/%d ok=%v", trusted, total, ok)
	}
	// An entry edited outside Pair still counts in the total, untrusted.
	f.edit("stop", func(h map[string]any) { h["timeoutSec"] = 30 })
	if trusted, total, ok := PairHookTrust(context.Background(), exe, f.home, "test", 5*time.Second); !ok || trusted != total-1 || total != len(hookEvents) {
		t.Errorf("with an edited entry = %d/%d ok=%v, want %d/%d", trusted, total, ok, len(hookEvents)-1, len(hookEvents))
	}
}

// TestGrantTrust_RealCodex runs the grant against the codex on PATH, in a
// throwaway CODEX_HOME. Opt in with PAIRCLI_TEST_REAL_CODEX=1.
func TestGrantTrust_RealCodex(t *testing.T) {
	if os.Getenv("PAIRCLI_TEST_REAL_CODEX") != "1" {
		t.Skip("set PAIRCLI_TEST_REAL_CODEX=1 to run against a real codex")
	}
	codexBin, err := exec.LookPath("codex")
	if err != nil {
		t.Skip("codex not on PATH")
	}
	home := t.TempDir()
	t.Setenv("HOME", home)
	codexHomeIn(t, filepath.Join(home, ".codex"))
	if err := InstallHooks(); err != nil {
		t.Fatal(err)
	}
	opts := GrantOptions{Codex: codexBin, Home: home, ClientVersion: "test"}
	first := GrantTrust(context.Background(), opts)
	if !first.Trusted || first.Granted == 0 {
		t.Fatalf("first grant = %+v", first)
	}
	if trusted, total, ok := PairHookTrust(context.Background(), codexBin, home, "test", DefaultTrustTimeout); !ok || trusted != total || total == 0 {
		t.Errorf("trust after grant = %d/%d ok=%v", trusted, total, ok)
	}

	// Simulate a revocation by deleting one trusted_hash; maintain must not re-grant it.
	var stopKey string
	for k := range first.Record {
		if keyLabel(k) == "stop" {
			stopKey = k
		}
	}
	if err := RevokeTrust(context.Background(), codexBin, "test", []string{stopKey}, DefaultTrustTimeout); err != nil {
		t.Fatal(err)
	}
	opts.Record = first.Record
	maintain := GrantTrust(context.Background(), opts)
	if maintain.Trusted || !hasResultNote(maintain, "revoked in /hooks: stop") {
		t.Errorf("maintain after revoke = %+v, want stop left revoked", maintain)
	}
	opts.Record, opts.Explicit = maintain.Record, true
	if explicit := GrantTrust(context.Background(), opts); !explicit.Trusted || explicit.Granted != 1 {
		t.Errorf("explicit init after revoke = %+v, want stop granted", explicit)
	}
}
