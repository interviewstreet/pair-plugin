package main

import (
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"time"

	"github.com/interviewstreet/pair-plugin/cli/internal/maintain"
	"github.com/interviewstreet/pair-plugin/cli/internal/state"
)

// The seams between maintain and the machine, replaceable in tests.
var (
	maintainUpdate     = selfUpdateQuietly
	maintainExec       = syscall.Exec
	maintainExecutable = os.Executable
)

func runMaintain(args []string) error { return maintainRun(args, os.Stderr) }

// maintainRun is `paircli maintain [--after-update]`, which an enrolled
// machine's SessionStart hook starts at most once a day: it self-updates,
// then re-runs setup's hook and Codex-trust steps for every detected agent,
// so an agent installed later gets hooks and a removed or stale entry is
// repaired. It logs in only when there is no credential. It never blocks a
// session and never runs on a machine init did not enroll.
func maintainRun(args []string, w io.Writer) error {
	// `--after-update <old version>` is the re-exec after an update.
	afterUpdate, updatedFrom := false, ""
	switch {
	case len(args) == 0:
	case args[0] == "--after-update" && len(args) == 2:
		afterUpdate, updatedFrom = true, args[1]
	default:
		return fmt.Errorf("unexpected argument %q", args[0])
	}
	if !initEnrolled() {
		return nil
	}

	lock, err := state.TryLock("setup")
	if errors.Is(err, state.ErrLocked) {
		// Another setup is running; the next session tries again.
		return nil
	}
	if err != nil {
		return err
	}
	defer lock.Release()
	if !afterUpdate && !maintain.Due(time.Now()) {
		return nil // another run finished while this one was starting
	}
	log, closeLog := maintainLog(w)
	defer closeLog()

	update := fmt.Sprintf("updated %s to %s", updatedFrom, version)
	if !afterUpdate {
		outcome, err := maintainUpdate(context.Background(), "")
		if err != nil {
			outcome = "skipped: " + err.Error()
		}
		fmt.Fprintf(log, "paircli: maintain: update: %s\n", outcome)
		if strings.HasPrefix(outcome, "updated") {
			// Run the rest with the new code. A flock does not survive exec,
			// so release it first; if another run takes it in between, the
			// next session retries, and every step is idempotent.
			lock.Release()
			exe, err := maintainExecutable()
			if err != nil {
				return err
			}
			return maintainExec(exe, []string{exe, "maintain", "--after-update", version}, os.Environ())
		}
		update = outcome
	}

	// The login step does nothing with a credential; without one it opens
	// the Pair login, as the plugin's session start does.
	// Keep to the agent `init --harness` chose, if it chose one.
	rec, _ := readInitRecord()
	err = runSetupSteps(setupArgs{
		Harness: rec.Scope, UpdateOutcome: update,
		PerAgent: true, GrantCodexTrust: true, Enroll: true,
	}, log, io.Discard)
	if stampErr := maintain.WriteStamp(time.Now()); stampErr != nil {
		fmt.Fprintf(log, "paircli: maintain: could not write %s: %v\n", maintain.StampPath(), stampErr)
	}
	return err
}

// maintainLog opens ~/.paircli/logs/maintain-<time>.log, falling back to w.
func maintainLog(w io.Writer) (io.Writer, func()) {
	dir := state.LogDir()
	if dir == "" || os.MkdirAll(dir, 0o700) != nil {
		return w, func() {}
	}
	f, err := os.OpenFile(filepath.Join(dir, "maintain-"+time.Now().Format("20060102-150405")+".log"), os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o600)
	if err != nil {
		return w, func() {}
	}
	return f, func() { f.Close() }
}
