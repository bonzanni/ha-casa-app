"""Guest permission evaluation: what the vault's accessor list says about the guest account.

The SDK lists the browsing vault's users but cannot say which one is the extension's guest.
A vault with no user that is unable to edit items means the guest can edit them
(``not_readonly``: the Setup page warns); a vault with at least one read-only user is taken as
the guest being one (``readonly``). No accessor list (not configured, or the credential
service did not answer) is ``unknown``. Nothing is gated on it (action model, design §5.2)."""
from dataclasses import dataclass

@dataclass(frozen=True)
class GuestRole:
    guest_role: str
    readonly_users: int
    editable_users: int

def assess_guest(accessors):
    readonly = editable = 0
    # History access (1024) is also held by users who cannot edit items.
    write_bits = 128 | 64 | 256 | 512 | 2097152 | 2
    if accessors is not None:
        for accessor in accessors:
            if accessor.accessor_type == 'user':
                if accessor.permissions & write_bits:
                    editable += 1
                else:
                    readonly += 1
    role = 'unknown'
    if accessors is not None:
        role = 'readonly' if readonly else 'not_readonly'
    return GuestRole(role, readonly, editable)
