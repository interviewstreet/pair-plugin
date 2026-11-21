package prtrigger

import (
	"bytes"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// The first run of a freshly written script can take seconds on a loaded
// machine (Gatekeeper scans it), which the production bound does not allow for.
func init() { resolveTimeout = 30 * time.Second }

// fakeGH puts a `gh` script with the given body first on PATH.
func fakeGH(t *testing.T, body string) {
	t.Helper()
	bin := t.TempDir()
	if err := os.WriteFile(filepath.Join(bin, "gh"), []byte("#!/bin/sh\n"+body+"\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", bin+string(os.PathListSeparator)+os.Getenv("PATH"))
}

// ghView is a fake gh body printing what `gh pr view --json state,url` prints
// for a PR in the given state.
func ghView(state, url string) string {
	return `echo '{"state":"` + state + `","url":"` + url + `"}'`
}

func TestResolveCurrentBranchPR(t *testing.T) {
	fakeGH(t, ghView("OPEN", testPRURL))
	got, err := ResolveCurrentBranchPR(t.TempDir())
	if err != nil || got != wantPR {
		t.Errorf("ResolveCurrentBranchPR = %+v, %v, want %+v", got, err, wantPR)
	}
}

// gh pr view names the branch's newest PR in any state. A branch left checked
// out after its PR merged or closed has no PR to publish.
func TestResolveCurrentBranchPR_OnlyAnOpenPR(t *testing.T) {
	for _, state := range []string{"MERGED", "CLOSED"} {
		t.Run(state, func(t *testing.T) {
			fakeGH(t, ghView(state, testPRURL))
			if pr, err := ResolveCurrentBranchPR(t.TempDir()); err == nil {
				t.Errorf("ResolveCurrentBranchPR = %+v, want an error for a %s PR", pr, state)
			}
		})
	}
}

// After a push on a branch whose PR is merged or closed the hook does nothing:
// no publish starts and nothing is printed.
func TestRunnerRun_PushOnABranchWhosePRIsNotOpen(t *testing.T) {
	tests := []struct {
		state     string
		wantStart bool
	}{{"OPEN", true}, {"MERGED", false}, {"CLOSED", false}}
	for _, tt := range tests {
		t.Run(tt.state, func(t *testing.T) {
			fakeGH(t, ghView(tt.state, testPRURL))
			var starts []startCall
			r := fakeRunner(nil, nil, &starts)
			r.Resolve = ResolveCurrentBranchPR
			var out bytes.Buffer
			ev := bashEvent("git push", "", nil)
			ev.CWD = t.TempDir()
			if err := r.Run(ev, &out); err != nil {
				t.Fatalf("Run: %v", err)
			}
			if started := len(starts) == 1 && starts[0].pr == wantPR; started != tt.wantStart || len(starts) > 1 {
				t.Errorf("starts = %+v, want a start: %v", starts, tt.wantStart)
			}
			if printed := out.Len() > 0; printed != tt.wantStart {
				t.Errorf("printed %q, want output: %v", out.String(), tt.wantStart)
			}
		})
	}
}

func TestResolveCurrentBranchPR_AsksGHForTheBranchURLInTheDirectory(t *testing.T) {
	dir := t.TempDir()
	record := filepath.Join(t.TempDir(), "call")
	fakeGH(t, `echo "$PWD|$*" > `+record+"\n"+ghView("OPEN", testPRURL))
	if _, err := ResolveCurrentBranchPR(dir); err != nil {
		t.Fatal(err)
	}
	data, err := os.ReadFile(record)
	if err != nil {
		t.Fatal(err)
	}
	wantDir, _ := filepath.EvalSymlinks(dir)
	gotDir, args, _ := strings.Cut(strings.TrimSpace(string(data)), "|")
	if gotEval, _ := filepath.EvalSymlinks(gotDir); gotEval != wantDir {
		t.Errorf("gh ran in %q, want %q", gotDir, wantDir)
	}
	if want := "pr view --json state,url"; args != want {
		t.Errorf("gh args = %q, want %q", args, want)
	}
}

func TestResolveCurrentBranchPR_Failures(t *testing.T) {
	tests := []struct {
		name string
		body string
	}{
		{"no pull request", "exit 1"},
		{"no output", "true"},
		{"not json", "echo " + testPRURL},
		{"no state", `echo '{"url":"` + testPRURL + `"}'`},
		{"not a github url", ghView("OPEN", "https://gitlab.com/a/b/pull/1")},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			fakeGH(t, tt.body)
			if pr, err := ResolveCurrentBranchPR(t.TempDir()); err == nil {
				t.Errorf("ResolveCurrentBranchPR = %+v, want an error", pr)
			}
		})
	}
}

func TestResolveCurrentBranchPR_MissingDirectory(t *testing.T) {
	fakeGH(t, ghView("OPEN", testPRURL))
	if _, err := ResolveCurrentBranchPR(filepath.Join(t.TempDir(), "missing")); err == nil {
		t.Error("want an error for a missing directory")
	}
}

func TestResolveCurrentBranchPR_NoGH(t *testing.T) {
	t.Setenv("PATH", t.TempDir())
	if _, err := ResolveCurrentBranchPR(t.TempDir()); err == nil {
		t.Error("want an error without gh on PATH")
	}
}
