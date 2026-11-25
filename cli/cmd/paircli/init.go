package main

import (
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"time"

	"github.com/interviewstreet/pair-plugin/cli/internal/binpath"
	"github.com/interviewstreet/pair-plugin/cli/internal/claudecode"
	"github.com/interviewstreet/pair-plugin/cli/internal/codex"
	"github.com/interviewstreet/pair-plugin/cli/internal/deviceauth"
	"github.com/interviewstreet/pair-plugin/cli/internal/engine"
	"github.com/interviewstreet/pair-plugin/cli/internal/pi"
	"github.com/interviewstreet/pair-plugin/cli/internal/selfupdate"
	"github.com/interviewstreet/pair-plugin/cli/internal/state"
)

// Exit codes init uses besides 0 and 1.
const (
	exitSetupBusy   = 3 // another process holds the setup lock past initLockWait
	exitInstallBusy = 4 // another process holds .bootstrap.lock past installLockWait
)

// The seams between init and the machine, replaceable in tests.
var (
	// initLockWait is how long init waits for another setup run.
	initLockWait = 120 * time.Second
	// installLockWait covers bootstrap.py's 300 s download limit plus margin.
	installLockWait = 330 * time.Second
	initExec        = syscall.Exec
	initExecutable  = os.Executable
	initBinaryPath  = binpath.Executable
	initHasTTY      = hasTTY
	initGhState     = ghState
)

// initArgs is a parsed `paircli init` invocation.
type initArgs struct {
	InstallSelf  string
	AfterInstall bool
	Harness      string
	NoLogin      bool
}

func parseInitArgs(args []string) (initArgs, error) {
	var a initArgs
	fs := flag.NewFlagSet("init", flag.ContinueOnError)
	fs.SetOutput(io.Discard)
	fs.StringVar(&a.InstallSelf, "install-self", "", "install this binary to ~/.paircli/bin from DIR, then remove DIR (used by the installer)")
	fs.BoolVar(&a.AfterInstall, "after-install", false, "internal: re-run of init by the binary --install-self installed")
	fs.StringVar(&a.Harness, "harness", "", "claude-code | codex | pi (default: every detected harness)")
	fs.BoolVar(&a.NoLogin, "no-login", false, "skip the device login")
	// init always enrolls and grants Codex trust; the flags are accepted so
	// the installer can say so explicitly. --yes is a no-op: init never asks.
	fs.Bool("enroll", true, "record this machine as set up by init (always on)")
	fs.Bool("grant-codex-trust", true, "approve Pair's hooks inside Codex (always on)")
	fs.Bool("yes", false, "accepted for scripts; init never asks for confirmation")
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

func runInit(args []string) error { return initRun(args, os.Stderr) }

// initRun sets this machine up from the installer: the CLI, every detected
// agent's hooks, Codex approval of those hooks, the device login, and the
// enrolment that keeps them up to date. It never reads stdin (under
// `curl … | sh` that is the script), and it asks no question: it prints what
// it will set up, then does it (D3).
func initRun(args []string, w io.Writer) error {
	a, err := parseInitArgs(args)
	if err != nil {
		return err
	}
	if !a.AfterInstall {
		printInitSummary(w, a.Harness)
	}
	if a.InstallSelf != "" {
		bin, err := installSelf(a.InstallSelf)
		if err != nil {
			return err
		}
		fmt.Fprintf(w, "Pair: installed paircli %s to %s\n", version, engine.Home(filepath.Dir(bin)))
		argv := []string{bin, "init", "--after-install"}
		if a.Harness != "" {
			argv = append(argv, "--harness", a.Harness)
		}
		if a.NoLogin {
			argv = append(argv, "--no-login")
		}
		return initExec(bin, argv, os.Environ())
	}

	lock, err := state.WaitLock("setup", initLockWait)
	if errors.Is(err, state.ErrLocked) {
		log := readSetupStatus().Log
		if log == "" {
			log = "~/.paircli/logs/setup-*.log"
		}
		return &exitError{code: exitSetupBusy, err: fmt.Errorf("setup is already running in another process (log: %s); approve the login there, then re-run", log)}
	}
	if err != nil {
		return err
	}
	defer lock.Release()

	if err := recordInstallPath(); err != nil {
		fmt.Fprintf(w, "Pair: could not record the installed binary: %v\n", err)
	}
	r := &initReporter{w: w}
	// Only a person at a terminal re-running init is asking again for Codex
	// trust they revoked; an MDM's recurring run (no terminal) never is (D1).
	interactive := initHasTTY()
	err = runSetupSteps(setupArgs{
		Harness: a.Harness, NoLogin: a.NoLogin,
		GrantCodexTrust: true, Enroll: true, PerAgent: true, ExplicitTrust: interactive,
		NoBrowser: !interactive, Progress: w, Report: r.report,
	}, io.Discard, io.Discard)
	if err != nil {
		s := readSetupStatus()
		msg := "setup failed"
		if s.Error != nil {
			msg = *s.Error
		}
		return fmt.Errorf("Pair is not set up: %s (log: %s). Fix it and run %s init again", msg, s.Log, installedBinaryHint())
	}

	if a.NoLogin {
		fmt.Fprintf(w, "Pair: set up without logging in. The Pair login opens at your next agent session, or run %s login.\n", installedBinaryHint())
	}
	switch initGhState() {
	case "missing":
		fmt.Fprintln(w, "Pair: one step left. Install the GitHub CLI: `brew install gh` (macOS) or see https://cli.github.com (Linux), then run `gh auth login`.")
	case "logged-out":
		fmt.Fprintln(w, "Pair: one step left. Run `gh auth login`.")
	default:
		if a.NoLogin {
			// said above
		} else if auth, ok := deviceauth.Resolve(); ok && auth.User != "" {
			fmt.Fprintf(w, "Pair: logged in as %s. Done: your next `gh pr create` or `git push` publishes to pair.sh.\n", auth.User)
		} else {
			fmt.Fprintln(w, "Pair: Done: your next `gh pr create` or `git push` publishes to pair.sh.")
		}
	}
	return nil
}

// printInitSummary is the up-front disclosure (D3): what init sets up, before
// it changes anything.
func printInitSummary(w io.Writer, harness string) {
	var names []string
	codexToo := false
	for _, name := range harnessOrder {
		if harness != "" && name != harness {
			continue
		}
		if harness != "" || harnessTable[name].Detect() {
			names = append(names, harnessDisplay[name])
			codexToo = codexToo || name == "codex"
		}
	}
	agents := "your coding agents (none found yet)"
	if len(names) > 0 {
		agents = joinAnd(names)
	}
	hooks := "hooks in " + agents
	if codexToo {
		hooks += ", approved in Codex on your behalf"
	}
	cli, updates := "the Pair CLI in ~/.paircli/bin", "automatic updates, and hooks for agents you install later"
	if runningFromHomebrew() {
		cli, updates = "the Pair CLI from Homebrew", "hooks for agents you install later (update Pair with brew upgrade)"
	}
	fmt.Fprintf(w, `Pair will set up:
  - %s
  - %s
  - after you log in, PR evidence (including redacted agent transcripts) is uploaded to pair.sh
    when you create or push a PR
  - %s
Uninstall any time: %s uninstall
`, cli, hooks, updates, installedBinaryHint())
}

var harnessDisplay = map[string]string{"claude-code": "Claude Code", "codex": "Codex", "pi": "Pi"}

func joinAnd(items []string) string {
	if len(items) <= 1 {
		return strings.Join(items, "")
	}
	return strings.Join(items[:len(items)-1], ", ") + " and " + items[len(items)-1]
}

// initReporter turns setup's step outcomes into init's one-line progress.
type initReporter struct {
	w            io.Writer
	codexPending bool
}

func (r *initReporter) report(step string, steps map[string]string) {
	outcome := steps[step]
	switch step {
	case "update":
		if strings.HasPrefix(outcome, "updated") {
			fmt.Fprintf(r.w, "Pair: %s\n", outcome)
		}
	case "hooks":
		var found []string
		for _, name := range harnessOrder {
			if steps["hooks:"+name] != "" {
				found = append(found, harnessDisplay[name])
			}
		}
		if len(found) > 0 {
			fmt.Fprintf(r.w, "Pair: found %s\n", strings.Join(found, ", "))
		}
		for _, name := range harnessOrder {
			o := steps["hooks:"+name]
			switch {
			case o == "":
			case strings.HasPrefix(o, "failed"):
				fmt.Fprintf(r.w, "Pair: hooks %s for %s\n", o, harnessDisplay[name])
			case name == "codex":
				r.codexPending = true // reported with its trust outcome
			default:
				fmt.Fprintf(r.w, "Pair: hooks installed for %s (%s)\n", harnessDisplay[name], hookFileFor(name))
			}
		}
		if strings.HasPrefix(outcome, "failed") {
			fmt.Fprintf(r.w, "Pair: hooks %s\n", outcome)
		}
	case "codex_trust":
		if !r.codexPending {
			return
		}
		r.codexPending = false
		if strings.HasPrefix(outcome, "trusted") {
			fmt.Fprintf(r.w, "Pair: hooks installed and trusted for Codex (%s)\n", hookFileFor("codex"))
			return
		}
		fmt.Fprintf(r.w, "Pair: hooks installed for Codex (%s), not yet approved in Codex (%s)\n", hookFileFor("codex"), outcome)
		fmt.Fprintln(r.w, codex.InstallMessage())
	}
}

// hookFileFor is the config file a harness's hooks live in, for messages.
func hookFileFor(name string) string {
	switch name {
	case "claude-code":
		if p, err := claudecode.SettingsPath(); err == nil {
			return engine.Home(p)
		}
	case "codex":
		return engine.Home(codex.HooksPath())
	case "pi":
		return engine.Home(pi.DefaultDir())
	}
	return name
}

// runningFromHomebrew reports whether init runs as a Homebrew paircli.
func runningFromHomebrew() bool {
	exe, err := initBinaryPath()
	if err != nil {
		return false
	}
	if real, err := filepath.EvalSymlinks(exe); err == nil {
		exe = real
	}
	return binpath.IsHomebrewKeg(exe)
}

// installedBinaryHint is how messages name the installed binary: the full
// path for the installer's, which puts nothing on PATH (D6), and plain
// `paircli` for Homebrew's, which brew puts on PATH.
func installedBinaryHint() string {
	if runningFromHomebrew() {
		return "paircli"
	}
	return "~/.paircli/bin/paircli"
}

// installSelf copies the running binary, which the installer verified and
// started from its temp directory srcDir, to ~/.paircli/bin/paircli under
// .bootstrap.lock, writes the release marker the plugin's bootstrap checks,
// and removes srcDir. It copies into a temp file in ~/.paircli/bin and
// renames that, so /tmp on another filesystem (tmpfs) never breaks the rename.
func installSelf(srcDir string) (string, error) {
	exe, err := initExecutable()
	if err != nil {
		return "", err
	}
	if real, err := filepath.EvalSymlinks(exe); err == nil {
		exe = real
	}
	src, err := filepath.EvalSymlinks(srcDir)
	if err != nil || filepath.Dir(exe) != src {
		return "", fmt.Errorf("--install-self %s: not the directory this binary runs from (%s)", srcDir, exe)
	}
	lockPath := state.BootstrapLockPath()
	if lockPath == "" {
		return "", errors.New("could not resolve the home directory")
	}
	binDir := filepath.Dir(lockPath)
	if realBin, err := filepath.EvalSymlinks(binDir); err == nil && realBin == src {
		return "", fmt.Errorf("--install-self %s: this binary is already the installed one", srcDir)
	}
	if err := os.MkdirAll(binDir, 0o755); err != nil {
		return "", err
	}
	lock, err := state.WaitLockPath(lockPath, installLockWait)
	if errors.Is(err, state.ErrLocked) {
		return "", &exitError{code: exitInstallBusy, err: errors.New("another Pair install is running (log: ~/.paircli/logs/bootstrap-*.log); re-run in a few minutes")}
	}
	if err != nil {
		return "", err
	}
	defer lock.Release()

	target := filepath.Join(binDir, "paircli")
	if err := copyExecutable(exe, target); err != nil {
		return "", fmt.Errorf("install %s: %w", target, err)
	}
	if err := state.WriteJSONAtomic(filepath.Join(binDir, "release-source.json"), map[string]string{"manifest_url": selfupdate.DefaultManifestURL}); err != nil {
		return "", fmt.Errorf("record release source: %w", err)
	}
	removeInstallerDir(src, exe)
	return target, nil
}

// removeInstallerDir removes the installer's temp directory. Only a
// directory install.sh made (mktemp's "pair-install.*") is removed whole;
// from anywhere else only the binary goes, so a mistaken --install-self
// never deletes a user's folder.
func removeInstallerDir(dir, exe string) {
	if strings.HasPrefix(filepath.Base(dir), "pair-install.") {
		_ = os.RemoveAll(dir)
		return
	}
	_ = os.Remove(exe)
}

// copyExecutable copies src to a temp file next to dst, makes it 0755 and
// renames it over dst.
func copyExecutable(src, dst string) error {
	in, err := os.Open(src)
	if err != nil {
		return err
	}
	defer in.Close()
	tmp, err := os.CreateTemp(filepath.Dir(dst), ".paircli-install-*")
	if err != nil {
		return err
	}
	defer os.Remove(tmp.Name()) // no-op once renamed
	if _, err := io.Copy(tmp, in); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Chmod(0o755); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	return os.Rename(tmp.Name(), dst)
}

// hasTTY reports whether init has a terminal to talk to. Under `curl … | sh`
// stdin is the pipe, so it asks /dev/tty, never stdin.
func hasTTY() bool {
	f, err := os.OpenFile("/dev/tty", os.O_RDWR, 0)
	if err != nil {
		return false
	}
	f.Close()
	return true
}

// ghState checks the GitHub CLI Pair uses for PRs: "missing", "logged-out"
// or "ok". Pair never installs gh or logs in to it (D5).
func ghState() string {
	if _, err := exec.LookPath("gh"); err != nil || exec.Command("gh", "--version").Run() != nil {
		return "missing"
	}
	if exec.Command("gh", "auth", "status").Run() != nil {
		return "logged-out"
	}
	return "ok"
}

// installRecord is ~/.paircli/state/install.json. It exists only when the
// installed paircli is not ~/.paircli/bin/paircli (a Homebrew install), and
// tells the plugin's bootstrap to use that binary instead of downloading a
// second copy.
type installRecord struct {
	SchemaVersion int    `json:"schema_version"`
	Binary        string `json:"binary"`
}

func installRecordPath() string { return filepath.Join(state.Dir(), "install.json") }

// recordInstallPath writes install.json for a Homebrew paircli, and removes
// it otherwise, so it names the binary init last ran as only when that is a
// Homebrew one.
func recordInstallPath() error {
	exe, err := initBinaryPath()
	if err != nil {
		return err
	}
	real := exe
	if r, err := filepath.EvalSymlinks(exe); err == nil {
		real = r
	}
	// Only a Homebrew paircli is recorded: any other binary outside
	// ~/.paircli/bin (a dev build, a copy in Downloads) must not become the
	// one the plugin runs.
	if !binpath.IsHomebrewKeg(real) {
		if err := os.Remove(installRecordPath()); err != nil && !errors.Is(err, os.ErrNotExist) {
			return err
		}
		return nil
	}
	return state.WriteJSONAtomic(installRecordPath(), installRecord{SchemaVersion: state.SchemaVersion, Binary: exe})
}
