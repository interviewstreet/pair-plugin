package selfupdate

import (
	"net/url"
	"testing"
)

func TestReleaseRepositoryIsPlugin(test *testing.T) {
	if DefaultManifestURL != "https://github.com/interviewstreet/pair-plugin/releases/latest/download/latest.json" {
		test.Fatalf("unexpected release manifest: %s", DefaultManifestURL)
	}
	target, err := url.Parse("https://release-assets.githubusercontent.com/asset")
	if err != nil {
		test.Fatal(err)
	}
	for _, repository := range []string{"interviewstreet/pair-plugin", "interviewstreet/paircli", "rutvikchandla3/paircli"} {
		origin, parseErr := url.Parse("https://github.com/" + repository + "/releases/download/v0.5.0/paircli-darwin-arm64")
		if parseErr != nil {
			test.Fatal(parseErr)
		}
		if got, want := isGitHubReleaseAssetRedirect(origin, target), repository == "interviewstreet/pair-plugin"; got != want {
			test.Errorf("asset redirect allowed for %s = %t, want %t", repository, got, want)
		}
	}
}
