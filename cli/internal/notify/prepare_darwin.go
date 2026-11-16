package notify

import (
	"context"
	_ "embed"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"time"

	"github.com/interviewstreet/pair-plugin/cli/internal/state"
)

//go:embed assets/terminal-notifier-macos
var helperExecutable []byte

//go:embed assets/pair.icns
var helperIcon []byte

//go:embed assets/terminal-notifier-LICENSE.txt
var helperLicense []byte

// Prepare installs the bundled, Pair-branded notification helper without a package manager or network.
func Prepare() (string, error) {
	home, err := os.UserHomeDir()
	if err != nil {
		return "", err
	}
	if !filepath.IsAbs(home) {
		return "", errors.New("notification helper needs an absolute home directory")
	}
	lock, err := state.TryLock("notification-helper")
	if errors.Is(err, state.ErrLocked) {
		return "Pair notifications are being prepared", nil
	}
	if err != nil {
		return "", err
	}
	defer lock.Release()
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	return installBundle(home, notificationBundle{helperExecutable, helperSHA256, helperIcon, helperLicense}, func(argv []string) error {
		output, err := exec.CommandContext(ctx, argv[0], argv[1:]...).CombinedOutput()
		if err != nil {
			return fmt.Errorf("prepare notification helper: %w: %s", err, output)
		}
		return nil
	})
}
