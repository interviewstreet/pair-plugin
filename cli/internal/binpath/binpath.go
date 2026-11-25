// Package binpath names the paircli binary the way hooks and the plugin
// should call it: by a path that survives an upgrade.
package binpath

import (
	"os"
	"path/filepath"
	"regexp"
)

// cellarRe matches a Homebrew keg binary: <prefix>/Cellar/<formula>/<version>/bin/<name>.
var cellarRe = regexp.MustCompile(`^(.+)/Cellar/[^/]+/[^/]+/bin/([^/]+)$`)

// IsHomebrewKeg reports whether path (symlink-resolved) is a binary inside a
// Homebrew keg, under any prefix (/opt/homebrew, /usr/local, Linuxbrew's
// /home/linuxbrew/.linuxbrew).
func IsHomebrewKeg(path string) bool { return cellarRe.MatchString(filepath.Clean(path)) }

// Stable returns exe, or for a Homebrew keg binary the <prefix>/bin link to
// it, which `brew upgrade` keeps pointing at the current version. The link
// is used only when it exists and resolves to exe.
func Stable(exe string) string {
	m := cellarRe.FindStringSubmatch(exe)
	if m == nil {
		return exe
	}
	link := filepath.Join(m[1], "bin", m[2])
	if real, err := filepath.EvalSymlinks(link); err == nil && real == exe {
		return link
	}
	return exe
}

// Executable is the running binary's symlink-resolved path, made Stable.
func Executable() (string, error) {
	exe, err := os.Executable()
	if err != nil {
		return "", err
	}
	if real, err := filepath.EvalSymlinks(exe); err == nil {
		exe = real
	}
	return Stable(exe), nil
}
