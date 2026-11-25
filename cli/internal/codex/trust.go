package codex

import (
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"
)

// DefaultTrustTimeout bounds a whole trust session (start, list, write,
// verify), so a hung Codex cannot hold the caller's setup lock.
const DefaultTrustTimeout = 20 * time.Second

// TrustMemo is what Pair remembers about one of its Codex hook-trust keys.
type TrustMemo struct {
	// Hash is the hash Codex last reported trusted for the key, whether Pair
	// or the user trusted it.
	Hash string `json:"hash,omitempty"`
	// Revoked means the key went from trusted to untrusted while Pair's entry
	// stayed the same: the user revoked it in /hooks. Only an explicit
	// `paircli init` grants it again.
	Revoked bool `json:"revoked,omitempty"`
}

// TrustRecord is Pair's memory of its Codex hook-trust keys, by the key
// exactly as Codex reports it. It is kept in init.json.
type TrustRecord map[string]TrustMemo

// GrantOptions configures GrantTrust.
type GrantOptions struct {
	Codex         string        // codex binary
	Home          string        // directory hooks/list inspects (the user's home)
	Explicit      bool          // the user ran `paircli init`: grant revoked keys again
	Record        TrustRecord   // from init.json; nil is empty
	Timeout       time.Duration // 0 is DefaultTrustTimeout
	ClientVersion string
}

// GrantResult is the outcome of GrantTrust.
type GrantResult struct {
	// Outcome is a short, named result for setup.json: "trusted",
	// "not trusted", "unsupported", "timeout", "verify-failed: …" or
	// "failed: …".
	Outcome string
	// Notes name the events that were not trusted, and why.
	Notes []string
	// Trusted reports that every Pair hook this Codex lists is trusted.
	Trusted bool
	// Granted counts the keys this run wrote.
	Granted int
	// Record is opts.Record updated with this run's observations. It is
	// returned even on failure.
	Record TrustRecord
}

// String is the result as one setup.json step outcome.
func (r GrantResult) String() string {
	if len(r.Notes) == 0 {
		return r.Outcome
	}
	return r.Outcome + "; " + strings.Join(r.Notes, "; ")
}

// listedHook is the part of Codex's HookMetadata (app-server hooks/list)
// Pair reads. A Codex without hook approvals (before ~0.129) reports no hash
// or trust status.
type listedHook struct {
	Key         string  `json:"key"`
	HandlerType string  `json:"handlerType"`
	Command     *string `json:"command"`
	Matcher     *string `json:"matcher"`
	TimeoutSec  *int64  `json:"timeoutSec"`
	SourcePath  string  `json:"sourcePath"`
	PluginID    *string `json:"pluginId"`
	IsManaged   bool    `json:"isManaged"`
	Enabled     *bool   `json:"enabled"`
	CurrentHash *string `json:"currentHash"`
	TrustStatus *string `json:"trustStatus"`
}

type hooksListResult struct {
	Data []struct {
		Hooks []listedHook `json:"hooks"`
	} `json:"data"`
}

// GrantTrust approves Pair's hooks inside Codex through Codex's own trust
// RPCs, the way the TUI's "Trust" does: hooks/list, then config/batchWrite of
// hooks.state.<key>.trusted_hash with the hash Codex computed, then hooks/list
// again to verify. Pair never computes a hash itself.
//
// It picks only entries that are exactly what InstallHooks wrote, in
// $CODEX_HOME/hooks.json, unmanaged and not from a plugin. It never grants an
// entry Codex reports disabled, and without opts.Explicit never one the user
// revoked. Every failure is returned as a named outcome, never as an error:
// trust is a convenience on top of the hooks, which are already installed.
func GrantTrust(ctx context.Context, opts GrantOptions) GrantResult {
	timeout := opts.Timeout
	if timeout <= 0 {
		timeout = DefaultTrustTimeout
	}
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()

	res := GrantResult{Record: TrustRecord{}}
	for k, v := range opts.Record {
		res.Record[k] = v
	}
	fail := func(err error) GrantResult {
		switch {
		case ctx.Err() != nil:
			res.Outcome = "timeout"
		case errors.Is(err, errUnsupported):
			res.Outcome = "unsupported"
		default:
			res.Outcome = "failed: " + err.Error()
		}
		res.Trusted = false
		return res
	}

	want, err := expectedHooks()
	if err != nil {
		return fail(err)
	}
	srv, err := startAppServer(ctx, opts.Codex, opts.ClientVersion)
	if err != nil {
		return fail(err)
	}
	defer srv.close()

	listed, err := listHooks(srv, opts.Home)
	if err != nil {
		return fail(err)
	}
	picked, notes, ok := pickPairHooks(listed, want)
	if !ok {
		res.Outcome = "unsupported"
		return res
	}
	if len(picked) == 0 {
		// "Not listed" is only an older Codex lacking an event; Codex listing
		// none of Pair's entries means it reads another file (or spells its
		// path differently), and nothing here may claim approval.
		res.Outcome = "failed: codex lists none of Pair's hooks in " + HooksPath()
		return res
	}
	res.Notes = notes
	refused := len(notes) - countNotListed(notes)

	grants := map[string]any{}
	grantHash := map[string]string{}
	for _, p := range picked {
		key, hash, label := p.Key, *p.CurrentHash, p.label
		memo := res.Record[key]
		switch {
		case p.Enabled != nil && !*p.Enabled:
			res.Notes = append(res.Notes, "disabled in Codex: "+label)
			refused++
		case *p.TrustStatus == "trusted":
			res.Record[key] = TrustMemo{Hash: hash}
		case !opts.Explicit && memo.Revoked:
			res.Notes = append(res.Notes, "revoked in /hooks: "+label)
			refused++
		case !opts.Explicit && memo.Hash != "" && memo.Hash == hash:
			// Trusted before at this very hash, untrusted now: the user revoked
			// it. A Codex that rewrote config.toml from a stale copy looks the
			// same; that too is left alone, and status names the one command
			// (`paircli init`) that grants it again.
			res.Record[key] = TrustMemo{Hash: hash, Revoked: true}
			res.Notes = append(res.Notes, "revoked in /hooks: "+label)
			refused++
		default:
			grants[key] = map[string]any{"trusted_hash": hash}
			grantHash[key] = hash
		}
	}

	if len(grants) > 0 {
		edit := map[string]any{"keyPath": "hooks.state", "value": grants, "mergeStrategy": "upsert"}
		if err := srv.call("config/batchWrite", map[string]any{"edits": []any{edit}, "reloadUserConfig": true}, nil); err != nil {
			return fail(err)
		}
		after, err := listHooks(srv, opts.Home)
		if err != nil {
			return fail(err)
		}
		byKey := map[string]listedHook{}
		for _, h := range after {
			byKey[h.Key] = h
		}
		var bad []string
		for key, hash := range grantHash {
			h, ok := byKey[key]
			switch {
			case !ok:
				bad = append(bad, keyLabel(key)+" not listed")
			case h.TrustStatus == nil || *h.TrustStatus != "trusted":
				bad = append(bad, keyLabel(key)+" "+deref(h.TrustStatus))
			case h.CurrentHash == nil || *h.CurrentHash != hash:
				bad = append(bad, keyLabel(key)+" changed")
			default:
				res.Record[key] = TrustMemo{Hash: hash}
				res.Granted++
			}
		}
		if len(bad) > 0 {
			sort.Strings(bad)
			res.Outcome = "verify-failed: " + strings.Join(bad, ", ")
			return res
		}
	}

	res.Trusted = refused == 0
	if res.Trusted {
		res.Outcome = "trusted"
	} else {
		res.Outcome = "not trusted"
	}
	return res
}

// RevokeTrust deletes hooks.state entries for keys through Codex's
// config/batchWrite (uninstall uses it for the keys init.json recorded).
func RevokeTrust(ctx context.Context, codexPath, clientVersion string, keys []string, timeout time.Duration) error {
	if len(keys) == 0 {
		return nil
	}
	if timeout <= 0 {
		timeout = DefaultTrustTimeout
	}
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	srv, err := startAppServer(ctx, codexPath, clientVersion)
	if err != nil {
		return err
	}
	defer srv.close()
	var edits []any
	for _, key := range keys {
		edits = append(edits, map[string]any{"keyPath": "hooks.state." + strconv.Quote(key), "value": nil, "mergeStrategy": "replace"})
	}
	if err := srv.call("config/batchWrite", map[string]any{"edits": edits, "reloadUserConfig": true}, nil); err != nil {
		if ctx.Err() != nil {
			return fmt.Errorf("codex app-server timed out")
		}
		return err
	}
	return nil
}

// PairHookTrust reports, through hooks/list, how many of Pair's hooks in
// hooks.json Codex lists as trusted and enabled, out of all of Pair's entries
// there (less any event this Codex does not have). ok is false when Codex cannot answer (old Codex, timeout, no
// app-server) or lists none of Pair's entries, so the caller can fall back.
func PairHookTrust(ctx context.Context, codexPath, home, clientVersion string, timeout time.Duration) (trusted, total int, ok bool) {
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	want, err := expectedHooks()
	if err != nil {
		return 0, 0, false
	}
	srv, err := startAppServer(ctx, codexPath, clientVersion)
	if err != nil {
		return 0, 0, false
	}
	defer srv.close()
	listed, err := listHooks(srv, home)
	if err != nil {
		return 0, 0, false
	}
	picked, notes, ok := pickPairHooks(listed, want)
	if !ok || len(picked) == 0 {
		return 0, 0, false
	}
	// One per event Pair installed, except events this Codex does not have.
	for label, e := range want {
		if len(e.positions) > 0 && !hasNote(notes, notListedPrefix+label) {
			total++
		}
	}
	for _, p := range picked {
		if *p.TrustStatus == "trusted" && (p.Enabled == nil || *p.Enabled) {
			trusted++
		}
	}
	return trusted, total, true
}

func listHooks(srv *appServer, home string) ([]listedHook, error) {
	var res hooksListResult
	if err := srv.call("hooks/list", map[string]any{"cwds": []string{home}}, &res); err != nil {
		return nil, err
	}
	// hooks/list repeats user hooks once per cwd; keep each key once.
	seen := map[string]bool{}
	var out []listedHook
	for _, entry := range res.Data {
		for _, h := range entry.Hooks {
			if h.Key == "" || seen[h.Key] {
				continue
			}
			seen[h.Key] = true
			out = append(out, h)
		}
	}
	return out, nil
}

// expectedHook is one hook entry exactly as InstallHooks writes it.
type expectedHook struct {
	label     string // Codex's snake_case event name
	command   string
	matcher   *string
	timeout   int64
	positions map[[2]int]bool // (group, index) of Pair's entries in hooks.json
}

// expectedHooks describes Pair's entries in the current hooks.json, by event
// label.
func expectedHooks() (map[string]*expectedHook, error) {
	bin, err := pairCLIBinary()
	if err != nil {
		return nil, err
	}
	positions, err := ourHookPositions()
	if err != nil {
		return nil, err
	}
	want := map[string]*expectedHook{}
	for _, spec := range hookEvents {
		label := eventSnakeCase[spec.Event]
		e := &expectedHook{
			label:     label,
			command:   fmt.Sprintf("%s hook codex %s", bin, spec.Event),
			timeout:   codexTimeout(label, 10),
			positions: map[[2]int]bool{},
		}
		if spec.Matcher != "" {
			m := spec.Matcher
			e.matcher = &m
		}
		want[label] = e
	}
	for _, p := range positions {
		if e := want[eventSnakeCase[p.Event]]; e != nil {
			e.positions[[2]int{p.Group, p.Index}] = true
		}
	}
	return want, nil
}

// codexTimeout is the timeout Codex reports for a command hook written with
// seconds: Codex clamps interrupt and session_end hooks to 1–3 s.
func codexTimeout(label string, seconds int64) int64 {
	if label == "interrupt" || label == "session_end" {
		return min(3, max(1, seconds))
	}
	return max(1, seconds)
}

// pickedHook is a listed hook matched to one of Pair's events.
type pickedHook struct {
	listedHook
	label string
}

// pickPairHooks matches Codex's listing against Pair's expected entries: one
// per event, by source file, position and the whole entry. ok is false when
// this Codex reports no hash or trust status (it predates the trust API).
func pickPairHooks(listed []listedHook, want map[string]*expectedHook) (picked []pickedHook, notes []string, ok bool) {
	sources := hooksSourcePaths()
	matches := map[string][]listedHook{}
	inFile := map[string]bool{} // Codex lists some hook for the event in Pair's file
	for _, h := range listed {
		path, label, group, index, parsed := parseTrustKey(h.Key)
		e := want[label]
		if !parsed || e == nil || !sources[h.SourcePath] || !sources[path] {
			continue
		}
		if h.CurrentHash == nil || h.TrustStatus == nil {
			return nil, nil, false
		}
		inFile[label] = true
		if !e.positions[[2]int{group, index}] {
			continue
		}
		if h.IsManaged || h.PluginID != nil || h.HandlerType != "command" ||
			h.Command == nil || *h.Command != e.command ||
			h.TimeoutSec == nil || *h.TimeoutSec != e.timeout ||
			!sameMatcher(h.Matcher, e.matcher) {
			continue
		}
		matches[label] = append(matches[label], h)
	}
	for _, spec := range hookEvents {
		label := eventSnakeCase[spec.Event]
		switch m := matches[label]; {
		case len(m) == 1:
			picked = append(picked, pickedHook{listedHook: m[0], label: label})
		case len(m) > 1:
			notes = append(notes, "ambiguous: "+label)
		case inFile[label]:
			// Codex knows the event but not Pair's entry as written (edited,
			// moved, or numbered differently): never approved.
			notes = append(notes, "changed outside Pair: "+label)
		default:
			// This Codex lists nothing for the event: an older Codex without it.
			notes = append(notes, notListedPrefix+label)
		}
	}
	return picked, notes, true
}

const notListedPrefix = "not listed by this Codex: "

func hasNote(notes []string, note string) bool {
	for _, n := range notes {
		if n == note {
			return true
		}
	}
	return false
}

func countNotListed(notes []string) int {
	n := 0
	for _, note := range notes {
		if strings.HasPrefix(note, notListedPrefix) {
			n++
		}
	}
	return n
}

// hooksSourcePaths is the set of spellings Codex may report for
// $CODEX_HOME/hooks.json: absolute as configured, with its directory's
// symlinks resolved (macOS /var → /private/var, a symlinked home), and with
// the file itself resolved.
func hooksSourcePaths() map[string]bool {
	path := HooksPath()
	set := map[string]bool{}
	if path == "" {
		return set
	}
	if abs, err := filepath.Abs(path); err == nil {
		path = abs
	}
	set[path] = true
	if dir, err := filepath.EvalSymlinks(filepath.Dir(path)); err == nil {
		set[filepath.Join(dir, filepath.Base(path))] = true
	}
	if real, err := filepath.EvalSymlinks(path); err == nil {
		set[real] = true
	}
	return set
}

// parseTrustKey splits "<source path>:<event>:<group>:<index>".
func parseTrustKey(key string) (path, label string, group, index int, ok bool) {
	parts := strings.Split(key, ":")
	if len(parts) < 4 {
		return "", "", 0, 0, false
	}
	n := len(parts)
	g, err1 := strconv.Atoi(parts[n-2])
	i, err2 := strconv.Atoi(parts[n-1])
	if err1 != nil || err2 != nil || g < 0 || i < 0 {
		return "", "", 0, 0, false
	}
	return strings.Join(parts[:n-3], ":"), parts[n-3], g, i, true
}

func keyLabel(key string) string {
	if _, label, _, _, ok := parseTrustKey(key); ok {
		return label
	}
	return key
}

func sameMatcher(got, want *string) bool {
	if got == nil || want == nil {
		return got == nil && want == nil
	}
	return *got == *want
}

func deref(s *string) string {
	if s == nil {
		return "unknown"
	}
	return *s
}

// FindCodex locates the codex binary: saved (the path init resolved) first,
// then $PATH, then the usual install locations, which matter when a hook's
// PATH is narrower than the user's shell. "" means not found.
func FindCodex(saved string) string {
	if isExecutable(saved) {
		return saved
	}
	if path, err := exec.LookPath("codex"); err == nil {
		return path
	}
	home, _ := os.UserHomeDir()
	candidates := []string{
		"/opt/homebrew/bin/codex",
		"/usr/local/bin/codex",
		// Best guess at the desktop app's bundled CLI; not verified.
		"/Applications/Codex.app/Contents/Resources/codex",
	}
	if home != "" {
		candidates = append(candidates,
			filepath.Join(home, ".local", "bin", "codex"),
			filepath.Join(home, ".npm-global", "bin", "codex"),
			filepath.Join(home, ".bun", "bin", "codex"),
			filepath.Join(home, "Applications", "Codex.app", "Contents", "Resources", "codex"),
		)
	}
	for _, c := range candidates {
		if isExecutable(c) {
			return c
		}
	}
	return ""
}

func isExecutable(path string) bool {
	if path == "" {
		return false
	}
	info, err := os.Stat(path)
	return err == nil && !info.IsDir() && info.Mode()&0o111 != 0
}
