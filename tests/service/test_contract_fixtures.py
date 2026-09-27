"""The copies of the contract fixtures in tests/fixtures/bundle/ match the
originals byte-for-byte. If the contract changes, this test breaks first and
forces a resync rather than letting the copies drift silently.
"""

import json
from pathlib import Path

CONTRACT_FIXTURES = Path(
    "/Users/kousik/awt/workspaces/pair-poc/contract/tests/fixtures/bundle"
)
LOCAL_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "bundle"


def test_all_contract_fixtures_are_copied_here():
    contract_files = {p.name for p in CONTRACT_FIXTURES.iterdir() if p.suffix == ".json"}
    local_files = {p.name for p in LOCAL_FIXTURES.iterdir() if p.suffix == ".json"}
    assert contract_files == local_files, (contract_files ^ local_files)


def test_fixture_contents_agree_with_contract():
    for name in ("manifest-queued.json", "manifest-running.json",
                 "manifest-done.json", "manifest-failed.json",
                 "submit-response-accepted.json", "submit-response-duplicate.json",
                 "review-response-queued.json", "review-response-done.json"):
        expected = json.loads((CONTRACT_FIXTURES / name).read_text())
        got = json.loads((LOCAL_FIXTURES / name).read_text())
        assert got == expected, f"local fixture {name} drifted from the contract"


def test_manifest_fixtures_have_all_five_sections():
    for name in ("manifest-queued.json", "manifest-running.json",
                 "manifest-done.json", "manifest-failed.json"):
        m = json.loads((LOCAL_FIXTURES / name).read_text())
        assert set(m["sections"]) == {
            "summary", "look_first", "plan_vs_result", "pushback", "side_effects",
        }
        for state in m["sections"].values():
            # `error` is `null`, never an empty string, per contract.
            assert state["error"] is None or isinstance(state["error"], str)
            assert state["error"] != ""
