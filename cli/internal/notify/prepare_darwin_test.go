package notify

import (
	"bytes"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"github.com/interviewstreet/pair-plugin/cli/internal/state"
)

func TestPrepareOfflineWithoutHomebrew(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	t.Setenv("PATH", "")
	result, err := Prepare()
	if err != nil {
		t.Fatal(err)
	}
	if result != "Pair notifications installed" {
		t.Fatalf("prepare = %q", result)
	}
	app := filepath.Join(home, "Applications", pairBundleName)
	if output, err := exec.Command("/usr/bin/codesign", "--verify", "--strict", app).CombinedOutput(); err != nil {
		t.Fatalf("signature verification: %v: %s", err, output)
	}
	if output, err := exec.Command("/usr/bin/plutil", "-extract", "CFBundleIdentifier", "raw", "-o", "-", filepath.Join(app, "Contents", "Info.plist")).Output(); err != nil || strings.TrimSpace(string(output)) != pairBundleID {
		t.Fatalf("app identity = %q, %v", output, err)
	}
	if _, err := os.Stat(pairNotifierPath(home)); err != nil {
		t.Fatal(err)
	}
	if result, err := Prepare(); err != nil || result != "Pair notifications ready" {
		t.Fatalf("repeat = %q, %v", result, err)
	}
}

func TestPrepareIsSingleFlight(t *testing.T) {
	t.Setenv("HOME", t.TempDir())
	lock, err := state.TryLock("notification-helper")
	if err != nil {
		t.Fatal(err)
	}
	defer lock.Release()
	result, err := Prepare()
	if err != nil || result != "Pair notifications are being prepared" {
		t.Fatalf("concurrent prepare = %q, %v", result, err)
	}
}

func TestPrepareRejectsRelativeHomeBeforeCreatingState(t *testing.T) {
	t.Setenv("HOME", "relative")
	if _, err := Prepare(); err == nil {
		t.Fatal("relative home accepted")
	}
	if _, err := os.Stat("relative"); !os.IsNotExist(err) {
		t.Fatal("invalid HOME created state in the current directory")
	}
}

func TestSendDoesNotSelectRejectedUnmanagedHelper(t *testing.T) {
	t.Setenv("HOME", t.TempDir())
	helper := pairNotifierPath(os.Getenv("HOME"))
	if err := os.MkdirAll(filepath.Dir(helper), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(helper, []byte("#!/bin/sh\nexit 0\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	if _, err := Prepare(); err == nil {
		t.Fatal("unmanaged app was not rejected")
	}
	previous := run
	t.Cleanup(func() { run = previous })
	var selected string
	run = func(argv []string) error { selected = argv[0]; return nil }
	Send("Pair review ready", "https://pair.sh/acme/shop/pull/7")
	if selected == helper {
		t.Fatal("rejected unmanaged helper selected for execution")
	}
}

func TestSendDoesNotSelectHelperWhilePreparationLocked(t *testing.T) {
	t.Setenv("HOME", t.TempDir())
	if _, err := Prepare(); err != nil {
		t.Fatal(err)
	}
	lock, err := state.TryLock("notification-helper")
	if err != nil {
		t.Fatal(err)
	}
	defer lock.Release()
	previous := run
	t.Cleanup(func() { run = previous })
	var selected string
	run = func(argv []string) error { selected = argv[0]; return nil }
	Send("Pair review ready", "https://pair.sh/acme/shop/pull/7")
	if selected == pairNotifierPath(os.Getenv("HOME")) {
		t.Fatal("helper selected without acquiring its validation lock")
	}
}

func TestSendUsesPreparedPairHelper(t *testing.T) {
	t.Setenv("HOME", t.TempDir())
	t.Setenv("PATH", "")
	previous := run
	t.Cleanup(func() { run = previous })
	var selected []string
	run = func(argv []string) error { selected = argv; return nil }
	if !Send("Pair review ready", "https://pair.sh/acme/shop/pull/7") {
		t.Fatal("prepared helper was not selected")
	}
	if len(selected) != 7 || selected[0] != pairNotifierPath(os.Getenv("HOME")) || selected[5] != "-open" || selected[6] != "https://pair.sh/acme/shop/pull/7" {
		t.Fatalf("notification command = %q", selected)
	}
}

func TestPrepareRepairsResignedExecutable(t *testing.T) {
	t.Setenv("HOME", t.TempDir())
	if _, err := Prepare(); err != nil {
		t.Fatal(err)
	}
	trusted, err := os.ReadFile(pairNotifierPath(os.Getenv("HOME")))
	if err != nil {
		t.Fatal(err)
	}
	replacement, err := os.ReadFile("/usr/bin/true")
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(pairNotifierPath(os.Getenv("HOME")), replacement, 0o755); err != nil {
		t.Fatal(err)
	}
	app := filepath.Join(os.Getenv("HOME"), "Applications", pairBundleName)
	if output, err := exec.Command("/usr/bin/codesign", "--force", "--sign", "-", app).CombinedOutput(); err != nil {
		t.Fatalf("fixture signing: %v: %s", err, output)
	}
	if result, err := Prepare(); err != nil || result != "Pair notifications installed" {
		t.Fatalf("replacement was not repaired: %q, %v", result, err)
	}
	restored, err := os.ReadFile(pairNotifierPath(os.Getenv("HOME")))
	if err != nil || !bytes.Equal(restored, trusted) {
		t.Fatal("trusted signed executable was not restored")
	}
	if result, err := Prepare(); err != nil || result != "Pair notifications ready" {
		t.Fatalf("repaired helper not reusable: %q, %v", result, err)
	}
}
