package main

import (
	"bytes"
	"encoding/json"
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
	// Codex still holds both approvals at the hashes Pair recorded.
	writeCodexConfig(t, home, map[string]string{"k:stop:0:0": "h1", "k:session_start:0:0": "h2"})
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

// writeCodexConfig writes config.toml with one hooks.state section per key.
func writeCodexConfig(t *testing.T, home string, trusted map[string]string) {
	t.Helper()
	var b strings.Builder
	b.WriteString("model = \"x\"\n")
	for k, h := range trusted {
		b.WriteString("\n[hooks.state.\"" + k + "\"]\ntrusted_hash = \"" + h + "\"\n")
	}
	if err := os.WriteFile(filepath.Join(home, ".codex", "config.toml"), []byte(b.String()), 0o600); err != nil {
		t.Fatal(err)
	}
}

// A recorded key is a position in hooks.json: once another tool's hook sits
// there (its own approval, another hash), uninstall must leave it alone.
func TestUninstall_LeavesAnotherToolsApprovalAtARecordedKey(t *testing.T) {
	home, revoked := installedMachine(t)
	writeCodexConfig(t, home, map[string]string{"k:stop:0:0": "other-tool-hash", "k:session_start:0:0": "h2"})
	var out bytes.Buffer
	if err := uninstallRun(nil, &out); err != nil {
		t.Fatalf("uninstall: %v\n%s", err, out.String())
	}
	if len(*revoked) != 1 || strings.Join((*revoked)[0], ",") != "k:session_start:0:0" {
		t.Errorf("revoked = %v, want only Pair's own session_start approval", *revoked)
	}
}

// A partial install (an event removed by hand, or an older Pair's set) still
// has Pair hooks; uninstall removes them before deleting the binary they run.
func TestUninstall_RemovesAPartialHookSet(t *testing.T) {
	home, _ := installedMachine(t)
	path, _ := claudecode.SettingsPath()
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var settings map[string]any
	if err := json.Unmarshal(data, &settings); err != nil {
		t.Fatal(err)
	}
	delete(settings["hooks"].(map[string]any), "Stop")
	data, _ = json.Marshal(settings)
	if err := os.WriteFile(path, data, 0o600); err != nil {
		t.Fatal(err)
	}
	if claudecode.HooksInstalled() || !claudecode.AnyHooksPresent() {
		t.Fatal("fixture: want a partial Claude Code install")
	}
	var out bytes.Buffer
	if err := uninstallRun(nil, &out); err != nil {
		t.Fatalf("uninstall: %v\n%s", err, out.String())
	}
	if claudecode.AnyHooksPresent() {
		t.Errorf("Pair hooks left in settings.json:\n%s", out.String())
	}
	if _, err := os.Stat(filepath.Join(home, ".paircli")); !os.IsNotExist(err) {
		t.Errorf("~/.paircli still exists: %v", err)
	}
}

// When a harness's hooks cannot be removed, the binary they run stays.
func TestUninstall_KeepsTheBinaryWhenHooksRemain(t *testing.T) {
	home, _ := installedMachine(t)
	path, _ := claudecode.SettingsPath()
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	// Unparsable settings.json: UninstallHooks refuses to touch it.
	if err := os.WriteFile(path, append(data, []byte("\n{")...), 0o600); err != nil {
		t.Fatal(err)
	}
	orig := harnessTable["claude-code"]
	t.Cleanup(func() { harnessTable["claude-code"] = orig })
	h := orig
	h.Present = func() bool { return true }
	harnessTable["claude-code"] = h

	var out bytes.Buffer
	if err := uninstallRun(nil, &out); err == nil {
		t.Fatalf("uninstall succeeded with hooks left:\n%s", out.String())
	}
	if _, err := os.Stat(filepath.Join(home, ".paircli")); err != nil {
		t.Errorf("~/.paircli removed while hooks still run its binary: %v", err)
	}
	if !strings.Contains(out.String(), "kept ~/.paircli") {
		t.Errorf("output = %s", out.String())
	}
}
