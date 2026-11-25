package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/interviewstreet/pair-plugin/cli/internal/deviceauth"
	"github.com/interviewstreet/pair-plugin/cli/internal/selfupdate"
	"github.com/interviewstreet/pair-plugin/cli/internal/state"
)

// initEnv is setupEnv plus init's own seams: both agents detected and
// installed, Codex trust granted, gh ready, no terminal, no exec.
func initEnv(t *testing.T) (calls *[]string, trustCalls *[]bool) {
	t.Helper()
	calls = setupEnv(t)
	home, _ := os.UserHomeDir()
	for _, dir := range []string{".claude", ".codex"} {
		if err := os.MkdirAll(filepath.Join(home, dir), 0o700); err != nil {
			t.Fatal(err)
		}
	}
	fakeInstallEach(t,
		harnessResult{Name: "claude-code", Detected: true},
		harnessResult{Name: "codex", Detected: true},
		harnessResult{Name: "pi"},
	)
	trustCalls = &[]bool{}
	origTrust, origWait, origInstallWait := setupCodexTrust, initLockWait, installLockWait
	origTTY, origGh, origExec, origExe, origBin := initHasTTY, initGhState, initExec, initExecutable, initBinaryPath
	t.Cleanup(func() {
		setupCodexTrust, initLockWait, installLockWait = origTrust, origWait, origInstallWait
		initHasTTY, initGhState, initExec, initExecutable, initBinaryPath = origTTY, origGh, origExec, origExe, origBin
	})
	initBinaryPath = func() (string, error) { return filepath.Join(home, ".paircli", "bin", "paircli"), nil }
	setupCodexTrust = func(r *initRecord, explicit bool) (string, bool) {
		*trustCalls = append(*trustCalls, explicit)
		r.CodexPath = "/usr/local/bin/codex"
		return "trusted", true
	}
	initLockWait, installLockWait = 200*time.Millisecond, 200*time.Millisecond
	initHasTTY = func() bool { return false }
	initGhState = func() string { return "ok" }
	initExec = func(string, []string, []string) error { t.Fatal("init exec'd without --install-self"); return nil }
	return calls, trustCalls
}

func runInitArgs(t *testing.T, args ...string) (string, error) {
	t.Helper()
	var out bytes.Buffer
	err := initRun(args, &out)
	return out.String(), err
}

func exitCodeOf(err error) int {
	var coded interface{ ExitCode() int }
	if errors.As(err, &coded) {
		return coded.ExitCode()
	}
	return -1
}

// With no other setup running, init must get through every step: a lock it
// takes itself must not lock it out.
func TestInit_CompletesEveryStepAndEnrolls(t *testing.T) {
	calls, trustCalls := initEnv(t)
	out, err := runInitArgs(t)
	if err != nil {
		t.Fatalf("init: %v\n%s", err, out)
	}
	if got := strings.Join(*calls, ","); got != "update:,login:https://pair.example" {
		t.Errorf("calls = %s, want update and login (hooks are per agent)", got)
	}
	if len(*trustCalls) == 0 || (*trustCalls)[len(*trustCalls)-1] {
		t.Errorf("trust calls = %v, want a non-explicit grant without a terminal", *trustCalls)
	}
	rec, err := readInitRecord()
	if err != nil {
		t.Fatalf("init.json: %v", err)
	}
	if rec.Harnesses["claude-code"] != "installed" || rec.Harnesses["codex"] != "installed" || rec.CodexTrust != "trusted" ||
		rec.CodexPath != "/usr/local/bin/codex" || rec.EnrolledAt.IsZero() {
		t.Errorf("init.json = %+v", rec)
	}
	for _, want := range []string{
		"Pair will set up:",
		"hooks in Claude Code and Codex, approved in Codex on your behalf",
		"PR evidence (including redacted agent transcripts)",
		"Pair: found Claude Code, Codex",
		"Pair: hooks installed for Claude Code (~/.claude/settings.json)",
		"Pair: hooks installed and trusted for Codex (~/.codex/hooks.json)",
		"Done: your next `gh pr create` or `git push` publishes to pair.sh.",
	} {
		if !strings.Contains(out, want) {
			t.Errorf("output lacks %q:\n%s", want, out)
		}
	}
	if !strings.Contains(out, "WDJP-7Q2K") {
		t.Errorf("the login code did not reach the terminal:\n%s", out)
	}
}

func TestInit_OpensTheBrowserOnlyWithATerminal(t *testing.T) {
	for _, tty := range []bool{false, true} {
		initEnv(t)
		var noBrowser []bool
		setupEnsureLogin = func(_ string, _, _ io.Writer, nb bool) (deviceauth.StoredAuth, error) {
			noBrowser = append(noBrowser, nb)
			return deviceauth.StoredAuth{}, nil
		}
		initHasTTY = func() bool { return tty }
		if _, err := runInitArgs(t); err != nil {
			t.Fatal(err)
		}
		if len(noBrowser) == 0 || noBrowser[len(noBrowser)-1] == tty {
			t.Errorf("tty=%v: login noBrowser = %v, want %v", tty, noBrowser, !tty)
		}
	}
}

func TestInit_WaitsForAnotherSetupThenExits3WithTheLog(t *testing.T) {
	calls, _ := initEnv(t)
	if err := writeSetupStatus(setupStatus{State: setupRunning, Log: "/tmp/setup-1.log"}); err != nil {
		t.Fatal(err)
	}
	lock, err := state.TryLock("setup")
	if err != nil {
		t.Fatal(err)
	}
	defer lock.Release()

	start := time.Now()
	_, err = runInitArgs(t)
	if exitCodeOf(err) != exitSetupBusy || !strings.Contains(err.Error(), "already running in another process (log: /tmp/setup-1.log)") {
		t.Fatalf("init = %v, want exit 3 naming the log", err)
	}
	if time.Since(start) < initLockWait {
		t.Errorf("init gave up after %v, before initLockWait", time.Since(start))
	}
	if len(*calls) != 0 || initEnrolled() {
		t.Errorf("init ran steps (%v) or enrolled without the lock", *calls)
	}
}

func TestInit_LoginFailureLeavesNoEnrolment(t *testing.T) {
	initEnv(t)
	setupEnsureLogin = func(string, io.Writer, io.Writer, bool) (deviceauth.StoredAuth, error) {
		return deviceauth.StoredAuth{}, errors.New("login denied by user")
	}
	if _, err := runInitArgs(t); err == nil || !strings.Contains(err.Error(), "login denied") {
		t.Fatalf("init = %v, want the login failure", err)
	}
	if initEnrolled() {
		t.Error("init.json written after a failed login")
	}
}

func TestInit_NoLoginStillEnrolls(t *testing.T) {
	calls, _ := initEnv(t)
	out, err := runInitArgs(t, "--no-login")
	if err != nil {
		t.Fatal(err)
	}
	for _, c := range *calls {
		if strings.HasPrefix(c, "login:") {
			t.Errorf("login ran with --no-login: %v", *calls)
		}
	}
	if !initEnrolled() {
		t.Error("--no-login with hooks installed did not enroll")
	}
	if !strings.Contains(out, "without logging in") {
		t.Errorf("output = %s", out)
	}
}

func TestInit_OneFailedAgentStillEnrollsAndRecordsIt(t *testing.T) {
	_, trustCalls := initEnv(t)
	fakeInstallEach(t,
		harnessResult{Name: "claude-code", Detected: true},
		harnessResult{Name: "codex", Detected: true, Err: errors.New("hooks.json is not valid JSON")},
	)
	out, err := runInitArgs(t)
	if err != nil {
		t.Fatalf("init: %v", err)
	}
	rec, _ := readInitRecord()
	if !strings.HasPrefix(rec.Harnesses["codex"], "failed") || rec.Harnesses["claude-code"] != "installed" {
		t.Errorf("init.json harnesses = %v", rec.Harnesses)
	}
	if len(*trustCalls) != 0 {
		t.Error("granted Codex trust although its hooks failed to install")
	}
	if !strings.Contains(out, "for Codex") || !strings.Contains(out, "not valid JSON") {
		t.Errorf("output does not report the failed agent:\n%s", out)
	}
}

func TestInit_NoAgentFailsWithoutEnrolling(t *testing.T) {
	initEnv(t)
	fakeInstallEach(t, harnessResult{Name: "claude-code"}, harnessResult{Name: "codex"})
	if _, err := runInitArgs(t); err == nil {
		t.Fatal("init succeeded with no agent")
	}
	if initEnrolled() {
		t.Error("enrolled with no agent")
	}
}

func TestInit_GhMissingOrLoggedOutEndsWithOneStepLeft(t *testing.T) {
	for state, want := range map[string]string{
		"missing":    "Install the GitHub CLI: `brew install gh` (macOS) or see https://cli.github.com (Linux), then run `gh auth login`.",
		"logged-out": "Run `gh auth login`.",
	} {
		initEnv(t)
		initGhState = func() string { return state }
		out, err := runInitArgs(t)
		if err != nil {
			t.Fatalf("%s: init failed: %v", state, err)
		}
		if !strings.Contains(out, "one step left. "+want) || strings.Contains(out, "Done:") {
			t.Errorf("%s: output = %s", state, out)
		}
		if !initEnrolled() {
			t.Errorf("%s: everything else should still be set up", state)
		}
	}
}

func TestInit_UntrustedCodexPrintsTheHooksFallback(t *testing.T) {
	initEnv(t)
	setupCodexTrust = func(*initRecord, bool) (string, bool) { return "unsupported", false }
	out, err := runInitArgs(t)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(out, "not yet approved in Codex (unsupported)") || !strings.Contains(out, "approve the paircli hooks") {
		t.Errorf("output = %s", out)
	}
}

// The plugin's `setup` never asks Codex to trust anything (D1 is init-only).
func TestSetup_WithoutGrantFlagNeverCallsTrust(t *testing.T) {
	_, trustCalls := initEnv(t)
	for _, args := range [][]string{{"--harness", "codex", "--plugin-version", "0.4.1"}, {}} {
		if _, _, err := runSetupArgs(t, args...); err != nil {
			t.Fatal(err)
		}
	}
	if len(*trustCalls) != 0 || initEnrolled() {
		t.Errorf("setup called trust %v times or enrolled", len(*trustCalls))
	}
	if _, _, err := runSetupArgs(t, "--harness", "codex", "--grant-codex-trust"); err != nil {
		t.Fatal(err)
	}
	if len(*trustCalls) != 1 || (*trustCalls)[0] {
		t.Errorf("trust calls = %v, want one non-explicit grant", *trustCalls)
	}
}

func TestStatus_ShowsRevokedCodexTrust(t *testing.T) {
	initEnv(t)
	setupCodexTrust = func(*initRecord, bool) (string, bool) { return "not trusted; revoked in /hooks: stop", false }
	if _, err := runInitArgs(t); err != nil {
		t.Fatal(err)
	}
	var out bytes.Buffer
	if err := statusRun(nil, &out); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(out.String(), "Codex trust for Pair hooks was revoked in `/hooks`; run `~/.paircli/bin/paircli init` to grant it again") {
		t.Errorf("status = %s", out.String())
	}
	// A later plugin setup (no trust step) does not hide it.
	if _, _, err := runSetupArgs(t, "--harness", "claude-code", "--plugin-version", "0.4.1"); err != nil {
		t.Fatal(err)
	}
	out.Reset()
	if err := statusRun(nil, &out); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(out.String(), "revoked in `/hooks`") {
		t.Errorf("status after a plugin setup = %s", out.String())
	}
}

// fakeInstaller makes a temp directory holding a "verified" binary, as
// install.sh leaves it, and points init's executable at it.
func fakeInstaller(t *testing.T) (dir, exe string) {
	t.Helper()
	dir = filepath.Join(t.TempDir(), "pair-install.abc123")
	if err := os.Mkdir(dir, 0o700); err != nil {
		t.Fatal(err)
	}
	exe = filepath.Join(dir, "paircli")
	if err := os.WriteFile(exe, []byte("#!/bin/sh\necho new paircli\n"), 0o700); err != nil {
		t.Fatal(err)
	}
	initExecutable = func() (string, error) { return exe, nil }
	return dir, exe
}

func TestInit_InstallSelfInstallsMarksAndReExecs(t *testing.T) {
	initEnv(t)
	dir, _ := fakeInstaller(t)
	home, _ := os.UserHomeDir()
	bin := filepath.Join(home, ".paircli", "bin", "paircli")
	if err := os.MkdirAll(filepath.Dir(bin), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(bin, []byte("old"), 0o755); err != nil {
		t.Fatal(err)
	}
	var execd []string
	initExec = func(path string, argv, _ []string) error {
		execd = append([]string{path}, argv...)
		return nil
	}

	out, err := runInitArgs(t, "--install-self", dir, "--enroll", "--grant-codex-trust", "--no-login")
	if err != nil {
		t.Fatalf("init --install-self: %v", err)
	}
	if got, _ := os.ReadFile(bin); string(got) != "#!/bin/sh\necho new paircli\n" {
		t.Errorf("installed binary = %q", got)
	}
	if info, _ := os.Stat(bin); info.Mode().Perm() != 0o755 {
		t.Errorf("installed mode = %v, want 0755", info.Mode().Perm())
	}
	var marker map[string]string
	data, _ := os.ReadFile(filepath.Join(filepath.Dir(bin), "release-source.json"))
	if json.Unmarshal(data, &marker) != nil || marker["manifest_url"] != selfupdate.DefaultManifestURL {
		t.Errorf("release-source.json = %s, want the plugin bootstrap's marker", data)
	}
	if _, err := os.Stat(dir); !os.IsNotExist(err) {
		t.Errorf("installer temp dir still exists: %v", err)
	}
	if want := []string{bin, bin, "init", "--after-install", "--no-login"}; strings.Join(execd, " ") != strings.Join(want, " ") {
		t.Errorf("exec = %v, want %v", execd, want)
	}
	if !strings.Contains(out, "Pair will set up:") || !strings.Contains(out, "Pair: installed paircli") {
		t.Errorf("output = %s", out)
	}

	// The re-exec'd run does not print the summary again.
	out, err = runInitArgs(t, "--after-install", "--no-login")
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(out, "Pair will set up:") {
		t.Errorf("summary printed twice:\n%s", out)
	}
}

func TestInit_InstallSelfExits4WhileBootstrapHoldsTheLock(t *testing.T) {
	initEnv(t)
	dir, _ := fakeInstaller(t)
	if err := os.MkdirAll(filepath.Dir(state.BootstrapLockPath()), 0o755); err != nil {
		t.Fatal(err)
	}
	held, err := state.TryLockPath(state.BootstrapLockPath())
	if err != nil {
		t.Fatal(err)
	}
	defer held.Release()

	_, err = runInitArgs(t, "--install-self", dir)
	if exitCodeOf(err) != exitInstallBusy || !strings.Contains(err.Error(), "another Pair install is running") {
		t.Fatalf("init = %v, want exit 4", err)
	}
	home, _ := os.UserHomeDir()
	if _, err := os.Stat(filepath.Join(home, ".paircli", "bin", "paircli")); !os.IsNotExist(err) {
		t.Error("binary installed while bootstrap held the lock")
	}
}

// From a directory install.sh did not make, only the binary is removed.
func TestInit_InstallSelfRemovesOnlyAnInstallerDirectory(t *testing.T) {
	initEnv(t)
	dir := filepath.Join(t.TempDir(), "Downloads")
	exe := filepath.Join(dir, "paircli")
	if err := os.Mkdir(dir, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(exe, []byte("#!/bin/sh\n"), 0o700); err != nil {
		t.Fatal(err)
	}
	initExecutable = func() (string, error) { return exe, nil }
	keep := filepath.Join(dir, "notes.txt")
	if err := os.WriteFile(keep, []byte("mine"), 0o600); err != nil {
		t.Fatal(err)
	}
	initExec = func(string, []string, []string) error { return nil }
	if _, err := runInitArgs(t, "--install-self", dir); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(keep); err != nil {
		t.Errorf("a user file next to the binary was removed: %v", err)
	}
	if _, err := os.Stat(exe); !os.IsNotExist(err) {
		t.Errorf("the source binary was not removed: %v", err)
	}
}

func TestInit_KeepsAnUnreadableInitJSON(t *testing.T) {
	initEnv(t)
	if err := os.MkdirAll(filepath.Dir(initRecordPath()), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(initRecordPath(), []byte("{not json"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := runInitArgs(t); err == nil || !strings.Contains(err.Error(), "init.json is unreadable") {
		t.Fatalf("init = %v, want the unreadable init.json named", err)
	}
	if got, _ := os.ReadFile(initRecordPath()); string(got) != "{not json" {
		t.Errorf("init.json overwritten: %q", got)
	}
}

func TestInit_HarnessRerunKeepsTheOtherAgents(t *testing.T) {
	initEnv(t)
	if _, err := runInitArgs(t); err != nil {
		t.Fatal(err)
	}
	if _, err := runInitArgs(t, "--harness", "codex"); err != nil {
		t.Fatal(err)
	}
	if rec, _ := readInitRecord(); rec.Harnesses["claude-code"] != "installed" || rec.Harnesses["codex"] == "" {
		t.Errorf("harnesses = %v, want both kept", rec.Harnesses)
	}
}

func TestInit_InstallSelfRefusesADirectoryItDoesNotRunFrom(t *testing.T) {
	initEnv(t)
	fakeInstaller(t)
	other := t.TempDir()
	if _, err := runInitArgs(t, "--install-self", other); err == nil {
		t.Fatal("init removed a directory it was not started from")
	}
	if _, err := os.Stat(other); err != nil {
		t.Errorf("directory removed: %v", err)
	}
}

// A Homebrew paircli records itself in install.json, so the plugin's
// bootstrap uses it instead of downloading a second copy; the installer's
// own binary removes the record.
func TestInit_RecordsOnlyAHomebrewBinary(t *testing.T) {
	initEnv(t)
	brew := "/opt/homebrew/Cellar/pair/0.6.0/bin/paircli" // no bin link in the test: the keg path itself
	initBinaryPath = func() (string, error) { return brew, nil }
	out, err := runInitArgs(t, "--no-login")
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(out, "~/.paircli/bin") || !strings.Contains(out, "Uninstall any time: paircli uninstall") ||
		!strings.Contains(out, "brew upgrade") || strings.Contains(out, "automatic updates") {
		t.Errorf("output for a Homebrew paircli:\n%s", out)
	}
	var rec installRecord
	data, err := os.ReadFile(installRecordPath())
	if err != nil || json.Unmarshal(data, &rec) != nil || rec.Binary != brew {
		t.Fatalf("install.json = %s (%v), want %s", data, err, brew)
	}

	home, _ := os.UserHomeDir()
	initBinaryPath = func() (string, error) { return filepath.Join(home, ".paircli", "bin", "paircli"), nil }
	if _, err := runInitArgs(t); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(installRecordPath()); !os.IsNotExist(err) {
		t.Errorf("install.json kept for the binary in ~/.paircli/bin: %v", err)
	}

	// A binary that isn't Homebrew's (a dev build, a downloaded copy) is never recorded.
	initBinaryPath = func() (string, error) { return filepath.Join(t.TempDir(), "paircli"), nil }
	if _, err := runInitArgs(t); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(installRecordPath()); !os.IsNotExist(err) {
		t.Errorf("install.json written for a non-Homebrew binary: %v", err)
	}
}

// Only a person at a terminal re-running init asks again for Codex trust they
// revoked; an MDM's recurring run (no terminal) must not override it (D1).
func TestInit_RegrantsRevokedTrustOnlyFromATerminal(t *testing.T) {
	for _, tty := range []bool{false, true} {
		_, trustCalls := initEnv(t)
		initHasTTY = func() bool { return tty }
		if _, err := runInitArgs(t); err != nil {
			t.Fatal(err)
		}
		if len(*trustCalls) == 0 || (*trustCalls)[len(*trustCalls)-1] != tty {
			t.Errorf("tty=%v: trust calls = %v, want explicit=%v", tty, *trustCalls, tty)
		}
	}
}
