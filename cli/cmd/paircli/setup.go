package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/interviewstreet/pair-plugin/cli/internal/codex"
	"github.com/interviewstreet/pair-plugin/cli/internal/notify"
	"github.com/interviewstreet/pair-plugin/cli/internal/selfupdate"
	"github.com/interviewstreet/pair-plugin/cli/internal/state"
)

// Setup states, as written to setup.json and shown by `paircli status`.
const (
	setupIdle    = "idle" // no run in progress; the last run (if any) succeeded
	setupRunning = "running"
	setupFailed  = "failed"
)

// setupStatus is setup.json: the outcome of the latest `paircli setup` run.
// Every run overwrites it, so a failure is visible with its real error until
// the next run replaces it; it never blocks a retry.
type setupStatus struct {
	SchemaVersion int               `json:"schema_version"`
	State         string            `json:"state"`
	Error         *string           `json:"error"`
	UpdatedAt     time.Time         `json:"updated_at"`
	Log           string            `json:"log"`
	Steps         map[string]string `json:"steps,omitempty"`
}

func setupStatusPath() string { return filepath.Join(state.Dir(), "setup.json") }

// readSetupStatus loads setup.json. A missing or unreadable file is idle.
func readSetupStatus() setupStatus {
	var s setupStatus
	data, err := os.ReadFile(setupStatusPath())
	if err != nil || json.Unmarshal(data, &s) != nil || s.State == "" {
		return setupStatus{SchemaVersion: state.SchemaVersion, State: setupIdle}
	}
	return s
}

func writeSetupStatus(s setupStatus) error {
	s.SchemaVersion = state.SchemaVersion
	s.UpdatedAt = time.Now().UTC()
	return state.WriteJSONAtomic(setupStatusPath(), s)
}

// The steps of a setup run, replaceable so tests need no network, no real
// hook config and no browser.
var (
	setupSelfUpdate    = selfUpdateQuietly
	setupInstallHooks  = installHooksFor
	setupInstallEach   = installEachHarness
	setupCodexTrust    = grantCodexTrust
	setupEnsureLogin   = ensureLogin
	setupNotifications = notify.Prepare
)

// minPluginVersion is the oldest Pair plugin this CLI recommends. An older
// plugin still gets every setup step (the bootstrap contract is stable); setup
// only records a note that status and doctor show.
var minPluginVersion = "0.4.0"

// setupArgs is a parsed `paircli setup` invocation, plus the options only
// init and maintain set.
type setupArgs struct {
	Harness       string
	PluginVersion string
	NoLogin       bool
	JSON          bool

	// GrantCodexTrust approves Pair's Codex hooks inside Codex after
	// installing them (D1). Only init and maintain pass it; the plugin's
	// `setup --harness …` never does.
	GrantCodexTrust bool
	// Enroll writes ~/.paircli/state/init.json once the hooks and the login
	// succeeded, which turns on maintain for this machine.
	Enroll bool

	// PerAgent records each detected agent's hook outcome as its own step
	// ("hooks:<harness>") and fails the hooks step only when every detected
	// agent failed. `setup` keeps the all-or-nothing hooks step.
	PerAgent bool
	// ExplicitTrust grants Codex trust the user revoked: they ran init.
	ExplicitTrust bool
	// NoBrowser prints the login URL without opening a browser.
	NoBrowser bool
	// Progress, when set, receives the login's code and URL and Report's
	// lines instead of stderr (which then only feeds the log).
	Progress io.Writer
	// Report, when set, is called after each step with the step's name and
	// every outcome recorded so far.
	Report func(step string, steps map[string]string)
}

func parseSetupArgs(args []string) (setupArgs, error) {
	var a setupArgs
	fs := flag.NewFlagSet("setup", flag.ContinueOnError)
	fs.SetOutput(io.Discard) // setupRun reports parse errors itself
	fs.StringVar(&a.Harness, "harness", "", "claude-code | codex | pi (default: every detected harness)")
	fs.StringVar(&a.PluginVersion, "plugin-version", "", "Pair plugin version (used by the plugin integration)")
	fs.BoolVar(&a.NoLogin, "no-login", false, "skip the device login")
	fs.BoolVar(&a.JSON, "json", false, "print the result as JSON")
	fs.BoolVar(&a.GrantCodexTrust, "grant-codex-trust", false, "approve Pair's Codex hooks inside Codex")
	fs.BoolVar(&a.Enroll, "enroll", false, "record this machine as set up by the installer (turns on maintain)")
	if err := fs.Parse(args); err != nil {
		return a, err
	}
	if fs.NArg() > 0 {
		return a, fmt.Errorf("unexpected argument %q", fs.Arg(0))
	}
	if a.Harness != "" {
		if _, ok := harnessTable[a.Harness]; !ok {
			return a, fmt.Errorf("unknown harness %q (want claude-code | codex | pi)", a.Harness)
		}
	}
	return a, nil
}

func runSetup(args []string) error { return setupRun(args, os.Stdout, os.Stderr) }

// setupRun makes this machine ready for `paircli publish`: it updates the CLI,
// installs the agent hooks and logs in. It is safe to run any number of times
// and to start from a hook: a second run while one is in progress exits at
// once. It can take minutes, because the login waits for the user.
//
// stderr (and the run's log file) carry progress, including the login code
// and URL; stdout carries only the one-line result, or the JSON with --json.
func setupRun(args []string, stdout, stderr io.Writer) error {
	a, err := parseSetupArgs(args)
	if err != nil {
		return err
	}
	lock, err := state.TryLock("setup")
	if errors.Is(err, state.ErrLocked) {
		return printSetupResult(stdout, a.JSON, "setup already running", readSetupRunning())
	}
	if err != nil {
		return err
	}
	defer lock.Release()
	return runSetupSteps(a, stdout, stderr)
}

// runSetupSteps runs setup's steps and records them in setup.json. The caller
// holds the setup lock: setup takes it with TryLock, init waits for it.
func runSetupSteps(a setupArgs, stdout, stderr io.Writer) error {
	logPath, logFile := openSetupLog()
	progress := a.Progress
	if logFile != nil {
		defer logFile.Close()
		stderr = io.MultiWriter(stderr, logFile)
		if progress != nil {
			progress = io.MultiWriter(progress, logFile)
		}
	}
	if progress == nil {
		progress = stderr
	}
	status := setupStatus{State: setupRunning, Log: logPath, Steps: map[string]string{}}
	if err := writeSetupStatus(status); err != nil {
		fmt.Fprintf(stderr, "paircli: could not write setup status: %v\n", err)
	}

	var failures []string
	step := func(name string, run func() (string, error), tolerate bool) {
		fmt.Fprintf(stderr, "paircli: setup: %s\n", name)
		outcome, err := run()
		defer func() {
			if a.Report != nil {
				a.Report(name, status.Steps)
			}
		}()
		switch {
		case err == nil:
			status.Steps[name] = outcome
		case tolerate:
			status.Steps[name] = "skipped: " + err.Error()
			fmt.Fprintf(stderr, "paircli: setup: %s skipped: %v\n", name, err)
		default:
			status.Steps[name] = "failed: " + err.Error()
			failures = append(failures, name+": "+err.Error())
			fmt.Fprintf(stderr, "paircli: setup: %s failed: %v\n", name, err)
		}
	}

	if note := pluginNote(a.PluginVersion); note != "" {
		status.Steps["plugin"] = note
	}
	step("update", func() (string, error) { return setupSelfUpdate(context.Background(), a.PluginVersion) }, true)
	if a.PerAgent && a.Harness == "" {
		step("hooks", func() (string, error) { return installHooksPerAgent(stderr, status.Steps) }, false)
	} else {
		step("hooks", func() (string, error) { return setupInstallHooks(stderr, a.Harness) }, false)
	}
	var enrolment initRecord
	var enrolmentErr error
	if a.GrantCodexTrust || a.Enroll {
		if enrolment, enrolmentErr = readInitRecord(); errors.Is(enrolmentErr, os.ErrNotExist) {
			enrolmentErr = nil
		}
	}
	if a.GrantCodexTrust && codexHooksInstalled(a, status.Steps) {
		step("codex_trust", func() (string, error) {
			outcome, trusted := setupCodexTrust(&enrolment, a.ExplicitTrust)
			if !trusted && a.Report == nil { // init's report prints it in context
				fmt.Fprintln(progress, codex.InstallMessage())
			}
			return outcome, nil
		}, true)
	}
	step("notifications", setupNotifications, true)
	if a.NoLogin {
		status.Steps["login"] = "skipped: --no-login"
		if a.Report != nil {
			a.Report("login", status.Steps)
		}
	} else {
		step("login", func() (string, error) {
			if _, err := setupEnsureLogin(resolveServer(""), progress, progress, a.NoBrowser); err != nil {
				return "", err
			}
			return "logged in", nil
		}, false)
	}
	switch {
	case enrolmentErr != nil && (a.Enroll || initEnrolled()):
		// Never replace an init.json we could not read: it holds the user's
		// Codex revocations.
		msg := fmt.Sprintf("init.json is unreadable (%v); remove %s and run init again", enrolmentErr, initRecordPath())
		status.Steps["enroll"] = "failed: " + msg
		if a.Enroll {
			failures = append(failures, "enroll: "+msg)
		}
	case a.Enroll && len(failures) == 0:
		if enrolment.Harnesses == nil {
			enrolment.Harnesses = map[string]string{}
		}
		for _, name := range harnessOrder {
			if outcome := status.Steps["hooks:"+name]; outcome != "" {
				enrolment.Harnesses[name] = outcome
			}
		}
		if a.Harness != "" {
			enrolment.Harnesses[a.Harness] = status.Steps["hooks"]
		}
		if t := status.Steps["codex_trust"]; t != "" {
			enrolment.CodexTrust = t
		}
		if err := writeInitRecord(enrolment); err != nil {
			failures = append(failures, "enroll: "+err.Error())
			status.Steps["enroll"] = "failed: " + err.Error()
		} else {
			status.Steps["enroll"] = "enrolled"
		}
	case a.GrantCodexTrust && initEnrolled():
		// Keep what this run learned about Codex trust, even when another
		// step failed.
		enrolment.CodexTrust = status.Steps["codex_trust"]
		if err := writeInitRecord(enrolment); err != nil {
			fmt.Fprintf(stderr, "paircli: could not update init.json: %v\n", err)
		}
	}

	status.State = setupIdle
	summary := "setup complete"
	if len(failures) > 0 {
		msg := strings.Join(failures, "; ")
		status.State, status.Error = setupFailed, &msg
		summary = "setup failed: " + msg
	}
	if err := writeSetupStatus(status); err != nil {
		fmt.Fprintf(stderr, "paircli: could not write setup status: %v\n", err)
	}
	if err := printSetupResult(stdout, a.JSON, summary, status); err != nil {
		return err
	}
	if len(failures) > 0 {
		return errors.New("setup failed")
	}
	return nil
}

// codexHooksInstalled reports whether this run installed Codex's hooks, the
// precondition for granting their trust.
func codexHooksInstalled(a setupArgs, steps map[string]string) bool {
	if a.Harness != "" {
		return a.Harness == "codex" && !strings.HasPrefix(steps["hooks"], "failed")
	}
	if a.PerAgent {
		return steps["hooks:codex"] == "installed"
	}
	return harnessTable["codex"].Detect() && !strings.HasPrefix(steps["hooks"], "failed")
}

// pluginNote is the "plugin" step: a note when the plugin that started setup
// is older than this CLI recommends, "" otherwise. It never fails setup.
func pluginNote(pluginVersion string) string {
	if pluginVersion == "" || selfupdate.VersionAtLeast(pluginVersion, minPluginVersion) {
		return ""
	}
	return fmt.Sprintf("update the Pair plugin (have %s, recommended >= %s)", pluginVersion, minPluginVersion)
}

// installHooksPerAgent installs every detected agent's hooks, recording each
// one as a "hooks:<harness>" step. One agent failing does not fail the step
// while another succeeded: maintain retries the failed one later.
func installHooksPerAgent(w io.Writer, steps map[string]string) (string, error) {
	var installed, failed []string
	for _, r := range setupInstallEach(w) {
		switch {
		case !r.Detected:
			continue
		case r.Err != nil:
			steps["hooks:"+r.Name] = "failed: " + r.Err.Error()
			failed = append(failed, r.Name)
		default:
			steps["hooks:"+r.Name] = "installed"
			installed = append(installed, r.Name)
		}
	}
	switch {
	case len(installed) == 0 && len(failed) == 0:
		return "", errNoHarnesses
	case len(installed) == 0:
		return "", fmt.Errorf("hook install failed for %s", strings.Join(failed, ", "))
	case len(failed) > 0:
		return fmt.Sprintf("installed for %s; failed for %s", strings.Join(installed, ", "), strings.Join(failed, ", ")), nil
	}
	return "installed for " + strings.Join(installed, ", "), nil
}

// readSetupRunning is the status reported to a run that lost the lock race.
func readSetupRunning() setupStatus {
	s := readSetupStatus()
	s.State = setupRunning
	return s
}

func printSetupResult(w io.Writer, jsonOut bool, summary string, s setupStatus) error {
	if jsonOut {
		s.SchemaVersion = state.SchemaVersion
		enc := json.NewEncoder(w)
		enc.SetIndent("", "  ")
		return enc.Encode(s)
	}
	_, err := fmt.Fprintf(w, "paircli: %s\n", summary)
	return err
}

// openSetupLog creates this run's log file. It returns no file when the log
// directory is unusable: a missing log must not stop setup.
func openSetupLog() (string, *os.File) {
	dir := state.LogDir()
	if dir == "" || os.MkdirAll(dir, 0o700) != nil {
		return "", nil
	}
	path := filepath.Join(dir, "setup-"+time.Now().Format("20060102-150405")+".log")
	f, err := os.OpenFile(path, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o600)
	if err != nil {
		return "", nil
	}
	return path, f
}

// selfUpdateQuietly installs a newer paircli when there is one. Its failures
// (offline, Homebrew-managed, plugin too old) never fail setup, so the caller
// tolerates its error.
func selfUpdateQuietly(ctx context.Context, pluginVersion string) (string, error) {
	result, err := selfupdate.Run(ctx, selfupdate.Options{
		CurrentVersion: version,
		ManifestURL:    os.Getenv("PAIRCLI_UPDATE_MANIFEST_URL"),
		PluginVersion:  pluginVersion,
		AllowInsecure:  os.Getenv("PAIRCLI_ALLOW_INSECURE_DOWNLOAD") == "1",
	})
	switch {
	case err != nil:
		return "", err
	case result.SkippedReason != "":
		return "skipped: " + result.SkippedReason, nil
	case result.Updated:
		return fmt.Sprintf("updated %s to %s", result.CurrentVersion, result.LatestVersion), nil
	}
	return "current (" + result.CurrentVersion + ")", nil
}

// installHooksFor installs the named harness's hooks, or every detected
// harness's when none is named.
func installHooksFor(w io.Writer, harness string) (string, error) {
	if harness == "" {
		return "installed for detected harnesses", installAllHarnesses(w)
	}
	return "installed for " + harness, installHarness(w, harness)
}
