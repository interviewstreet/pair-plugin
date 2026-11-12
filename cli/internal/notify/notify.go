// Package notify shows best-effort desktop notifications: terminal-notifier on macOS,
// notify-send on Linux, and nothing anywhere else. A notification that cannot
// be shown is not an error; publishing never depends on it.
package notify

import (
	"context"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"runtime"
	"time"
)

const timeout = 5 * time.Second

var reviewPath = regexp.MustCompile(`^/[A-Za-z0-9-]+/[A-Za-z0-9_.-]+/pull/[1-9][0-9]*/?$`)

var prepare = Prepare

// macScript reads the title and message from argv, so neither can alter the
// script.
const macScript = "on run argv\ndisplay notification (item 2 of argv) with title (item 1 of argv)\nend run"

// run starts a command; tests replace it.
var run = func(argv []string) error {
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()
	return exec.CommandContext(ctx, argv[0], argv[1:]...).Run()
}

// Send shows a notification and reports whether it was shown.
func Send(title, message string) bool {
	var argv []string
	if runtime.GOOS == "darwin" {
		result, err := prepare()
		if err == nil && (result == "Pair notifications installed" || result == "Pair notifications ready") {
			if home, err := os.UserHomeDir(); err == nil && filepath.IsAbs(home) {
				argv = notifierCommand(pairNotifierPath(home), title, message)
			}
		}
	}
	if argv == nil {
		argv = command(runtime.GOOS, exec.LookPath, title, message)
	}
	if argv == nil {
		return false
	}
	return run(argv) == nil
}

// command is the argv that shows the notification on goos, or nil when the
// platform has no supported notifier.
func command(goos string, lookPath func(string) (string, error), title, message string) []string {
	switch goos {
	case "darwin":
		target := reviewTarget(message)
		candidates := []string{"terminal-notifier", "/opt/homebrew/bin/terminal-notifier", "/usr/local/bin/terminal-notifier"}
		for _, name := range candidates {
			if bin, err := lookPath(name); err == nil {
				return notifierCommand(bin, title, message)
			}
		}
		if target != "" {
			return nil
		}
		if bin, err := lookPath("osascript"); err == nil {
			return []string{bin, "-e", macScript, "--", title, message}
		}
	case "linux":
		if bin, err := lookPath("notify-send"); err == nil {
			return []string{bin, "--app-name", "Pair", title, message}
		}
	}
	return nil
}

func notifierCommand(bin, title, message string) []string {
	argv := []string{bin, "-title", title, "-message", message}
	if target := reviewTarget(message); target != "" {
		argv = append(argv, "-open", target)
	}
	return argv
}

// reviewTarget accepts only a standalone HTTP(S) review URL, never a shell command or custom scheme.
func reviewTarget(message string) string {
	parsed, err := url.ParseRequestURI(message)
	if err != nil || (parsed.Scheme != "https" && parsed.Scheme != "http") ||
		parsed.Hostname() == "" || parsed.User != nil || parsed.RawQuery != "" ||
		parsed.ForceQuery || parsed.Fragment != "" || parsed.RawPath != "" ||
		!reviewPath.MatchString(parsed.Path) {
		return ""
	}
	return message
}
