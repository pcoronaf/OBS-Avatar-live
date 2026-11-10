"""Consent records (spec §8). A profile cannot be created without one."""

from __future__ import annotations

import datetime as _dt
import getpass
from dataclasses import asdict, dataclass

CONSENT_STATEMENT_ES = (
    "Confirmo que tengo derecho a usar esta fotografía para generar un avatar sintético en vivo; "
    "si la persona no soy yo, cuento con su consentimiento explícito. No usaré el avatar para "
    "suplantar identidades, eludir verificaciones biométricas, de prueba de vida o KYC, ni para engañar."
)
CONSENT_VERSION = 1


class ConsentError(ValueError):
    pass


@dataclass
class ConsentRecord:
    rights_confirmed: bool
    subject_is_self: bool
    third_party_consent_reference: str | None
    statement: str
    statement_version: int
    confirmed_by: str
    confirmed_at: str

    def to_dict(self) -> dict:
        return asdict(self)


def make_consent(payload: dict) -> ConsentRecord:
    if not isinstance(payload, dict) or payload.get("rights_confirmed") is not True:
        raise ConsentError("explicit confirmation of the right to use the photo is required")
    is_self = bool(payload.get("subject_is_self", False))
    ref = payload.get("third_party_consent_reference")
    if not is_self and not (isinstance(ref, str) and ref.strip()):
        raise ConsentError("a third-party consent reference is required when the photo is not of the user")
    try:
        user = getpass.getuser()
    except Exception:
        user = "unknown"
    return ConsentRecord(True, is_self, ref.strip() if isinstance(ref, str) and ref.strip() else None,
                         CONSENT_STATEMENT_ES, CONSENT_VERSION, user,
                         _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"))
