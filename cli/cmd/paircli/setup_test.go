package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/interviewstreet/pair-plugin/cli/internal/deviceauth"
	"github.com/interviewstreet/pair-plugin/cli/internal/state"
)

// setupEnv points every path setup and status touch at a temp dir and returns
// a call log the fake steps append to.
func setupEnv(t *testing.T) *[]string {
	t.Helper()
	root := t.TempDir()
	t.Setenv("HOME", root)
	t.Setenv("CODEX_HOME", filepath.Join(root, ".codex"))
	t.Setenv("PAIRCLI_PI_DIR", filepath.Join(root, ".pi", "agent"))
	t.Setenv(deviceauth.EnvFile, filepath.Join(root, ".paircli", "auth.json"))
	t.Setenv(deviceauth.EnvToken, "")
	t.Setenv(serverURLEnv, "https://pair.example")

	origReader := statusAnalysisReader
	t.Cleanup(func() { statusAnalysisReader = origReader })
	statusAnalysisReader = func() (analysisReader, bool) { return nil, false }

	calls := &[]string{}
	origUpdate, origHooks, origLogin, origNotifications := setupSelfUpdate, setupInstallHooks, setupEnsureLogin, setupNotifications
	t.Cleanup(func() {
		setupSelfUpdate, setupInstallHooks, setupEnsureLogin, setupNotifications = origUpdate, origHooks, origLogin, origNotifications
	})
	setupNotifications = func() (string, error) { return "ready", nil }
	setupSelfUpdate = func(_ context.Context, pluginVersion string) (string, error) {
		*calls = append(*calls, "update:"+pluginVersion)
		return "current", nil
	}
	setupInstallHooks = func(_ io.Writer, harness string) (string, error) {
		*calls = append(*calls, "hooks:"+harness)
		return "installed", nil
	}
	setupEnsureLogin = func(server string, out, progress io.Writer) (deviceauth.StoredAuth, error) {
		*calls = append(*calls, "login:"+server)
		io.WriteString(progress, "First copy your one-time code: WDJP-7Q2K\n")
		return deviceauth.StoredAuth{}, nil
	}
	return calls
}

func runSetupArgs(t *testing.T, args ...string) (stdout, stderr string, err error) {
	t.Helper()
	var out, errOut bytes.Buffer
	err = setupRun(args, &out, &errOut)
	return out.String(), errOut.String(), err
}

func TestSetup_RunsEveryStepInOrder(t *testing.T) {
	calls := setupEnv(t)
	stdout, _, err := runSetupArgs(t, "--harness", "claude-code", "--plugin-version", "0.3.0")
	if err != nil {
		t.Fatalf("setup: %v", err)
	}
	want := []string{"update:0.3.0", "hooks:claude-code", "login:https://pair.example"}
	if strings.Join(*calls, ",") != strings.Join(want, ",") {
		t.Errorf("calls = %v, want %v", *calls, want)
	}
	if strings.Count(strings.TrimSpace(stdout), "\n") != 0 || !strings.Contains(stdout, "setup complete") {
		t.Errorf("stdout = %q, want one summary line", stdout)
	}
	got := readSetupStatus()
	if got.State != setupIdle || got.Error != nil || got.Steps["login"] != "logged in" {
		t.Errorf("setup.json = %+v", got)
	}
}

func TestSetup_WithoutHarnessInstallsDetected(t *testing.T) {
	calls := setupEnv(t)
	if _, _, err := runSetupArgs(t); err != nil {
		t.Fatal(err)
	}
	if (*calls)[1] != "hooks:" {
		t.Errorf("calls = %v, want hooks installed with no harness named", *calls)
	}
}

func TestSetup_NoLoginSkipsLogin(t *testing.T) {
	calls := setupEnv(t)
	if _, _, err := runSetupArgs(t, "--no-login"); err != nil {
		t.Fatal(err)
	}
	for _, c := range *calls {
		if strings.HasPrefix(c, "login:") {
			t.Errorf("login ran with --no-login: %v", *calls)
		}
	}
	if got := readSetupStatus().Steps["login"]; !strings.HasPrefix(got, "skipped") {
		t.Errorf("login step = %q, want skipped", got)
	}
}

func TestSetup_RejectsBadArguments(t *testing.T) {
	calls := setupEnv(t)
	for _, args := range [][]string{{"--harness", "vim"}, {"extra"}, {"--bogus"}} {
		if _, _, err := runSetupArgs(t, args...); err == nil {
			t.Errorf("setup %v succeeded, want an error", args)
		}
	}
	if len(*calls) != 0 {
		t.Errorf("steps ran for invalid arguments: %v", *calls)
	}
}

func TestSetup_UpdateFailureIsTolerated(t *testing.T) {
	setupEnv(t)
	setupSelfUpdate = func(context.Context, string) (string, error) {
		return "", errors.New("paircli is managed by Homebrew")
	}
	if _, _, err := runSetupArgs(t); err != nil {
		t.Fatalf("setup failed on a tolerated update error: %v", err)
	}
	got := readSetupStatus()
	if got.State != setupIdle || !strings.Contains(got.Steps["update"], "Homebrew") {
		t.Errorf("setup.json = %+v", got)
	}
}

func TestSetup_PreparesNotificationsBeforeLogin(t *testing.T) {
	calls := setupEnv(t)
	setupNotifications = func() (string, error) {
		*calls = append(*calls, "notifications")
		return "Pair notifications installed", nil
	}
	if _, _, err := runSetupArgs(t); err != nil {
		t.Fatal(err)
	}
	if got := strings.Join(*calls, ","); got != "update:,hooks:,notifications,login:https://pair.example" {
		t.Fatalf("setup order = %s", got)
	}
}

func TestSetup_NotificationFailureDoesNotBlockSetup(t *testing.T) {
	setupEnv(t)
	setupNotifications = func() (string, error) { return "", errors.New("codesign failed") }
	if _, _, err := runSetupArgs(t); err != nil {
		t.Fatal(err)
	}
	status := readSetupStatus()
	if status.State != setupIdle || !strings.Contains(status.Steps["notifications"], "codesign failed") || status.Steps["login"] != "logged in" {
		t.Fatalf("notification failure blocked setup: %+v", status)
	}
}

func TestSetup_FailureRecordsTheRealErrorAndTheNextRunRetries(t *testing.T) {
	calls := setupEnv(t)
	setupEnsureLogin = func(string, io.Writer, io.Writer) (deviceauth.StoredAuth, error) {
		return deviceauth.StoredAuth{}, errors.New("device code expired, run paircli login again")
	}
	stdout, _, err := runSetupArgs(t)
	if err == nil {
		t.Fatal("setup succeeded, want a failure")
	}
	failed := readSetupStatus()
	if failed.State != setupFailed || failed.Error == nil || !strings.Contains(*failed.Error, "device code expired") {
		t.Fatalf("setup.json = %+v, want failed with the login error", failed)
	}
	if !strings.Contains(stdout, "device code expired") {
		t.Errorf("stdout = %q, want the real error", stdout)
	}

	// Failure is not sticky: the next run goes through every step again.
	*calls = nil
	setupEnsureLogin = func(string, io.Writer, io.Writer) (deviceauth.StoredAuth, error) {
		return deviceauth.StoredAuth{}, nil
	}
	if _, _, err := runSetupArgs(t); err != nil {
		t.Fatalf("retry: %v", err)
	}
	if len(*calls) != 2 { // update and hooks; login is the fake above
		t.Errorf("calls on retry = %v, want update and hooks again", *calls)
	}
	if got := readSetupStatus(); got.State != setupIdle || got.Error != nil {
		t.Errorf("setup.json after retry = %+v, want idle without an error", got)
	}
}

func TestSetup_HookFailureStillTriesLogin(t *testing.T) {
	calls := setupEnv(t)
	setupInstallHooks = func(io.Writer, string) (string, error) { return "", errors.New("settings.json is not valid JSON") }
	if _, _, err := runSetupArgs(t); err == nil {
		t.Fatal("setup succeeded, want a failure")
	}
	if got := readSetupStatus(); got.State != setupFailed || !strings.Contains(*got.Error, "settings.json") {
		t.Errorf("setup.json = %+v", got)
	}
	if len(*calls) != 2 || !strings.HasPrefix((*calls)[1], "login:") {
		t.Errorf("calls = %v, want login attempted after the hook failure", *calls)
	}
}

func TestSetup_SingleFlight(t *testing.T) {
	calls := setupEnv(t)
	lock, err := state.TryLock("setup")
	if err != nil {
		t.Fatal(err)
	}
	defer lock.Release()

	stdout, _, err := runSetupArgs(t, "--json")
	if err != nil {
		t.Fatalf("second setup: %v", err)
	}
	if len(*calls) != 0 {
		t.Errorf("steps ran while another setup held the lock: %v", *calls)
	}
	var got setupStatus
	if err := json.Unmarshal([]byte(stdout), &got); err != nil || got.State != setupRunning {
		t.Errorf("stdout = %q (%v), want the running state", stdout, err)
	}
	if _, err := os.Stat(setupStatusPath()); err == nil {
		t.Error("the losing run wrote setup.json")
	}
}

func TestSetup_ReleasesTheLock(t *testing.T) {
	setupEnv(t)
	if _, _, err := runSetupArgs(t); err != nil {
		t.Fatal(err)
	}
	lock, err := state.TryLock("setup")
	if err != nil {
		t.Fatalf("lock still held after setup: %v", err)
	}
	lock.Release()
}

func TestSetup_JSONOutputAndProgress(t *testing.T) {
	setupEnv(t)
	stdout, stderr, err := runSetupArgs(t, "--json")
	if err != nil {
		t.Fatal(err)
	}
	var got map[string]any
	if err := json.Unmarshal([]byte(stdout), &got); err != nil {
		t.Fatalf("stdout is not JSON: %v (%q)", err, stdout)
	}
	for _, key := range []string{"schema_version", "state", "error", "updated_at", "log", "steps"} {
		if _, ok := got[key]; !ok {
			t.Errorf("missing %q in %v", key, got)
		}
	}
	if !strings.Contains(stderr, "WDJP-7Q2K") {
		t.Errorf("stderr = %q, want the login code as progress", stderr)
	}
	logPath, _ := got["log"].(string)
	data, err := os.ReadFile(logPath)
	if err != nil || !strings.Contains(string(data), "WDJP-7Q2K") {
		t.Errorf("setup log %q = %q, %v, want the login code", logPath, data, err)
	}
}

func TestSetup_RunningIsWrittenBeforeTheSteps(t *testing.T) {
	setupEnv(t)
	var during string
	setupInstallHooks = func(io.Writer, string) (string, error) {
		during = readSetupStatus().State
		return "installed", nil
	}
	if _, _, err := runSetupArgs(t); err != nil {
		t.Fatal(err)
	}
	if during != setupRunning {
		t.Errorf("state during the run = %q, want running", during)
	}
}

// An older plugin still gets every step; setup only records a note, which
// status and doctor show.
func TestSetup_OldPluginRunsEveryStepAndRecordsANote(t *testing.T) {
	calls := setupEnv(t)
	orig := minPluginVersion
	t.Cleanup(func() { minPluginVersion = orig })
	minPluginVersion = "0.9.0"

	if _, _, err := runSetupArgs(t, "--harness", "codex", "--plugin-version", "0.4.1"); err != nil {
		t.Fatalf("setup: %v", err)
	}
	if got := strings.Join(*calls, ","); got != "update:0.4.1,hooks:codex,login:https://pair.example" {
		t.Errorf("calls = %s, want every step", got)
	}
	want := "update the Pair plugin (have 0.4.1, recommended >= 0.9.0)"
	if got := readSetupStatus(); got.State != setupIdle || got.Steps["plugin"] != want {
		t.Errorf("setup.json = %+v, want idle with plugin %q", got, want)
	}
	var out bytes.Buffer
	if err := statusRun(nil, &out); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(out.String(), want) {
		t.Errorf("status = %q, want the plugin note", out.String())
	}
}

// fakeInstallEach replaces the per-agent hook install with fixed outcomes.
func fakeInstallEach(t *testing.T, results ...harnessResult) {
	t.Helper()
	orig := setupInstallEach
	t.Cleanup(func() { setupInstallEach = orig })
	setupInstallEach = func(io.Writer) []harnessResult { return results }
}

func TestSetupSteps_PerAgentToleratesOneFailedAgent(t *testing.T) {
	setupEnv(t)
	fakeInstallEach(t,
		harnessResult{Name: "claude-code", Detected: true},
		harnessResult{Name: "codex", Detected: true, Err: errors.New("hooks.json is not valid JSON")},
		harnessResult{Name: "pi"},
	)
	var out, errOut bytes.Buffer
	if err := runSetupSteps(setupArgs{PerAgent: true}, &out, &errOut); err != nil {
		t.Fatalf("one failed agent failed setup: %v", err)
	}
	got := readSetupStatus()
	if got.State != setupIdle || got.Steps["hooks:claude-code"] != "installed" ||
		!strings.Contains(got.Steps["hooks:codex"], "not valid JSON") || got.Steps["hooks:pi"] != "" {
		t.Errorf("setup.json = %+v", got)
	}
	stubDoctor(t, nil, errors.New("not found"))
	var doctor bytes.Buffer
	if err := doctorReport(&doctor, time.Now()); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(doctor.String(), "codex hooks failed") {
		t.Errorf("doctor does not show the failed agent:\n%s", doctor.String())
	}
}

func TestSetupSteps_PerAgentFailsWhenEveryAgentFails(t *testing.T) {
	setupEnv(t)
	fakeInstallEach(t, harnessResult{Name: "codex", Detected: true, Err: errors.New("read-only file system")})
	var out, errOut bytes.Buffer
	if err := runSetupSteps(setupArgs{PerAgent: true}, &out, &errOut); err == nil {
		t.Fatal("setup succeeded with every agent failing")
	}
	if got := readSetupStatus(); got.State != setupFailed || !strings.Contains(*got.Error, "codex") {
		t.Errorf("setup.json = %+v", got)
	}
}

func TestSetupSteps_PerAgentFailsWithNoAgent(t *testing.T) {
	setupEnv(t)
	fakeInstallEach(t, harnessResult{Name: "claude-code"}, harnessResult{Name: "codex"})
	var out, errOut bytes.Buffer
	if err := runSetupSteps(setupArgs{PerAgent: true}, &out, &errOut); err == nil {
		t.Fatal("setup succeeded with no agent detected")
	}
}

// The plugin's bootstrap.py drives every CLI version this way. A change that
// breaks this test breaks every released plugin (0.4.0 and later).
func TestBootstrapContract_SetupAndStatusJSON(t *testing.T) {
	setupEnv(t)
	if _, _, err := runSetupArgs(t, "--harness", "claude-code", "--plugin-version", "0.4.0"); err != nil {
		t.Fatalf("setup --harness claude-code --plugin-version 0.4.0: %v", err)
	}
	var out bytes.Buffer
	if err := statusRun([]string{"--json"}, &out); err != nil {
		t.Fatal(err)
	}
	// The fields plugins/pair/hooks/bootstrap.py status_line() reads.
	var status struct {
		LoggedIn   *bool            `json:"logged_in"`
		Hooks      map[string]*bool `json:"hooks"`
		CLIVersion *string          `json:"cli_version"`
		Setup      *struct {
			Log *string `json:"log"`
		} `json:"setup"`
	}
	if err := json.Unmarshal(out.Bytes(), &status); err != nil {
		t.Fatalf("status --json: %v\n%s", err, out.String())
	}
	if got := readSetupStatus().Steps["plugin"]; got != "" {
		t.Errorf("plugin 0.4.0 got a note: %q", got)
	}
	if status.LoggedIn == nil || status.CLIVersion == nil || *status.CLIVersion == "" ||
		status.Hooks["claude-code"] == nil || status.Hooks["codex"] == nil ||
		status.Setup == nil || status.Setup.Log == nil || *status.Setup.Log == "" {
		t.Errorf("status --json is missing a field bootstrap.py reads:\n%s", out.String())
	}
}
