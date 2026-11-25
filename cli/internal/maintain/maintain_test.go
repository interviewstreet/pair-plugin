package maintain

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestDue(t *testing.T) {
	t.Setenv("HOME", t.TempDir())
	now := time.Date(2026, 11, 25, 12, 0, 0, 0, time.UTC)
	if Due(now) {
		t.Error("due without init.json (a plugin-only machine)")
	}
	if err := os.MkdirAll(filepath.Dir(InitPath()), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(InitPath(), []byte("{}"), 0o600); err != nil {
		t.Fatal(err)
	}
	if !Due(now) {
		t.Error("not due with init.json and no stamp")
	}
	if err := WriteStamp(now.Add(-time.Hour)); err != nil {
		t.Fatal(err)
	}
	if Due(now) {
		t.Error("due an hour after the last run")
	}
	if err := WriteStamp(now.Add(-Interval - time.Minute)); err != nil {
		t.Fatal(err)
	}
	if !Due(now) {
		t.Error("not due a day after the last run")
	}
}
