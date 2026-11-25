package main

import (
	"bytes"
	"context"
	"errors"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/interviewstreet/pair-plugin/cli/internal/maintain"
	"github.com/interviewstreet/pair-plugin/cli/internal/state"
)

// maintainEnv is an enrolled machine (init.json) with every step faked.
func maintainEnv(t *testing.T) (calls *[]string, trustCalls *[]bool) {
	t.Helper()
	calls, trustCalls = initEnv(t)
	if err := writeInitRecord(initRecord{Harnesses: map[string]string{"claude-code": "installed"}}); err != nil {
		t.Fatal(err)
	}
	origUpdate, origExec, origExe := maintainUpdate, maintainExec, maintainExecutable
	t.Cleanup(func() { maintainUpdate, maintainExec, maintainExecutable = origUpdate, origExec, origExe })
	maintainUpdate = func(_ context.Context, pluginVersion string) (string, error) {
		*calls = append(*calls, "maintain-update:"+pluginVersion)
		return "current (0.6.0)", nil
	}
	maintainExec = func(string, []string, []string) error { t.Fatal("maintain exec'd without an update"); return nil }
	maintainExecutable = func() (string, error) { return "/home/u/.paircli/bin/paircli", nil }
	return calls, trustCalls
}

func stamped() bool {
	_, err := os.Stat(maintain.StampPath())
	return err == nil
}

func TestMaintain_DoesNothingOnAMachineInitDidNotEnroll(t *testing.T) {
	calls := setupEnv(t)
	if err := maintainRun(nil, &bytes.Buffer{}); err != nil {
		t.Fatal(err)
	}
	if len(*calls) != 0 || stamped() {
		t.Errorf("maintain ran on a plugin-only machine: %v", *calls)
	}
}

func TestMaintain_SkipsWhileSetupRunsAndLeavesTheStampUnset(t *testing.T) {
	calls, _ := maintainEnv(t)
	lock, err := state.TryLock("setup")
	if err != nil {
		t.Fatal(err)
	}
	defer lock.Release()
	if err := maintainRun(nil, &bytes.Buffer{}); err != nil {
		t.Fatal(err)
	}
	if len(*calls) != 0 || stamped() {
		t.Errorf("maintain ran (%v) or stamped while setup held the lock", *calls)
	}
}

func TestMaintain_RepairsHooksAndTrustWithoutAPluginVersion(t *testing.T) {
	calls, trustCalls := maintainEnv(t)
	if err := maintainRun(nil, &bytes.Buffer{}); err != nil {
		t.Fatal(err)
	}
	if len(*calls) == 0 || (*calls)[0] != "maintain-update:" {
		t.Errorf("calls = %v, want the update with no plugin version first", *calls)
	}
	if len(*trustCalls) == 0 || (*trustCalls)[len(*trustCalls)-1] {
		t.Errorf("trust calls = %v, want a non-explicit grant (revoked keys stay revoked)", *trustCalls)
	}
	if !stamped() {
		t.Error("maintain.json not written")
	}
	if rec, _ := readInitRecord(); rec.Harnesses["codex"] != "installed" {
		t.Errorf("init.json = %+v, want the newly detected codex recorded", rec)
	}
	if got := readSetupStatus().Steps["update"]; got != "current (0.6.0)" {
		t.Errorf("setup update step = %q, want maintain's own outcome", got)
	}
}

// A failed update is visible in setup.json (and so in status and doctor).
func TestMaintain_RecordsAFailedUpdate(t *testing.T) {
	maintainEnv(t)
	maintainUpdate = func(context.Context, string) (string, error) {
		return "", errors.New("download update: checksum verification failed")
	}
	if err := maintainRun(nil, &bytes.Buffer{}); err != nil {
		t.Fatal(err)
	}
	if got := readSetupStatus().Steps["update"]; got != "skipped: download update: checksum verification failed" {
		t.Errorf("setup update step = %q", got)
	}
}

func TestMaintain_LogsInWithoutACredential(t *testing.T) {
	calls, _ := maintainEnv(t)
	if err := maintainRun(nil, &bytes.Buffer{}); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(strings.Join(*calls, ","), "login:") {
		t.Errorf("calls = %v, want a login on a machine with no credential", *calls)
	}

}

// Two runs started together: the second finds the first's stamp and stops.
func TestMaintain_ARunThatIsNoLongerDueStops(t *testing.T) {
	calls, _ := maintainEnv(t)
	if err := maintain.WriteStamp(time.Now()); err != nil {
		t.Fatal(err)
	}
	if err := maintainRun(nil, &bytes.Buffer{}); err != nil {
		t.Fatal(err)
	}
	if len(*calls) != 0 {
		t.Errorf("calls = %v, want none after a fresh stamp", *calls)
	}
}

func TestMaintain_AfterUpdateNeedsTheOldVersion(t *testing.T) {
	maintainEnv(t)
	if err := maintainRun([]string{"--after-update"}, &bytes.Buffer{}); err == nil {
		t.Error("--after-update without a version accepted")
	}
}

// After an update, maintain releases the setup lock, then runs the new
// binary's hook and trust steps.
func TestMaintain_ReleasesTheLockBeforeReExecAfterAnUpdate(t *testing.T) {
	calls, _ := maintainEnv(t)
	maintainUpdate = func(context.Context, string) (string, error) { return "updated 0.6.0 to 0.7.0", nil }
	var execd []string
	maintainExec = func(path string, argv, _ []string) error {
		lock, err := state.TryLock("setup")
		if err != nil {
			t.Errorf("setup lock still held at exec: %v", err)
		} else {
			lock.Release()
		}
		execd = argv
		return nil
	}
	if err := maintainRun(nil, &bytes.Buffer{}); err != nil {
		t.Fatal(err)
	}
	if strings.Join(execd, " ") != "/home/u/.paircli/bin/paircli maintain --after-update "+version {
		t.Errorf("exec = %v", execd)
	}
	if len(*calls) != 0 || stamped() {
		t.Errorf("the old binary ran steps (%v) or stamped before the re-exec", *calls)
	}

	maintainUpdate = func(context.Context, string) (string, error) { return "", errors.New("must not update again") }
	if err := maintainRun([]string{"--after-update", "0.6.0"}, &bytes.Buffer{}); err != nil {
		t.Fatal(err)
	}
	if !stamped() || readSetupStatus().Steps["codex_trust"] == "" {
		t.Errorf("--after-update did not run the hook and trust steps: %+v", readSetupStatus())
	}
	if got := readSetupStatus().Steps["update"]; got != "updated 0.6.0 to "+version {
		t.Errorf("setup update step = %q, want the update recorded", got)
	}
}

// maintain keeps to the one agent `init --harness` chose.
func TestMaintain_KeepsInitsHarnessScope(t *testing.T) {
	calls, _ := maintainEnv(t)
	if _, err := runInitArgs(t, "--harness", "codex", "--no-login"); err != nil {
		t.Fatal(err)
	}
	if rec, _ := readInitRecord(); rec.Scope != "codex" {
		t.Fatalf("init.json scope = %q, want codex", rec.Scope)
	}
	*calls = nil
	if err := maintainRun(nil, &bytes.Buffer{}); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(strings.Join(*calls, ","), "hooks:codex") {
		t.Errorf("calls = %v, want hooks installed for codex only", *calls)
	}
}
