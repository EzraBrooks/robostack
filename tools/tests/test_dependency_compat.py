import pytest

import abi_check
from check_dependency_compat import constraint_compatible


@pytest.mark.parametrize("constraint, pin, compatible", [
    (">=1.88.0,<1.89.0a0", "1.88", True),
    (">=1.88.0,<1.89.0a0", "1.90", False),
    (">=3.12,<3.13.0a0", "3.12.*", True),
    (">=11.2,<12", "11.*", True),
    (">=2.0.2,<3", "2", True),
    (">1.88,<1.89", "1.88", True),
    ("<1.88.0a0", "1.88", False),
    (">=1.86|>=2", "2", True),
    ("==1.88.1", "1.88", True),
    ("==1.88.1", "1.90", False),
    ("~=1.88.1", "1.88", True),
    ("", "1.88", True),
    ("==1.88.1", "gcc", True),
])
def test_constraint_compatible(constraint, pin, compatible):
    assert constraint_compatible(constraint, pin) is compatible


def test_newest_orders_conda_versions():
    versions = {v: {"version": v} for v in ("1.10.0", "1.9.2", "1.10.0rc1", "1.2")}
    assert abi_check.newest(versions)["version"] == "1.10.0"
