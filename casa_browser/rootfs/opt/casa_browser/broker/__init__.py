"""Private credential broker interface types."""

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
