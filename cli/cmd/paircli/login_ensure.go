package main

import (
	"errors"
	"io"
	"strings"

	"github.com/interviewstreet/pair-plugin/cli/internal/deviceauth"
)

// ensureLogin returns the credential for server, running the device login
// first when there is none. A PAIRCLI_AUTH_TOKEN credential carries no server
// and is accepted for any. noBrowser prints the login URL without opening it.
func ensureLogin(server string, out, progress io.Writer, noBrowser bool) (deviceauth.StoredAuth, error) {
	server = strings.TrimRight(server, "/")
	if a, ok := deviceauth.Resolve(); ok && (a.ServerURL == "" || strings.TrimRight(a.ServerURL, "/") == server) {
		return a, nil
	}
	args := []string{"--server", server}
	if noBrowser {
		args = append(args, "--no-browser")
	}
	if err := loginRun(args, out, progress); err != nil {
		return deviceauth.StoredAuth{}, err
	}
	a, ok := deviceauth.Resolve()
	if !ok {
		return deviceauth.StoredAuth{}, errors.New("login completed without a usable credential")
	}
	return a, nil
}
