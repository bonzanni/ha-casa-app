import os
import time
from pathlib import Path

import pytest

from hindsight_provenance_contract import run_contract

pytestmark = [pytest.mark.docker, pytest.mark.slow]


def test_supported_hindsight_preserves_provenance_contract(tmp_path: Path) -> None:
    # Both variables are read here, before the `try`: the `finally` below sends
    # a DELETE to base_url unconditionally, so a guard that only checked the URL
    # (or one placed inside the `try`) would still let a request leave on a
    # missing version. No CI tier supplies either variable; the scheduled slow
    # tier selects this file with `-m slow`, so absent configuration must be a
    # skip, not an error (#629).
    missing = [name for name in ("HINDSIGHT_URL", "HINDSIGHT_EXPECTED_VERSION")
               if not os.environ.get(name)]
    if missing:
        pytest.skip(f"no {' / '.join(missing)}; live Hindsight contract test skipped")
    base_url = os.environ["HINDSIGHT_URL"]
    bank = f"casa-provenance-contract-{os.getpid()}-{int(time.time())}"
    try:
        report = run_contract(
            base_url,
            bank=bank,
            expected_version=os.environ["HINDSIGHT_EXPECTED_VERSION"],
            record_path=tmp_path / "complete-envelopes.json",
        )
        assert report["items"]
    finally:
        from hindsight_provenance_contract import _request
        _request(base_url, "DELETE", f"/v1/default/banks/{bank}", timeout=30)
