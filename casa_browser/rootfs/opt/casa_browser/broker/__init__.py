"""Private credential broker interface types."""
from dataclasses import dataclass

ERROR_CODES = frozenset({'unsupported', 'refused', 'not_found', 'conflict', 'unavailable', 'timeout', 'internal',
                         'page_changed'})   # slice 5u: driver-to-broker only
MAX_LINE = 64 * 1024

class BrokerError(Exception):
    """A fixed boundary code, without SDK or request text."""

    def __init__(self, code):
        if not isinstance(code, str) or code not in ERROR_CODES:
            code = 'internal'
        self.args = (code,)
        self.code = code

@dataclass(frozen=True)
class LoginMetadata:
    site_id: str
    roles_available: tuple[str, ...]
    extra_field_names: tuple[str, ...]

@dataclass(frozen=True)
class LoginOutcome:
    status: str
    reason: str

@dataclass(frozen=True)
class GuestStatus:
    state: str
    reason: str
