package main

import (
	"bytes"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/interviewstreet/pair-plugin/cli/internal/claudecode"
	"github.com/interviewstreet/pair-plugin/cli/internal/codex"
	"github.com/interviewstreet/pair-plugin/cli/internal/deviceauth"
)

// installedMachine is a temp HOME as init leaves it: Claude Code and Codex
// hooks, a credential, init.json with two Codex trust keys, and ~/.paircli.
func installedMachine(t *testing.T) (home string, revoked *[][]string) {
	t.Helper()
	setupEnv(t)
	home, _ = os.UserHomeDir()
	for _, dir := range []string{".claude", ".codex"} {
		if err := os.MkdirAll(filepath.Join(home, dir), 0o700); err != nil {
			t.Fatal(err)
		}
	}
	if err := claudecode.InstallHooks(); err != nil {
		t.Fatal(err)
	}
	if err := codex.InstallHooks(); err != nil {
		t.Fatal(err)
	}
	if err := deviceauth.Save(deviceauth.DefaultFile(), deviceauth.StoredAuth{AccessToken: "secret", ServerURL: "https://pair.example"}); err != nil {
		t.Fatal(err)
	}
	codexBin := filepath.Join(t.TempDir(), "codex")
	if err := os.WriteFile(codexBin, []byte("#!/bin/sh\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := writeInitRecord(initRecord{CodexPath: codexBin, Trust: codex.TrustRecord{"k:stop:0:0": {Hash: "h1"}, "k:session_start:0:0": {Hash: "h2"}}}); err != nil {
		t.Fatal(err)
	}
	revoked = &[][]string{}
	orig := uninstallRevokeTrust
	t.Cleanup(func() { uninstallRevokeTrust = orig })
	uninstallRevokeTrust = func(_ string, keys []string) error {
		*revoked = append(*revoked, keys)
		return nil
	}
	return home, revoked
}

func TestUninstall_RemovesEverythingAndRemindsAboutThePlugin(t *testing.T) {
	home, revoked := installedMachine(t)
	var out bytes.Buffer
	if err := uninstallRun(nil, &out); err != nil {
		t.Fatalf("uninstall: %v\n%s", err, out.String())
	}
	if len(*revoked) == 0 || strings.Join((*revoked)[len(*revoked)-1], ",") != "k:session_start:0:0,k:stop:0:0" {
		t.Errorf("revoked = %v, want init.json's two keys", *revoked)
	}
	if claudecode.HooksInstalled() || codex.HooksInstalled() {
		t.Error("hooks still installed")
	}
	if _, err := os.Stat(filepath.Join(home, ".paircli")); !os.IsNotExist(err) {
		t.Errorf("~/.paircli still exists: %v", err)
	}
	if _, ok := deviceauth.Resolve(); ok {
		t.Error("credential still usable")
	}
	for _, want := range []string{"claude plugin uninstall pair@pair", "codex plugin remove pair@pair", "removed Pair's hook approvals from Codex (2 recorded)"} {
		if !strings.Contains(out.String(), want) {
			t.Errorf("output lacks %q:\n%s", want, out.String())
		}
	}
}

func TestUninstall_LeavesTrustEntriesWhenCodexCannotRemoveThem(t *testing.T) {
	home, _ := installedMachine(t)
	uninstallRevokeTrust = func(string, []string) error { return errors.New("codex app-server timed out") }
	var out bytes.Buffer
	if err := uninstallRun(nil, &out); err != nil {
		t.Fatalf("uninstall: %v", err)
	}
	if !strings.Contains(out.String(), "left Pair's hook approvals in Codex's config.toml (codex app-server timed out)") {
		t.Errorf("output = %s", out.String())
	}
	if _, err := os.Stat(filepath.Join(home, ".paircli")); !os.IsNotExist(err) {
		t.Error("a trust failure stopped the rest of the uninstall")
	}
}

func TestUninstall_SaysSoWhenInitJSONIsUnreadable(t *testing.T) {
	home, revoked := installedMachine(t)
	if err := os.WriteFile(initRecordPath(), []byte("{not json"), 0o600); err != nil {
		t.Fatal(err)
	}
	var out bytes.Buffer
	if err := uninstallRun(nil, &out); err != nil {
		t.Fatal(err)
	}
	if len(*revoked) != 0 || !strings.Contains(out.String(), "hook approvals Pair added are left") {
		t.Errorf("output = %s", out.String())
	}
	if _, err := os.Stat(filepath.Join(home, ".paircli")); !os.IsNotExist(err) {
		t.Error("~/.paircli still exists")
	}
}

// A plugin-only machine has no init.json: nothing to revoke, the rest as before.
func TestUninstall_PluginOnlyMachine(t *testing.T) {
	_, revoked := installedMachine(t)
	if err := os.Remove(initRecordPath()); err != nil {
		t.Fatal(err)
	}
	var out bytes.Buffer
	if err := uninstallRun(nil, &out); err != nil {
		t.Fatal(err)
	}
	if len(*revoked) != 0 || claudecode.HooksInstalled() {
		t.Errorf("revoked = %v, hooks installed = %v", *revoked, claudecode.HooksInstalled())
	}
}
