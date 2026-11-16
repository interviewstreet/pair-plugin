//go:build !darwin

package notify

// Prepare is a no-op on platforms that do not need the macOS helper.
func Prepare() (string, error) { return "not needed on this platform", nil }
