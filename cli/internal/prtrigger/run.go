// Package prtrigger turns a PostToolUse hook event into a background `paircli
// publish`: it recognises a successful `gh pr create` or `git push` in a shell
// command, finds the pull request it touched, starts the publish detached and
// builds the one JSON object the hook prints for the agent.
package prtrigger

import (
	"encoding/json"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
	"syscall"
	"time"

	"github.com/interviewstreet/pair-plugin/cli/internal/gitinfo"
	"github.com/interviewstreet/pair-plugin/cli/internal/state"
)

// Output is the JSON object a PostToolUse hook prints: a message for the user
// and the same fact as context for the agent.
type Output struct {
	SystemMessage      string             `json:"systemMessage"`
	HookSpecificOutput HookSpecificOutput `json:"hookSpecificOutput"`
}

// HookSpecificOutput is the PostToolUse part of Output.
type HookSpecificOutput struct {
	HookEventName     string `json:"hookEventName"`
	AdditionalContext string `json:"additionalContext"`
}

// Runner is a PostToolUse trigger. Its fields are the only side effects, so
// tests can supply fakes.
type Runner struct {
	// Resolve returns the open PR for the branch checked out in a directory.
	Resolve func(dir string) (PR, error)
	// Start launches `paircli publish` for a PR detached from this process.
	// head is the commit a push sent, or "" when there is none to wait for.
	Start func(pr PR, dir, head string) error
	// Head returns the commit checked out in a directory. nil reads none.
	Head func(dir string) (string, error)
}

// Default is the Runner the installed hooks use.
var Default = Runner{Resolve: ResolveCurrentBranchPR, Start: StartPublish, Head: LocalHead}

// shaRe matches a full lowercase commit SHA.
var shaRe = regexp.MustCompile(`^[0-9a-f]{40}$`)

// Handle starts a publish when ev is a successful `gh pr create` or `git push`
// on a PR branch. It returns nil when there is nothing to do. A push also hands
// publish the commit it sent, so publish can wait for GitHub to show it.
func (r Runner) Handle(ev Event) (*Output, error) {
	op, ok := CommandOperation(ev.Command)
	if !ok {
		return nil, nil
	}
	dir, err := filepath.Abs(RepoDir(ev.CWD, ev.Command))
	if err != nil {
		return nil, err
	}
	pr, ok := PRFromEvent(ev, dir, r.Resolve)
	if !ok {
		return nil, nil
	}
	head := ""
	if op.Kind == OpPush {
		head = r.pushedHead(dir)
	}
	if err := r.Start(pr, dir, head); err != nil {
		return nil, fmt.Errorf("start publish for %s: %w", pr.URL(), err)
	}
	url := pr.ReviewURL()
	return &Output{
		SystemMessage: "Pair is publishing this PR's review in the background. Pair review: " + url,
		HookSpecificOutput: HookSpecificOutput{
			HookEventName: "PostToolUse",
			AdditionalContext: "Pair is publishing the review for this pull request in the background. " +
				"Pair review: " + url + ". Share this link with the user.",
		},
	}, nil
}

// pushedHead is the commit a push from dir sent: the branch's local head, which
// the push has just made the remote's. "" when it cannot be read; publish then
// takes whatever head GitHub reports, as it always did.
func (r Runner) pushedHead(dir string) string {
	if r.Head == nil {
		return ""
	}
	sha, err := r.Head(dir)
	if sha = strings.ToLower(strings.TrimSpace(sha)); err != nil || !shaRe.MatchString(sha) {
		return ""
	}
	return sha
}

// LocalHead returns the commit checked out in dir, bounded by gitinfo's hook
// timeout.
func LocalHead(dir string) (string, error) {
	sha, _, err := gitinfo.HeadAndBranch(dir)
	if sha == "" {
		return "", err
	}
	return sha, nil
}

// Run is Handle plus printing: the Output, if any, is written to w as one line
// of JSON. Nothing is written when there is nothing to do or Handle fails.
func (r Runner) Run(ev Event, w io.Writer) error {
	out, err := r.Handle(ev)
	if err != nil || out == nil {
		return err
	}
	return json.NewEncoder(w).Encode(out)
}

// StartPublish runs `paircli publish <pr-url> --repo-dir <dir> --wait` in a new
// session so it outlives the hook, with stdout and stderr going to a
// timestamped log under state.LogDir(). --wait (with the default timeout) keeps
// the process alive after the upload until the review is ready, which is when
// it sends the notification that says so.
func StartPublish(pr PR, dir, head string) error {
	exe, err := os.Executable()
	if err != nil {
		return err
	}
	return startDetached(exe, pr, dir, head)
}

// PublishArgs is the arguments (after the binary) of the detached publish: the
// PR, its repository directory, --wait with the default timeout, --if-open so
// a PR merged or closed by the time the publish starts is left alone and,
// after a push, --expect-head with the pushed commit.
func PublishArgs(pr PR, dir, head string) []string {
	args := []string{"publish", pr.URL(), "--repo-dir", dir, "--wait", "--if-open"}
	if head != "" {
		args = append(args, "--expect-head", head)
	}
	return args
}

// startDetached is StartPublish for an explicit paircli binary.
func startDetached(exe string, pr PR, dir, head string) error {
	logs := state.LogDir()
	if logs == "" {
		return fmt.Errorf("no log directory")
	}
	if err := os.MkdirAll(logs, 0o700); err != nil {
		return err
	}
	name := fmt.Sprintf("publish-%s-%s.log", state.Key(pr.Owner, pr.Repo, pr.Number), time.Now().Format("20060102-150405"))
	log, err := os.OpenFile(filepath.Join(logs, name), os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o600)
	if err != nil {
		return err
	}
	defer log.Close()

	cmd := exec.Command(exe, PublishArgs(pr, dir, head)...)
	cmd.Stdout = log
	cmd.Stderr = log
	cmd.SysProcAttr = &syscall.SysProcAttr{Setsid: true}
	if err := cmd.Start(); err != nil {
		return err
	}
	return cmd.Process.Release()
}
