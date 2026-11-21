package pr

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/interviewstreet/pair-plugin/cli/internal/model"
)

// fakeRunner maps a joined gh argument list to canned bytes or an error, and
// fails the test on any unexpected call.
type fakeRunner struct {
	t         *testing.T
	responses map[string][]byte
	errs      map[string]error
	calls     []string
}

func newFakeRunner(t *testing.T) *fakeRunner {
	return &fakeRunner{t: t, responses: map[string][]byte{}, errs: map[string]error{}}
}

func (f *fakeRunner) withFile(key, path string) *fakeRunner {
	f.t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		f.t.Fatalf("reading testdata %s: %v", path, err)
	}
	f.responses[key] = data
	return f
}

func (f *fakeRunner) withErr(key string, err error) *fakeRunner {
	f.errs[key] = err
	return f
}

func (f *fakeRunner) Run(args ...string) ([]byte, error) {
	key := strings.Join(args, " ")
	f.calls = append(f.calls, key)
	if err, ok := f.errs[key]; ok {
		return nil, err
	}
	if out, ok := f.responses[key]; ok {
		return out, nil
	}
	f.t.Fatalf("fakeRunner: unexpected gh args: %q", key)
	return nil, nil
}

const testRepo = "acme/shop"

func prViewKey() string {
	return fmt.Sprintf("pr view 42 -R %s --json number,url,title,body,headRefName,headRefOid,baseRefName,createdAt,commits", testRepo)
}

// prDiffKey is the combined diff pinned to testdata/pr_view.json's head.
func prDiffKey() string {
	return compareDiffKey("main", strings.Repeat("d", 40))
}

func compareDiffKey(base, head string) string {
	return fmt.Sprintf("api -H Accept: application/vnd.github.diff repos/%s/compare/%s...%s", testRepo, base, head)
}

func commitAPIKey(sha string) string {
	return fmt.Sprintf("api repos/%s/commits/%s", testRepo, sha)
}

func TestFetch_Basic(t *testing.T) {
	r := newFakeRunner(t).
		withFile(prViewKey(), "testdata/pr_view.json").
		withFile(prDiffKey(), "testdata/pr_diff.txt")

	got, err := Fetch(r, testRepo, 42, false)
	if err != nil {
		t.Fatalf("Fetch: %v", err)
	}

	if got.Repo != testRepo || got.Number != 42 || got.URL != "https://github.com/acme/shop/pull/42" {
		t.Errorf("Repo/Number/URL = %q/%d/%q", got.Repo, got.Number, got.URL)
	}
	if got.Title != "Add checkout flow" || got.Body != "This adds the checkout flow." {
		t.Errorf("Title/Body = %q/%q", got.Title, got.Body)
	}
	if got.HeadRef != "feature/checkout" || got.BaseRef != "main" {
		t.Errorf("HeadRef/BaseRef = %q/%q", got.HeadRef, got.BaseRef)
	}
	// The head comes from headRefOid (lowercased), not from the last commit.
	if want := strings.Repeat("d", 40); got.HeadSHA != want {
		t.Errorf("HeadSHA = %q, want %q", got.HeadSHA, want)
	}
	wantCreated := time.Date(2026, 9, 20, 10, 0, 0, 0, time.UTC)
	if !got.CreatedAt.Equal(wantCreated) {
		t.Errorf("CreatedAt = %v, want %v", got.CreatedAt, wantCreated)
	}

	if len(got.Commits) != 2 {
		t.Fatalf("len(Commits) = %d, want 2", len(got.Commits))
	}
	c0 := got.Commits[0]
	if c0.SHA != "aaaaaaa1111111111111111111111111111111" || c0.Message != "Add checkout handler" {
		t.Errorf("commit0 = %+v", c0)
	}
	c1 := got.Commits[1]
	wantMsg1 := "Fix checkout validation\n\nHandles empty cart edge case."
	if c1.SHA != "bbbbbbb2222222222222222222222222222222" || c1.Message != wantMsg1 {
		t.Errorf("commit1 = %+v, want message %q", c1, wantMsg1)
	}
	if c0.Files != nil || c1.Files != nil {
		t.Errorf("commit Files should be nil without withCommitPatches")
	}

	if len(got.Files) != 1 {
		t.Fatalf("len(Files) = %d, want 1", len(got.Files))
	}
	f := got.Files[0]
	if f.Path != "checkout.go" || f.Status != model.StatusModified {
		t.Errorf("file = %+v", f)
	}
	if len(f.Hunks) != 1 || len(f.Hunks[0].Lines) != 3 {
		t.Errorf("file hunks = %+v", f.Hunks)
	}
}

func TestFetch_CommitPatches(t *testing.T) {
	r := newFakeRunner(t).
		withFile(prViewKey(), "testdata/pr_view.json").
		withFile(prDiffKey(), "testdata/pr_diff.txt").
		withFile(commitAPIKey("aaaaaaa1111111111111111111111111111111"), "testdata/commit_aaaaaaa.json").
		withFile(commitAPIKey("bbbbbbb2222222222222222222222222222222"), "testdata/commit_bbbbbbb.json")

	got, err := Fetch(r, testRepo, 42, true)
	if err != nil {
		t.Fatalf("Fetch: %v", err)
	}
	if len(got.Commits) != 2 {
		t.Fatalf("len(Commits) = %d, want 2", len(got.Commits))
	}

	c0 := got.Commits[0]
	if len(c0.Files) != 1 {
		t.Fatalf("commit0 Files = %+v, want 1", c0.Files)
	}
	if c0.Files[0].Path != "checkout.go" || c0.Files[0].Status != model.StatusAdded {
		t.Errorf("commit0 file = %+v", c0.Files[0])
	}
	if len(c0.Files[0].Hunks) != 1 || len(c0.Files[0].Hunks[0].Lines) != 2 {
		t.Errorf("commit0 file hunks = %+v", c0.Files[0].Hunks)
	}

	c1 := got.Commits[1]
	if len(c1.Files) != 3 {
		t.Fatalf("commit1 Files = %+v, want 3", c1.Files)
	}
	mod := c1.Files[0]
	if mod.Path != "validate.go" || mod.Status != model.StatusModified {
		t.Errorf("commit1 file0 = %+v", mod)
	}
	if len(mod.Hunks) != 1 || len(mod.Hunks[0].Lines) != 3 {
		t.Errorf("commit1 file0 hunks = %+v", mod.Hunks)
	}
	ren := c1.Files[1]
	if ren.Path != "newname.go" || ren.OldPath != "oldname.go" || ren.Status != model.StatusRenamed {
		t.Errorf("commit1 file1 = %+v", ren)
	}
	if len(ren.Hunks) != 1 || len(ren.Hunks[0].Lines) != 2 {
		t.Errorf("commit1 file1 hunks = %+v", ren.Hunks)
	}
	bin := c1.Files[2]
	if bin.Path != "binary.png" || !bin.Binary || len(bin.Hunks) != 0 {
		t.Errorf("commit1 file2 (no patch) = %+v, want Binary:true, no hunks", bin)
	}
}

func TestFetch_CommitLimit(t *testing.T) {
	const n = 101
	var sb strings.Builder
	sb.WriteString(`{"number":7,"url":"https://github.com/acme/shop/pull/7","title":"t","body":"","headRefName":"h","headRefOid":"` + strings.Repeat("f", 40) + `","baseRefName":"main","createdAt":"2026-09-20T10:00:00Z","commits":[`)
	for i := 0; i < n; i++ {
		if i > 0 {
			sb.WriteByte(',')
		}
		sha := fmt.Sprintf("%040d", i)
		sb.WriteString(fmt.Sprintf(`{"oid":%q,"messageHeadline":"c%d","messageBody":"","committedDate":"2026-09-20T10:00:00Z"}`, sha, i))
	}
	sb.WriteString(`]}`)

	r := newFakeRunner(t)
	r.responses[fmt.Sprintf("pr view 7 -R %s --json number,url,title,body,headRefName,headRefOid,baseRefName,createdAt,commits", testRepo)] = []byte(sb.String())
	r.responses[compareDiffKey("main", strings.Repeat("f", 40))] = []byte("")
	for i := 0; i < 100; i++ {
		sha := fmt.Sprintf("%040d", i)
		r.responses[commitAPIKey(sha)] = []byte(`{"files":[]}`)
	}
	// Deliberately no response registered for commit 100 (the 101st commit);
	// fakeRunner.Run calls t.Fatalf if Fetch tries to fetch it.

	got, err := Fetch(r, testRepo, 7, true)
	if err != nil {
		t.Fatalf("Fetch: %v", err)
	}
	if len(got.Commits) != n {
		t.Fatalf("len(Commits) = %d, want %d", len(got.Commits), n)
	}

	apiCalls := 0
	for _, c := range r.calls {
		if strings.HasPrefix(c, "api repos/") {
			apiCalls++
		}
	}
	if apiCalls != 100 {
		t.Errorf("api calls = %d, want 100", apiCalls)
	}
	if got.Commits[100].Files != nil {
		t.Errorf("commit 100 (101st): Files = %+v, want nil (skipped, no error)", got.Commits[100].Files)
	}
	// GitHub caps the commit list, so the head is not necessarily its last entry.
	if got.HeadSHA != strings.Repeat("f", 40) || got.HeadSHA == got.Commits[len(got.Commits)-1].SHA {
		t.Errorf("HeadSHA = %q, want headRefOid", got.HeadSHA)
	}
}

func TestFetch_NoHeadRefOid(t *testing.T) {
	r := newFakeRunner(t)
	r.responses[prViewKey()] = []byte(`{"number":42,"url":"u","title":"t","body":"","headRefName":"h","baseRefName":"main","createdAt":"2026-09-20T10:00:00Z","commits":[]}`)
	// With no head to pin the diff to, gh pr diff is the only source.
	r.responses[fmt.Sprintf("pr diff 42 -R %s", testRepo)] = []byte("")
	got, err := Fetch(r, testRepo, 42, false)
	if err != nil {
		t.Fatalf("Fetch: %v", err)
	}
	if got.HeadSHA != "" {
		t.Errorf("HeadSHA = %q, want empty when gh sends no headRefOid", got.HeadSHA)
	}
}

func prFilesKey() string {
	return fmt.Sprintf("api --paginate repos/%s/pulls/42/files?per_page=100", testRepo)
}

// tooLargeDiffErr is the error GH.Run returns when GitHub will not render a
// PR's combined diff.
var tooLargeDiffErr = errors.New("gh pr diff 42 -R acme/shop: exit status 1: could not find pull request diff: " +
	"HTTP 406: Sorry, the diff exceeded the maximum number of lines (20000) " +
	"(https://api.github.com/repos/acme/shop/pulls/42)\nPullRequest.diff too_large")

// pagedFiles concatenates the files API pages as `gh api --paginate` prints them.
func pagedFiles(t *testing.T, paths ...string) []byte {
	t.Helper()
	var out []byte
	for _, p := range paths {
		data, err := os.ReadFile(p)
		if err != nil {
			t.Fatalf("reading testdata %s: %v", p, err)
		}
		out = append(out, data...)
	}
	return out
}

func TestFetch_DiffTooLargeUsesFilesAPI(t *testing.T) {
	r := newFakeRunner(t).
		withFile(prViewKey(), "testdata/pr_view.json").
		withErr(prDiffKey(), tooLargeDiffErr)
	r.responses[prFilesKey()] = pagedFiles(t, "testdata/pr_files_page1.json", "testdata/pr_files_page2.json")

	got, err := Fetch(r, testRepo, 42, false)
	if err != nil {
		t.Fatalf("Fetch: %v", err)
	}
	if got.Title != "Add checkout flow" || len(got.Commits) != 2 {
		t.Errorf("metadata = %q with %d commits", got.Title, len(got.Commits))
	}
	if len(got.Files) != 4 {
		t.Fatalf("len(Files) = %d, want 4 across both pages", len(got.Files))
	}

	modified, renamed, removed, huge := got.Files[0], got.Files[1], got.Files[2], got.Files[3]
	if modified.Path != "checkout.go" || modified.Status != model.StatusModified ||
		len(modified.Hunks) != 1 || len(modified.Hunks[0].Lines) != 3 {
		t.Errorf("modified = %+v", modified)
	}
	if renamed.Path != "cart/new.go" || renamed.OldPath != "cart/old.go" || renamed.Status != model.StatusRenamed ||
		len(renamed.Hunks) != 1 {
		t.Errorf("renamed = %+v", renamed)
	}
	if removed.Path != "legacy/plan.go" || removed.Status != model.StatusDeleted || len(removed.Hunks) != 1 {
		t.Errorf("removed = %+v", removed)
	}
	// GitHub omits the patch of a very large file; the file is kept without hunks.
	if huge.Path != "fixtures/huge.json" || !huge.Binary || len(huge.Hunks) != 0 {
		t.Errorf("patchless file = %+v", huge)
	}
	// Which endpoint answered decides whether that Binary means anything: here
	// it means "GitHub sent no patch", not "the file is binary".
	if got.FilesSource != model.SourceAPI {
		t.Errorf("FilesSource = %q, want %q", got.FilesSource, model.SourceAPI)
	}
}

func TestFetch_FilesSourceIsTheDiffWhenItRenders(t *testing.T) {
	r := newFakeRunner(t).
		withFile(prViewKey(), "testdata/pr_view.json").
		withFile(prDiffKey(), "testdata/pr_diff.txt")

	got, err := Fetch(r, testRepo, 42, false)
	if err != nil {
		t.Fatalf("Fetch: %v", err)
	}
	if got.FilesSource != model.SourceDiff {
		t.Errorf("FilesSource = %q, want %q", got.FilesSource, model.SourceDiff)
	}
}

func TestFetch_DiffTooLargeFilesAPIError(t *testing.T) {
	r := newFakeRunner(t).
		withFile(prViewKey(), "testdata/pr_view.json").
		withErr(prDiffKey(), tooLargeDiffErr).
		withErr(prFilesKey(), errors.New("exit status 1: HTTP 502"))

	_, err := Fetch(r, testRepo, 42, false)
	if err == nil {
		t.Fatal("Fetch: err = nil, want non-nil")
	}
	for _, want := range []string{"too_large", "files API", "HTTP 502"} {
		if !strings.Contains(err.Error(), want) {
			t.Errorf("err = %q, want it to mention %q", err, want)
		}
	}
}

func TestFetch_DiffErrorIsNotRetried(t *testing.T) {
	r := newFakeRunner(t).
		withFile(prViewKey(), "testdata/pr_view.json").
		withErr(prDiffKey(), errors.New("exit status 1: HTTP 404: Not Found"))

	_, err := Fetch(r, testRepo, 42, false)
	if err == nil || !strings.Contains(err.Error(), "combined diff") || !strings.Contains(err.Error(), "HTTP 404") {
		t.Fatalf("err = %v, want the combined diff error", err)
	}
	for _, call := range r.calls {
		if strings.HasPrefix(call, "api --paginate") {
			t.Errorf("Fetch called the files API for a diff error that is not too_large: %q", call)
		}
	}
}

func TestFetch_GHError(t *testing.T) {
	r := newFakeRunner(t).
		withErr(prViewKey(), errors.New("exit status 1: GraphQL: Could not resolve to a PullRequest with the number of 42. (repository.pullRequest)"))

	_, err := Fetch(r, testRepo, 42, false)
	if err == nil {
		t.Fatal("Fetch: err = nil, want non-nil")
	}
	if !strings.Contains(err.Error(), "gh pr view") {
		t.Errorf("err = %q, want it to mention gh pr view", err)
	}
	if !strings.Contains(err.Error(), "Could not resolve to a PullRequest") {
		t.Errorf("err = %q, want it to include the underlying gh message", err)
	}
}

// writeGitFixture builds a minimal .git dir (HEAD + config with an origin
// remote) under dir, mirroring internal/gitinfo's test fixtures.
func writeGitFixture(t *testing.T, dir, remoteURL string) {
	t.Helper()
	gitDir := filepath.Join(dir, ".git")
	if err := os.MkdirAll(gitDir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(gitDir, "HEAD"), []byte("ref: refs/heads/main\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	cfg := "[remote \"origin\"]\n\turl = " + remoteURL + "\n"
	if err := os.WriteFile(filepath.Join(gitDir, "config"), []byte(cfg), 0o644); err != nil {
		t.Fatal(err)
	}
}

func TestResolveArg_URL(t *testing.T) {
	dir := t.TempDir()
	writeGitFixture(t, dir, "git@github.com:someone/somewhere.git")

	repo, number, err := ResolveArg(dir, "https://github.com/acme/shop/pull/123")
	if err != nil {
		t.Fatalf("ResolveArg: %v", err)
	}
	if repo != "acme/shop" || number != 123 {
		t.Errorf("repo/number = %q/%d, want acme/shop/123", repo, number)
	}
}

func TestResolveArg_Number(t *testing.T) {
	dir := t.TempDir()
	writeGitFixture(t, dir, "git@github.com:acme/shop.git")

	repo, number, err := ResolveArg(dir, "42")
	if err != nil {
		t.Fatalf("ResolveArg: %v", err)
	}
	if repo != "acme/shop" || number != 42 {
		t.Errorf("repo/number = %q/%d, want acme/shop/42", repo, number)
	}
}

func TestResolveArg_NumberNoRemote(t *testing.T) {
	dir := t.TempDir()
	gitDir := filepath.Join(dir, ".git")
	if err := os.MkdirAll(gitDir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(gitDir, "HEAD"), []byte("ref: refs/heads/main\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	_, _, err := ResolveArg(dir, "42")
	if err == nil {
		t.Fatal("ResolveArg: err = nil, want non-nil (no remote configured)")
	}
}

func TestResolveArg_NotAPRArg(t *testing.T) {
	dir := t.TempDir()
	_, _, err := ResolveArg(dir, "not-a-number-or-url")
	if err == nil {
		t.Fatal("ResolveArg: err = nil, want non-nil")
	}
}

func TestHeadSHA(t *testing.T) {
	key := "pr view 42 -R acme/shop --json headRefOid --jq .headRefOid"
	r := newFakeRunner(t)
	r.responses[key] = []byte(strings.ToUpper(strings.Repeat("ab", 20)) + "\n")
	got, err := HeadSHA(r, testRepo, 42)
	if err != nil || got != strings.Repeat("ab", 20) {
		t.Fatalf("HeadSHA = %q, %v", got, err)
	}

	r.responses[key] = []byte("not-a-sha\n")
	if _, err := HeadSHA(r, testRepo, 42); err == nil {
		t.Error("an invalid head was accepted")
	}
	r.errs[key] = errors.New("gh failed")
	if _, err := HeadSHA(r, testRepo, 42); err == nil {
		t.Error("a gh failure was swallowed")
	}
}

func TestState(t *testing.T) {
	key := "pr view 42 -R acme/shop --json state --jq .state"
	for _, want := range []string{"OPEN", "MERGED", "CLOSED"} {
		r := newFakeRunner(t)
		r.responses[key] = []byte(want + "\n")
		if got, err := State(r, testRepo, 42); err != nil || got != want {
			t.Errorf("State = %q, %v, want %q", got, err, want)
		}
	}

	r := newFakeRunner(t)
	r.responses[key] = []byte("\n")
	if _, err := State(r, testRepo, 42); err == nil {
		t.Error("an empty state was accepted")
	}
	r.errs[key] = errors.New("gh failed")
	if _, err := State(r, testRepo, 42); err == nil {
		t.Error("a gh failure was swallowed")
	}
}

// pushingGitHub is a fake GitHub where someone pushes right after gh pr view
// answers: the head view returned is already stale when the diff or the files
// are asked for. Every source renders the file at the head it reads.
type pushingGitHub struct {
	t        *testing.T
	heads    []string // heads[i] is the PR head after i pushes
	pushes   int      // pushes left to make, one after each view
	tooLarge bool     // GitHub renders no combined diff, so the files API answers
	at       int      // index of the current head in heads
	views    int
}

func (g *pushingGitHub) Run(args ...string) ([]byte, error) {
	key := strings.Join(args, " ")
	head := g.heads[g.at]
	switch {
	case key == prViewKey():
		g.views++
		if g.pushes > 0 {
			g.pushes--
			g.at++
		}
		return []byte(fmt.Sprintf(`{"number":42,"url":"u","title":"t","body":"","headRefName":"h",`+
			`"headRefOid":%q,"baseRefName":"main","createdAt":"2026-09-20T10:00:00Z",`+
			`"commits":[{"oid":%q,"messageHeadline":"m","committedDate":"2026-09-20T10:05:00Z"}]}`, head, head)), nil
	case key == fmt.Sprintf("pr diff 42 -R %s", testRepo):
		return g.diff(head)
	case strings.HasPrefix(key, compareDiffKey("main", "")):
		return g.diff(strings.TrimPrefix(key, compareDiffKey("main", "")))
	case key == prFilesKey():
		return []byte(fmt.Sprintf(`[{"filename":"src/audio.py","status":"modified","patch":%q,`+
			`"contents_url":"https://api.github.com/repos/acme/shop/contents/src%%2Faudio.py?ref=%s"}]`,
			"@@ -1,1 +1,1 @@\n-EOT_MIN_SILENCE_MS = 1000\n+"+silenceAt(head), head)), nil
	}
	g.t.Fatalf("pushingGitHub: unexpected gh args: %q", key)
	return nil, nil
}

func (g *pushingGitHub) diff(head string) ([]byte, error) {
	if g.tooLarge {
		return nil, errors.New("gh: The diff is taking too long to generate. (HTTP 422)")
	}
	return []byte("diff --git a/src/audio.py b/src/audio.py\n--- a/src/audio.py\n+++ b/src/audio.py\n" +
		"@@ -1,1 +1,1 @@\n-EOT_MIN_SILENCE_MS = 1000\n+" + silenceAt(head) + "\n"), nil
}

// silenceAt is the line src/audio.py adds at head.
func silenceAt(head string) string { return "EOT_MIN_SILENCE_MS = " + head[:7] }

// TestFetch_HeadMovesDuringFetch checks that the files Fetch returns are
// always the ones at the head it reports, however a push lands.
func TestFetch_HeadMovesDuringFetch(t *testing.T) {
	var heads []string
	for _, c := range "abcd" {
		heads = append(heads, strings.Repeat(string(c), 40))
	}
	tests := []struct {
		name      string
		pushes    int
		tooLarge  bool
		wantViews int
		wantErr   string
	}{
		{name: "no push", pushes: 0, wantViews: 1},
		{name: "push after the view", pushes: 1, wantViews: 1},
		{name: "pushes after every view", pushes: 3, wantViews: 1},
		{name: "files API, no push", pushes: 0, tooLarge: true, wantViews: 1},
		{name: "files API, push after the view", pushes: 1, tooLarge: true, wantViews: 2},
		{name: "files API, pushes after every view", pushes: 3, tooLarge: true, wantViews: maxFetchAttempts,
			wantErr: "kept moving"},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			g := &pushingGitHub{t: t, heads: heads, pushes: tc.pushes, tooLarge: tc.tooLarge}
			got, err := Fetch(g, testRepo, 42, false)
			if g.views != tc.wantViews {
				t.Errorf("gh pr view calls = %d, want %d", g.views, tc.wantViews)
			}
			if tc.wantErr != "" {
				if err == nil || !strings.Contains(err.Error(), tc.wantErr) {
					t.Fatalf("Fetch err = %v, want one mentioning %q", err, tc.wantErr)
				}
				return
			}
			if err != nil {
				t.Fatalf("Fetch: %v", err)
			}
			if len(got.Files) != 1 {
				t.Fatalf("len(Files) = %d, want 1", len(got.Files))
			}
			added := got.Files[0].AddedLines()
			if want := silenceAt(got.HeadSHA); len(added) != 1 || added[0].Text != want {
				t.Errorf("added lines = %+v, want the line at head %s: %q", added, got.HeadSHA, want)
			}
		})
	}
}
