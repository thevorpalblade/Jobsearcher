"""Contact extraction from free ad text.

Structured contacts come from the source adapters. This module adds cheap regex-based
extraction of emails/phone numbers mentioned in the ad text; LLM-based extraction of
named contacts ("Frågor om tjänsten besvaras av ...") comes with the ranking milestone.
"""

from __future__ import annotations

import re

from jobsearcher.models import Contact

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# Swedish numbers: 07x-xxx xx xx, 08-xxx xx xx, +46 ...
PHONE_RE = re.compile(r"(?<![\w.])(?:\+46\s?(?:\(0\)\s?)?|0)\d[\d\s-]{5,12}\d(?!\d)")

# Generic mailboxes are useful for applying but aren't a person to contact.
_GENERIC_LOCALPARTS = {
    "noreply",
    "no-reply",
    "donotreply",
    "info",
    "jobb",
    "jobs",
    "rekrytering",
    "recruitment",
    "career",
    "careers",
    "karriar",
    "hr",
    "support",
}


def extract_contacts_from_text(text: str, provenance: str = "ad_text") -> list[Contact]:
    contacts: list[Contact] = []
    for email in dict.fromkeys(m.rstrip(".") for m in EMAIL_RE.findall(text or "")):
        local = email.split("@", 1)[0].lower()
        role = "generic mailbox" if local in _GENERIC_LOCALPARTS else None
        contacts.append(Contact(email=email, role=role, provenance=provenance))
    for phone in dict.fromkeys(p.strip() for p in PHONE_RE.findall(text or "")):
        if 8 <= len(re.sub(r"\D", "", phone)) <= 12:
            contacts.append(Contact(phone=phone, provenance=provenance))
    return contacts
