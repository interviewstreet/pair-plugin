package main

import (
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"

	"github.com/interviewstreet/pair-plugin/cli/internal/codex"
)

// uninstallOwnedTrustKeys picks the recorded keys still approved at Pair's
// hash; a test seam.
var uninstallOwnedTrustKeys = codex.OwnedTrustKeys

// uninstallRevokeTrust removes Codex hook-trust entries; a test seam.
var uninstallRevokeTrust = func(codexPath string, keys []string) error {
	return codex.RevokeTrust(context.Background(), codexPath, version, keys, codexTrustTimeout)
}

func runUninstall(args []string) error { return uninstallRun(args, os.Stdout) }

// uninstallRun removes Pair from this machine: the Codex trust entries init
// recorded, every harness's hooks, the credential, and ~/.paircli. It does
// not look for the plugin; it reminds the user to remove it, since the
// plugin's bootstrap would install Pair again at the next session start.
func uninstallRun(args []string, w io.Writer) error {
	if len(args) > 0 {
		return fmt.Errorf("unexpected argument %q", args[0])
	}
	var failed []string

	rec, err := readInitRecord()
	switch {
	case errors.Is(err, os.ErrNotExist):
	case err != nil:
		fmt.Fprintf(w, "paircli: could not read %s (%v), so any hook approvals Pair added are left in Codex's config.toml; they match no hook and do nothing\n", initRecordPath(), err)
	case len(rec.Trust) > 0:
		// Only approvals that are still Pair's: a recorded key is a position
		// in hooks.json and may now hold another tool's hook and approval.
		keys := uninstallOwnedTrustKeys(rec.Trust)
		if len(keys) == 0 {
			fmt.Fprintln(w, "paircli: no hook approvals of Pair's left in Codex")
			break
		}
		err := errors.New("codex binary not found")
		if bin := codex.FindCodex(rec.CodexPath); bin != "" {
			err = uninstallRevokeTrust(bin, keys)
		}
		if err == nil {
			fmt.Fprintf(w, "paircli: removed Pair's hook approvals from Codex (%d recorded)\n", len(keys))
		} else {
			fmt.Fprintf(w, "paircli: left Pair's hook approvals in Codex's config.toml (%v); they match no hook and do nothing\n", err)
		}
	}

	hooksErr := uninstallAllHarnesses(w)
	if hooksErr != nil {
		failed = append(failed, hooksErr.Error())
	}
	if err := logoutRun(nil, w, w); err != nil {
		failed = append(failed, "logout: "+err.Error())
	}
	home, err := os.UserHomeDir()
	if hooksErr != nil {
		// The hooks left behind run ~/.paircli/bin/paircli; removing it would
		// leave them failing at every agent event.
		fmt.Fprintln(w, "paircli: kept ~/.paircli, since some hooks are still installed and run its binary; fix the error above and run uninstall again")
	} else if err != nil {
		failed = append(failed, "remove ~/.paircli: "+err.Error())
	} else if err := os.RemoveAll(filepath.Join(home, ".paircli")); err != nil {
		failed = append(failed, "remove ~/.paircli: "+err.Error())
	} else {
		fmt.Fprintln(w, "paircli: ~/.paircli removed (binary, logs and state)")
	}

	fmt.Fprint(w, `If you also installed the Pair plugin, remove it too, or it installs Pair again at the next session start:
  claude plugin uninstall pair@pair
  codex plugin remove pair@pair
Restart any open Claude Code or Codex sessions.
`)
	if len(failed) > 0 {
		return fmt.Errorf("uninstall incomplete: %s", strings.Join(failed, "; "))
	}
	return nil
}
