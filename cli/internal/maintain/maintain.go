// Package maintain decides when an agent's SessionStart hook starts
// `paircli maintain`, the background run that keeps an installer-path
// machine updated and repaired. Only machines `paircli init` enrolled (they
// have ~/.paircli/state/init.json) are maintained; plugin-only machines never
// are, so their hooks behave exactly as before.
package maintain

import (
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"syscall"
	"time"

	"github.com/interviewstreet/pair-plugin/cli/internal/state"
)

// Interval is the least time between two maintain runs.
const Interval = 24 * time.Hour

// Spawn starts `paircli maintain` detached from the hook; a test seam.
var Spawn = spawnDetached

// InitPath is ~/.paircli/state/init.json, written by `paircli init`.
func InitPath() string { return filepath.Join(state.Dir(), "init.json") }

// StampPath is ~/.paircli/state/maintain.json, maintain's own timestamp.
// It is separate from setup.json, which the plugin's bootstrap rewrites
// every session.
func StampPath() string { return filepath.Join(state.Dir(), "maintain.json") }

// Stamp is maintain.json.
type Stamp struct {
	LastRun time.Time `json:"last_run"`
}

// Due reports whether this machine is enrolled and its last maintain run
// finished more than Interval ago (or never).
func Due(now time.Time) bool {
	if _, err := os.Stat(InitPath()); err != nil {
		return false
	}
	var s Stamp
	data, err := os.ReadFile(StampPath())
	if err != nil || json.Unmarshal(data, &s) != nil {
		return true
	}
	return now.Sub(s.LastRun) >= Interval
}

// WriteStamp records a finished maintain run.
func WriteStamp(now time.Time) error {
	return state.WriteJSONAtomic(StampPath(), Stamp{LastRun: now.UTC()})
}

// MaybeStart starts maintain in the background when it is due. A hook calls
// it on SessionStart; it never blocks or fails the session.
func MaybeStart() {
	if Due(time.Now()) {
		_ = Spawn()
	}
}

func spawnDetached() error {
	exe, err := os.Executable()
	if err != nil {
		return err
	}
	cmd := exec.Command(exe, "maintain")
	cmd.SysProcAttr = &syscall.SysProcAttr{Setsid: true}
	if err := cmd.Start(); err != nil {
		return err
	}
	return cmd.Process.Release()
}
