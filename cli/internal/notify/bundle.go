package notify

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

const (
	pairBundleName = "Pair Notifications.app"
	pairBundleID   = "sh.pair.notifications"
	markerName     = "paircli-notifier.sha256"
	markerPrefix   = "paircli-notifier-v1:"
	helperSHA256   = "64c8f37e21b2b39491c0adeb00219a92e0ba22b56355b979d8eed2bce3d1c858"
	pairInfoPlist  = `<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleExecutable</key><string>terminal-notifier</string>
<key>CFBundleIdentifier</key><string>sh.pair.notifications</string>
<key>CFBundleName</key><string>Pair</string>
<key>CFBundleDisplayName</key><string>Pair</string>
<key>CFBundleIconFile</key><string>Pair</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleShortVersionString</key><string>3.1.0</string>
<key>CFBundleVersion</key><string>17</string>
<key>LSMinimumSystemVersion</key><string>10.14</string>
<key>LSUIElement</key><true/>
<key>NSHumanReadableCopyright</key><string>Copyright © 2012-2017 Eloy Durán, Julien Blanchard. All rights reserved.</string>
<key>NSPrincipalClass</key><string>NSApplication</string>
</dict></plist>
`
)

type notificationBundle struct {
	executable []byte
	digest     string
	icon       []byte
	license    []byte
}

func pairNotifierPath(home string) string {
	return filepath.Join(home, "Applications", pairBundleName, "Contents", "MacOS", "terminal-notifier")
}

func bundleMarker(bundle notificationBundle) string {
	hash := sha256.New()
	hash.Write(bundle.executable)
	hash.Write(bundle.icon)
	hash.Write(bundle.license)
	hash.Write([]byte(pairInfoPlist))
	return markerPrefix + hex.EncodeToString(hash.Sum(nil))
}

// installBundle signs a staged, managed app before activating it; no archive is extracted at runtime.
func installBundle(home string, bundle notificationBundle, command func([]string) error) (string, error) {
	digest := sha256.Sum256(bundle.executable)
	if len(bundle.executable) == 0 || hex.EncodeToString(digest[:]) != bundle.digest {
		return "", errors.New("notification helper checksum mismatch")
	}
	if len(bundle.icon) < 8 || string(bundle.icon[:4]) != "icns" || len(bundle.license) == 0 {
		return "", errors.New("notification helper branding is incomplete")
	}
	if !filepath.IsAbs(home) {
		return "", errors.New("notification helper needs an absolute home directory")
	}
	apps := filepath.Join(home, "Applications")
	if info, err := os.Lstat(apps); err == nil && (!info.IsDir() || info.Mode()&os.ModeSymlink != 0) {
		return "", errors.New("Applications is not a regular directory")
	}
	if err := os.MkdirAll(apps, 0o755); err != nil {
		return "", err
	}
	target := filepath.Join(apps, pairBundleName)
	marker, markerErr := os.ReadFile(filepath.Join(target, "Contents", "Resources", markerName))
	if info, err := os.Lstat(target); err == nil {
		if !info.IsDir() || info.Mode()&os.ModeSymlink != 0 || markerErr != nil || !strings.HasPrefix(string(marker), markerPrefix) {
			return "", errors.New("refusing to replace an unmanaged Pair Notifications app")
		}
	} else if !errors.Is(err, os.ErrNotExist) {
		return "", err
	}
	stage, err := os.MkdirTemp(apps, ".pair-notifications-")
	if err != nil {
		return "", err
	}
	cleanup := true
	defer func() {
		if cleanup {
			os.RemoveAll(stage)
		}
	}()
	stagedApp := filepath.Join(stage, pairBundleName)
	files := []struct {
		path string
		data []byte
		mode os.FileMode
	}{
		{"Contents/MacOS/terminal-notifier", bundle.executable, 0o755},
		{"Contents/Info.plist", []byte(pairInfoPlist), 0o644},
		{"Contents/PkgInfo", []byte("APPL????"), 0o644},
		{"Contents/Resources/Pair.icns", bundle.icon, 0o644},
		{"Contents/Resources/terminal-notifier-LICENSE.txt", bundle.license, 0o644},
		{"Contents/Resources/" + markerName, []byte(bundleMarker(bundle)), 0o644},
	}
	for _, file := range files {
		target := filepath.Join(stagedApp, filepath.FromSlash(file.path))
		if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
			return "", err
		}
		if err := os.WriteFile(target, file.data, file.mode); err != nil {
			return "", err
		}
	}
	if err := command([]string{"/usr/bin/codesign", "--force", "--sign", "-", "--timestamp=none", stagedApp}); err != nil {
		return "", err
	}
	if err := command([]string{"/usr/bin/codesign", "--verify", "--strict", stagedApp}); err != nil {
		return "", err
	}
	if string(marker) == bundleMarker(bundle) {
		binaryPath := pairNotifierPath(home)
		binary, binaryErr := os.Lstat(binaryPath)
		if binaryErr == nil && binary.Mode().IsRegular() && binary.Mode().Perm()&0o111 != 0 {
			installed, installedErr := os.ReadFile(binaryPath)
			expected, expectedErr := os.ReadFile(filepath.Join(stagedApp, "Contents", "MacOS", "terminal-notifier"))
			icon, iconErr := os.ReadFile(filepath.Join(target, "Contents", "Resources", "Pair.icns"))
			info, infoErr := os.ReadFile(filepath.Join(target, "Contents", "Info.plist"))
			if installedErr == nil && expectedErr == nil && bytes.Equal(installed, expected) && iconErr == nil && bytes.Equal(icon, bundle.icon) && infoErr == nil && string(info) == pairInfoPlist {
				if err := command([]string{"/usr/bin/codesign", "--verify", "--strict", target}); err == nil {
					return "Pair notifications ready", nil
				}
			}
		}
	}
	backup := filepath.Join(stage, "previous.app")
	previous := false
	if _, err := os.Lstat(target); err == nil {
		if err := os.Rename(target, backup); err != nil {
			return "", err
		}
		previous = true
	}
	if err := os.Rename(stagedApp, target); err != nil {
		if previous {
			if restoreErr := os.Rename(backup, target); restoreErr != nil {
				cleanup = false
				return "", fmt.Errorf("notification install failed: %v; previous app retained at %s: %w", err, backup, restoreErr)
			}
		}
		return "", err
	}
	return "Pair notifications installed", nil
}
