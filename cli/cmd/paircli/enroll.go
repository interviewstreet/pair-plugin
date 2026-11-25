package main

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"time"

	"github.com/interviewstreet/pair-plugin/cli/internal/codex"
	"github.com/interviewstreet/pair-plugin/cli/internal/maintain"
	"github.com/interviewstreet/pair-plugin/cli/internal/state"
)

// initRecord is ~/.paircli/state/init.json: this machine was set up by
// `paircli init` (the installer path). Its presence is what turns on
// maintain; plugin-only machines never have one.
type initRecord struct {
	SchemaVersion int               `json:"schema_version"`
	Harnesses     map[string]string `json:"harnesses"`
	CodexTrust    string            `json:"codex_trust,omitempty"`
	// CodexPath is the codex binary init resolved, tried first later: a
	// SessionStart hook's PATH can be narrower than the user's shell.
	CodexPath string `json:"codex_path,omitempty"`
	// Trust is Pair's memory of each Codex hook-trust key, so maintain never
	// grants again a key the user revoked.
	Trust      codex.TrustRecord `json:"trust,omitempty"`
	EnrolledAt time.Time         `json:"enrolled_at"`
	UpdatedAt  time.Time         `json:"updated_at"`
}

func initRecordPath() string { return maintain.InitPath() }

// readInitRecord loads init.json. A missing file is os.ErrNotExist.
func readInitRecord() (initRecord, error) {
	var r initRecord
	data, err := os.ReadFile(initRecordPath())
	if err != nil {
		return r, err
	}
	return r, json.Unmarshal(data, &r)
}

// initEnrolled reports whether init.json exists.
func initEnrolled() bool {
	_, err := os.Stat(initRecordPath())
	return err == nil
}

// writeInitRecord replaces init.json atomically, keeping the first
// enrolment time.
func writeInitRecord(r initRecord) error {
	r.SchemaVersion = state.SchemaVersion
	r.UpdatedAt = time.Now().UTC()
	if r.EnrolledAt.IsZero() {
		r.EnrolledAt = r.UpdatedAt
	}
	return state.WriteJSONAtomic(initRecordPath(), r)
}

// codexTrustTimeout bounds the trust step; a variable so tests can shorten it.
var codexTrustTimeout = codex.DefaultTrustTimeout

// grantCodexTrust is the codex_trust step: it finds codex (init.json's saved
// path first), grants trust and records what it learned in r. trusted is
// false when the user still has to approve the hooks in Codex's /hooks.
func grantCodexTrust(r *initRecord, explicit bool) (outcome string, trusted bool) {
	// init looks on $PATH first (the user may have upgraded codex); maintain,
	// started from a hook with a narrower PATH, tries the saved path first.
	bin := codex.FindCodex(r.CodexPath)
	if explicit {
		if onPath := codex.FindCodex(""); onPath != "" {
			bin = onPath
		}
	}
	if bin == "" {
		return "skipped: codex binary not found", false
	}
	r.CodexPath = bin
	home, _ := os.UserHomeDir()
	res := codex.GrantTrust(context.Background(), codex.GrantOptions{
		Codex: bin, Home: home, Explicit: explicit, Record: r.Trust,
		Timeout: codexTrustTimeout, ClientVersion: version,
	})
	r.Trust = res.Record
	return res.String(), res.Trusted
}

// errInitIncomplete is returned by init when it set up what it could but did
// not finish; its message is already printed.
var errInitIncomplete = errors.New("init did not finish")
