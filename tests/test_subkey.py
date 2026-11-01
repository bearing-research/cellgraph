import pytest
from vestigo.provenance import derive_subkey as vestigo_derive_subkey

from cellgraph import derive_subkey

PARENT = "4a13460f" * 8


def test_derive_subkey_is_vestigos_byte_for_byte():
    for labels in [(), ("state",), ("variant=small",), ("content=ab", "iter=2")]:
        assert derive_subkey(PARENT, *labels) == vestigo_derive_subkey(PARENT, *labels)


def test_fixed_vectors():
    # Computed with vestigo 0.3.1's derive_subkey.
    assert derive_subkey("p", "a", "b") == (
        "af88c83cac7b5e948ca71a0600f3bb790ae6f1239e58daff555a0b247dfc0b62"
    )
    assert derive_subkey(PARENT, "variant=small") == (
        "2efa0cbf48f4b949b88c765ef6a01e7afa7a074add79cf53b48d5070991dbd08"
    )


def test_a_label_with_the_separator_is_refused_like_vestigo():
    with pytest.raises(ValueError, match="separator"):
        derive_subkey(PARENT, "a:b")
    with pytest.raises(ValueError, match="separator"):
        vestigo_derive_subkey(PARENT, "a:b")
