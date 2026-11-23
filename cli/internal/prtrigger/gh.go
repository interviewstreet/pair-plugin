package prtrigger

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"time"
)

// resolveTimeout bounds `gh pr view`: the hook runs inside the agent's tool
// loop, so it must not wait on a slow network.
var resolveTimeout = 3 * time.Second

// ResolveCurrentBranchPR returns the open pull request for the branch checked
// out in dir, using `gh pr view`. It fails when gh is missing, dir is not a
// directory, the branch has no PR, the branch's PR is not open (gh names the
// newest PR in any state, so a branch left checked out after its PR merged
// would otherwise keep publishing that PR), or gh takes longer than three
// seconds.
func ResolveCurrentBranchPR(dir string) (PR, error) {
	gh, err := exec.LookPath("gh")
	if err != nil {
		return PR{}, errors.New("gh not found")
	}
	if info, err := os.Stat(dir); err != nil || !info.IsDir() {
		return PR{}, fmt.Errorf("%s is not a directory", dir)
	}
	ctx, cancel := context.WithTimeout(context.Background(), resolveTimeout)
	defer cancel()
	cmd := exec.CommandContext(ctx, gh, "pr", "view", "--json", "state,url")
	cmd.Dir = dir
	cmd.WaitDelay = time.Second // stdin is nil, i.e. /dev/null
	out, err := cmd.Output()
	if err != nil {
		return PR{}, fmt.Errorf("gh pr view: %w", err)
	}
	var view struct {
		State string `json:"state"`
		URL   string `json:"url"`
	}
	if err := json.Unmarshal(out, &view); err != nil {
		return PR{}, fmt.Errorf("gh pr view: %w", err)
	}
	if view.State != "OPEN" {
		return PR{}, fmt.Errorf("the branch's pull request is not open (state %q)", view.State)
	}
	pr, ok := singlePR(view.URL)
	if !ok {
		return PR{}, errors.New("gh pr view did not print exactly one pull request URL")
	}
	return pr, nil
}
