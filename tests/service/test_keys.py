"""Key grammar. Split on __ into exactly 4 parts, head part is 12 hex."""

import pytest

from pair.service.keys import BadKey, parse_key


def test_parse_simple_key():
    k = parse_key("interviewstreet__pair-be__62__3453326da379")
    assert (k.owner, k.repo, k.pr, k.head12) == (
        "interviewstreet", "pair-be", 62, "3453326da379",
    )
    assert str(k) == "interviewstreet__pair-be__62__3453326da379"
    assert k.slug == "interviewstreet/pair-be"


def test_owner_and_repo_may_contain_single_underscore():
    # The contract explicitly permits owner/repo names with a single
    # underscore because we split on __ rather than on _.
    k = parse_key("some_org__pair_be__1__abcdef012345")
    assert (k.owner, k.repo, k.pr) == ("some_org", "pair_be", 1)


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "not-a-key",
        "a__b__c",                       # too few parts
        "a__b__1__abcdef012345__extra",  # too many parts
        "a__b__01__abcdef012345",         # leading zero
        "a__b__-1__abcdef012345",         # negative
        "a__b__1__ABCDEF012345",          # uppercase hex not allowed
        "a__b__1__abcdef01234",           # 11 chars
        "a__b__1__abcdef0123456",         # 13 chars
        "a__b__1__abcdefg12345",          # non-hex
        "a b__c__1__abcdef012345",         # whitespace
    ],
)
def test_bad_keys_reject(bad):
    with pytest.raises(BadKey):
        parse_key(bad)
