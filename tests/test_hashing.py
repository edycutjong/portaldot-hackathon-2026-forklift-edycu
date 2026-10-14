import pytest

from forklift.hashing import blake2_256, manifest_root, twox128


@pytest.mark.parametrize("name,expected", [
    ("System", "26aa394eea5630e07c48ae0c9558cef7"),
    ("Account", "b99d880ec681799c0cf30e8886371da9"),
    ("MerkleMountainRange", "434e2055f3154c54233a8dabeeb2839e"),
])
def test_twox128_matches_substrate_prefixes(name, expected):
    assert twox128(name).hex() == expected


def test_blake2_256_empty():
    assert blake2_256(b"").hex() == "0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8"


PAIRS = [(b"\x01", b"a"), (b"\x02\x00", b"bb"), (b"\x00\xff", b"")]


def test_manifest_root_is_order_independent():
    assert manifest_root(PAIRS) == manifest_root(reversed(PAIRS)) == manifest_root(sorted(PAIRS))


def test_manifest_root_is_deterministic_and_value_sensitive():
    base = manifest_root(PAIRS)
    assert base == manifest_root(list(PAIRS))
    changed = [(k, v + b"x" if k == b"\x01" else v) for k, v in PAIRS]
    assert manifest_root(changed) != base
    assert manifest_root(PAIRS[:-1]) != base


def test_manifest_root_framing_separates_key_and_value():
    # Without length framing these two stores would hash the same byte stream.
    assert manifest_root([(b"ab", b"c")]) != manifest_root([(b"a", b"bc")])


def test_manifest_root_rejects_duplicate_keys():
    with pytest.raises(ValueError):
        manifest_root([(b"\x01", b"a"), (b"\x01", b"b")])
