package deviceauth

import (
	"os"
	"os/exec"
	"runtime"
)

// OpenBrowser opens rawURL in the user's browser. It is a package-level
// variable so callers (and tests) can replace it; login treats any error as a
// warning, never a failure.
var OpenBrowser = func(rawURL string) error {
	switch runtime.GOOS {
	case "darwin":
		return exec.Command("open", rawURL).Start()
	case "windows":
		return exec.Command("rundll32", "url.dll,FileProtocolHandler", rawURL).Start()
	default:
		return exec.Command("xdg-open", rawURL).Start()
	}
}

// CanOpenBrowser reports whether this machine looks like it has a browser to
// open: not over SSH, and on Linux a graphical session. On a headless machine
// xdg-open fails, or worse, starts a text browser over the terminal, so login
// prints the URL instead. getenv is os.Getenv outside tests.
func CanOpenBrowser(goos string, getenv func(string) string) bool {
	if getenv("SSH_CONNECTION") != "" || getenv("SSH_TTY") != "" {
		return false
	}
	switch goos {
	case "darwin", "windows":
		return true
	default:
		// WSL opens the Windows browser (wslview) with no display of its own.
		return getenv("DISPLAY") != "" || getenv("WAYLAND_DISPLAY") != "" || getenv("WSL_DISTRO_NAME") != ""
	}
}

// HasBrowser is CanOpenBrowser for this process.
func HasBrowser() bool { return CanOpenBrowser(runtime.GOOS, os.Getenv) }
