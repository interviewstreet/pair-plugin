package notify

import (
	"errors"
	"os/exec"
	"path/filepath"
	"reflect"
	"runtime"
	"testing"
)

func lookup(found ...string) func(string) (string, error) {
	return func(name string) (string, error) {
		for _, f := range found {
			if f == name {
				if filepath.IsAbs(name) {
					return name, nil
				}
				return "/usr/bin/" + name, nil
			}
		}
		return "", exec.ErrNotFound
	}
}

func TestMacOSReviewNotificationOpensTheReviewURL(t *testing.T) {
	reviewURL := "https://pair.sh/interviewstreet/coderunner/pull/1182"
	for _, title := range []string{"Pair review ready", "PR #1182 uploaded to Pair", `Title \"quoted\"; echo nope`} {
		t.Run(title, func(t *testing.T) {
			got := command("darwin", lookup("terminal-notifier", "osascript"), title, reviewURL)
			want := []string{"/usr/bin/terminal-notifier", "-title", title, "-message", reviewURL, "-open", reviewURL}
			if !reflect.DeepEqual(got, want) {
				t.Errorf("command = %q, want %q", got, want)
			}
		})
	}
}

func TestMacOSFindsHomebrewNotifierWithoutShellPATH(t *testing.T) {
	for _, bin := range []string{"/opt/homebrew/bin/terminal-notifier", "/usr/local/bin/terminal-notifier"} {
		t.Run(bin, func(t *testing.T) {
			got := command("darwin", lookup(bin, "osascript"), "Pair review ready", "https://pair.sh/acme/shop/pull/7")
			if len(got) == 0 || got[0] != bin {
				t.Fatalf("command = %q, want Homebrew notifier %q", got, bin)
			}
		})
	}
}

func TestMacOSDoesNotSendReviewLinksThroughScriptEditor(t *testing.T) {
	if got := command("darwin", lookup("osascript"), "Pair review ready", "https://pair.sh/acme/shop/pull/7"); got != nil {
		t.Fatalf("link notification without a clickable notifier = %q, want nil", got)
	}
}

func TestReviewTarget(t *testing.T) {
	for _, target := range []string{
		"https://pair.sh/acme/shop/pull/7",
		"https://www.pair.sh/acme/shop/pull/7/",
		"https://private.example.test/acme/shop/pull/7",
		"http://localhost:8080/acme/shop/pull/7",
	} {
		t.Run(target, func(t *testing.T) {
			if got := reviewTarget(target); got != target {
				t.Errorf("reviewTarget = %q, want %q", got, target)
			}
		})
	}
	for _, message := range []string{
		"", "PR #7 failed", "https://pair.sh/acme/shop/pull/7; open /tmp",
		"file:///tmp/demo", "javascript:alert(1)", "pair://acme/shop/pull/7",
		"https://user:password@pair.sh/acme/shop/pull/7",
		"https://pair.sh/acme/shop/pull/7?token=secret",
		"https://pair.sh/acme/shop/pull/7?", "https://pair.sh/acme/shop/pull/7#details",
		"https://pair.sh/acme/shop/pull/0", "https://pair.sh/acme/shop/pull/7/extra",
		"https://pair.sh/acme/shop/pull/%37", "https://pair.sh/", "-execute open /tmp",
	} {
		t.Run(message, func(t *testing.T) {
			if got := reviewTarget(message); got != "" {
				t.Errorf("reviewTarget = %q, want no click action", got)
			}
			got := command("darwin", lookup("terminal-notifier"), "Pair", message)
			want := []string{"/usr/bin/terminal-notifier", "-title", "Pair", "-message", message}
			if !reflect.DeepEqual(got, want) {
				t.Errorf("command = %q, want %q", got, want)
			}
		})
	}
}

func TestMacOSPlainNotificationPrefersNativeNotifier(t *testing.T) {
	got := command("darwin", lookup("terminal-notifier", "osascript"), "Pair review failed", "Run paircli status for details.")
	want := []string{"/usr/bin/terminal-notifier", "-title", "Pair review failed", "-message", "Run paircli status for details."}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("command = %q, want %q", got, want)
	}
}

func TestMacOSUsesArgvSafeOsascript(t *testing.T) {
	got := command("darwin", lookup("osascript"), `Title "quoted"`, `msg"; do shell script "x`)
	want := []string{"/usr/bin/osascript", "-e", macScript, "--", `Title "quoted"`, `msg"; do shell script "x`}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("command = %q, want %q", got, want)
	}
}

func TestLinuxUsesNotifySend(t *testing.T) {
	got := command("linux", lookup("notify-send"), "Pair review ready", "PR #5")
	want := []string{"/usr/bin/notify-send", "--app-name", "Pair", "Pair review ready", "PR #5"}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("command = %q, want %q", got, want)
	}
}

func TestNoNotifierIsANoop(t *testing.T) {
	if command("darwin", lookup(), "t", "m") != nil || command("linux", lookup(), "t", "m") != nil {
		t.Error("a missing notifier binary produced a command")
	}
	if command("windows", lookup("osascript", "notify-send"), "t", "m") != nil {
		t.Error("an unsupported platform produced a command")
	}
}

func TestSendReportsAFailingNotifierAsNotShown(t *testing.T) {
	old := run
	defer func() { run = old }()
	oldPrepare := prepare
	defer func() { prepare = oldPrepare }()
	prepare = func() (string, error) { return "", errors.New("unavailable") }
	run = func([]string) error { return errors.New("boom") }
	if Send("t", "m") {
		t.Error("Send reported success for a failing notifier")
	}
}

func TestMacOSCommandDoesNotTrustHomeHelper(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	bin := pairNotifierPath(home)
	got := command("darwin", lookup(bin, "terminal-notifier"), "Pair review ready", "https://pair.sh/acme/shop/pull/7")
	if len(got) == 0 || got[0] != "/usr/bin/terminal-notifier" {
		t.Fatalf("unvalidated home helper selected: %q", got)
	}
}

func TestMacOSDoesNotResolvePairHelperFromRelativeHome(t *testing.T) {
	t.Setenv("HOME", "relative")
	got := command("darwin", lookup(pairNotifierPath("relative"), "terminal-notifier"), "Pair review ready", "https://pair.sh/acme/shop/pull/7")
	if len(got) == 0 || got[0] != "/usr/bin/terminal-notifier" {
		t.Fatalf("relative home helper was selected: %q", got)
	}
}

func TestSendPreparesHelperForAnAlreadyRunningOldSetupProcess(t *testing.T) {
	oldPrepare, oldRun := prepare, run
	t.Cleanup(func() { prepare, run = oldPrepare, oldRun })
	called := false
	prepare = func() (string, error) { called = true; return "ready", nil }
	run = func([]string) error { return nil }
	Send("Pair review ready", "https://pair.sh/acme/shop/pull/7")
	if called != (runtime.GOOS == "darwin") {
		t.Fatalf("prepare called = %v on %s", called, runtime.GOOS)
	}
}
