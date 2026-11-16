package notify

import (
	"bytes"
	"crypto/sha256"
	"debug/macho"
	"encoding/hex"
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

func testBundle() notificationBundle {
	executable := []byte("test helper executable")
	digest := sha256.Sum256(executable)
	return notificationBundle{executable, hex.EncodeToString(digest[:]), []byte("icns-test-icon"), []byte("MIT license")}
}

func TestPinnedHelperAssets(t *testing.T) {
	executable, err := os.ReadFile("assets/terminal-notifier-macos")
	if err != nil {
		t.Fatal(err)
	}
	digest := sha256.Sum256(executable)
	if hex.EncodeToString(digest[:]) != helperSHA256 {
		t.Fatal("vendored executable checksum differs from the reviewed upstream release")
	}
	fat, err := macho.NewFatFile(bytes.NewReader(executable))
	if err != nil {
		t.Fatal(err)
	}
	architectures := map[macho.Cpu]bool{}
	for _, architecture := range fat.Arches {
		architectures[architecture.Cpu] = true
	}
	if !architectures[macho.CpuArm64] || !architectures[macho.CpuAmd64] {
		t.Fatal("helper must support both macOS release architectures")
	}
	icon, err := os.ReadFile("assets/pair.icns")
	if err != nil || len(icon) < 8 || string(icon[:4]) != "icns" {
		t.Fatal("Pair icon is missing")
	}
	license, err := os.ReadFile("assets/terminal-notifier-LICENSE.txt")
	if err != nil || !bytes.Contains(license, []byte("Permission is hereby granted")) {
		t.Fatal("upstream MIT license is missing")
	}
}

func TestBundleInstallIsBrandedAndIdempotent(t *testing.T) {
	home := t.TempDir()
	bundle := testBundle()
	var calls [][]string
	run := func(argv []string) error { calls = append(calls, argv); return nil }
	result, err := installBundle(home, bundle, run)
	if err != nil || result != "Pair notifications installed" {
		t.Fatalf("install = %q, %v", result, err)
	}
	binary, err := os.Stat(pairNotifierPath(home))
	if err != nil || binary.Mode().Perm()&0o111 == 0 {
		t.Fatal("executable not installed")
	}
	resources := filepath.Join(home, "Applications", pairBundleName, "Contents", "Resources")
	for name, expected := range map[string][]byte{"Pair.icns": bundle.icon, "terminal-notifier-LICENSE.txt": bundle.license} {
		got, err := os.ReadFile(filepath.Join(resources, name))
		if err != nil || !bytes.Equal(got, expected) {
			t.Fatalf("asset %s differs", name)
		}
	}
	info, err := os.ReadFile(filepath.Join(resources, "..", "Info.plist"))
	if err != nil || !strings.Contains(string(info), pairBundleID) || !strings.Contains(string(info), "<string>Pair</string>") {
		t.Fatal("app identity not branded")
	}
	if len(calls) != 2 || !reflect.DeepEqual(calls[0][:4], []string{"/usr/bin/codesign", "--force", "--sign", "-"}) || calls[1][1] != "--verify" {
		t.Fatalf("app was not signed and verified: %v", calls)
	}
	calls = nil
	result, err = installBundle(home, bundle, run)
	if err != nil || result != "Pair notifications ready" || len(calls) != 3 || calls[0][1] != "--force" || calls[1][1] != "--verify" || calls[2][1] != "--verify" {
		t.Fatalf("repeat installation = %q, %v, calls %v", result, err, calls)
	}
	stages, _ := filepath.Glob(filepath.Join(home, "Applications", ".pair-notifications-*"))
	if len(stages) != 0 {
		t.Fatalf("temporary installs leaked: %v", stages)
	}
}

func TestBundleInstallRejectsBadAssetsBeforeWriting(t *testing.T) {
	for _, variant := range []string{"checksum", "icon", "license"} {
		t.Run(variant, func(t *testing.T) {
			home := t.TempDir()
			bundle := testBundle()
			switch variant {
			case "checksum":
				bundle.digest = "wrong"
			case "icon":
				bundle.icon = nil
			case "license":
				bundle.license = nil
			}
			if _, err := installBundle(home, bundle, func([]string) error { t.Fatal("command ran for invalid bundle"); return nil }); err == nil {
				t.Fatal("invalid bundle accepted")
			}
			if _, err := os.Stat(filepath.Join(home, "Applications")); !errors.Is(err, os.ErrNotExist) {
				t.Fatal("invalid bundle wrote app data")
			}
		})
	}
}

func TestBundleInstallDoesNotReplaceUnmanagedOrLinkedApps(t *testing.T) {
	for _, linked := range []bool{false, true} {
		t.Run(map[bool]string{false: "unmanaged", true: "symlink"}[linked], func(t *testing.T) {
			home := t.TempDir()
			target := filepath.Join(home, "Applications", pairBundleName)
			if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
				t.Fatal(err)
			}
			if linked {
				if err := os.Symlink(t.TempDir(), target); err != nil {
					t.Fatal(err)
				}
			} else if err := os.Mkdir(target, 0o755); err != nil {
				t.Fatal(err)
			}
			if _, err := installBundle(home, testBundle(), func([]string) error { return nil }); err == nil {
				t.Fatal("unmanaged app was replaced")
			}
			if _, err := os.Lstat(target); err != nil {
				t.Fatal("existing app was removed")
			}
		})
	}
}

func TestBundleFailedUpgradePreservesPreviousApp(t *testing.T) {
	home := t.TempDir()
	bundle := testBundle()
	if _, err := installBundle(home, bundle, func([]string) error { return nil }); err != nil {
		t.Fatal(err)
	}
	markerPath := filepath.Join(home, "Applications", pairBundleName, "Contents", "Resources", markerName)
	before, _ := os.ReadFile(markerPath)
	bundle.icon = []byte("icns-new-icon")
	if _, err := installBundle(home, bundle, func([]string) error { return errors.New("sign failed") }); err == nil {
		t.Fatal("signing error was swallowed")
	}
	after, _ := os.ReadFile(markerPath)
	if !bytes.Equal(before, after) {
		t.Fatal("failed upgrade replaced the previous app")
	}
	if _, err := installBundle(home, bundle, func([]string) error { return nil }); err != nil {
		t.Fatal(err)
	}
	after, _ = os.ReadFile(markerPath)
	if bytes.Equal(before, after) {
		t.Fatal("successful retry did not refresh changed assets")
	}
}

func TestBundleInstallRejectsRelativeHomeAndLinkedApplications(t *testing.T) {
	if _, err := installBundle("relative", testBundle(), func([]string) error { return nil }); err == nil {
		t.Fatal("relative home accepted")
	}
	home := t.TempDir()
	if err := os.Symlink(t.TempDir(), filepath.Join(home, "Applications")); err != nil {
		t.Fatal(err)
	}
	if _, err := installBundle(home, testBundle(), func([]string) error { return nil }); err == nil {
		t.Fatal("Applications symlink accepted")
	}
}

func TestBundleRepairsReplacedExecutable(t *testing.T) {
	home := t.TempDir()
	bundle := testBundle()
	run := func([]string) error { return nil }
	if _, err := installBundle(home, bundle, run); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(pairNotifierPath(home), []byte("different signed executable"), 0o755); err != nil {
		t.Fatal(err)
	}
	if result, err := installBundle(home, bundle, run); err != nil || result != "Pair notifications installed" {
		t.Fatalf("replacement was accepted: %q, %v", result, err)
	}
	restored, err := os.ReadFile(pairNotifierPath(home))
	if err != nil || !bytes.Equal(restored, bundle.executable) {
		t.Fatal("pinned executable was not restored")
	}
}
