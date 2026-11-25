package binpath

import (
	"os"
	"path/filepath"
	"testing"
)

func TestStable(t *testing.T) {
	prefix, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	keg := filepath.Join(prefix, "Cellar", "pair", "0.6.0", "bin", "paircli")
	if err := os.MkdirAll(filepath.Dir(keg), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(keg, []byte("#!/bin/sh\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	if got := Stable(keg); got != keg {
		t.Errorf("Stable without the bin link = %q, want the keg path", got)
	}
	link := filepath.Join(prefix, "bin", "paircli")
	if err := os.MkdirAll(filepath.Dir(link), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(keg, link); err != nil {
		t.Fatal(err)
	}
	if got := Stable(keg); got != link {
		t.Errorf("Stable = %q, want the brew link %q", got, link)
	}
	if got := Stable("/home/u/.paircli/bin/paircli"); got != "/home/u/.paircli/bin/paircli" {
		t.Errorf("Stable changed a non-brew path: %q", got)
	}
}

func TestIsHomebrewKeg(t *testing.T) {
	for path, want := range map[string]bool{
		"/opt/homebrew/Cellar/pair/0.6.0/bin/paircli":              true,
		"/usr/local/Cellar/paircli/0.1.0/bin/paircli":              true,
		"/home/linuxbrew/.linuxbrew/Cellar/pair/0.6.0/bin/paircli": true,
		"/home/u/.paircli/bin/paircli":                             false,
		"/opt/homebrew/bin/paircli":                                false,
	} {
		if got := IsHomebrewKeg(path); got != want {
			t.Errorf("IsHomebrewKeg(%s) = %v, want %v", path, got, want)
		}
	}
}
