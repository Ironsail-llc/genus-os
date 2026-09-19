"""Total admitted child attempts stay bounded across batches and descendants."""

from concurrent.futures import ThreadPoolExecutor

import pytest


def test_shared_ancestor_limit_is_atomic_and_rejection_does_not_charge_other_limits():
    from robothor.engine.spawn_limits import extend_limits, try_claim

    root = extend_limits((), 3)
    child = extend_limits(root, 1)
    assert try_claim(child)
    assert not try_claim(child)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: try_claim(root), range(20)))
    assert sum(results) == 2
    assert not try_claim(root)


@pytest.mark.parametrize("limit", [-1, 101, True, "3", None])
def test_invalid_limit_never_means_unrestricted(limit):
    from robothor.engine.spawn_limits import extend_limits

    with pytest.raises(ValueError):
        extend_limits((), limit)


def test_zero_does_not_erase_an_ancestor_limit():
    from robothor.engine.spawn_limits import extend_limits, try_claim

    assert try_claim(extend_limits((), 0))
    parent = extend_limits((), 1)
    child = extend_limits(parent, 0)
    assert try_claim(child)
    assert not try_claim(parent)
