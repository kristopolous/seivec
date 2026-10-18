"""Strip personal identifiers out of what callers say, before anything keeps it.

Callers volunteer things nobody needs to route a pothole: Social Security
numbers, card numbers, birthdays. `redact` swaps them for a placeholder
([SSN], [CARD], [DOB], [EMAIL]) at the point speech enters Sievec, so the
transcript, call log, audit trail, model prompts and handoff briefing only
ever see the placeholder. Values are never stored, not even hashed (a hash
of a 9-digit SSN is trivially reversible).
"""

from __future__ import annotations

import re

_DIGIT_WORD = r"(?:zero|oh|one|two|three|four|five|six|seven|eight|nine)"
# After a label even one digit counts, so a half-spoken number never shows.
# A run of digits or spoken digits, with the spaces/dashes/commas people put between them.
_NUM_RUN = rf"(?:(?:\d|{_DIGIT_WORD}\b)[\s.,-]*)+"
_MONTH = r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
_DATE = (rf"(?:{_MONTH}\.?\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,?\s+(?:of\s+)?\d{{2,4}})?"
         rf"|\d{{1,2}}(?:st|nd|rd|th)?\s+(?:of\s+)?{_MONTH}(?:,?\s+\d{{2,4}})?"
         rf"|\d{{1,2}}[/-]\d{{1,2}}[/-]\d{{2,4}})")
_LEAD = r"(?:\s+(?:number|no\.?|#))?(?:\s+(?:is|it's|was|are))?[\s:,]*"

_LABELED = [
    ("SSN", re.compile(rf"(\b(?:social security|social|ssn|s\.s\.n\.){_LEAD})({_NUM_RUN})", re.I)),
    ("CARD", re.compile(rf"(\b(?:card|credit card|debit card|visa|mastercard|amex){_LEAD})({_NUM_RUN})", re.I)),
    ("DOB", re.compile(rf"(\b(?:date of birth|d\.?o\.?b\.?|born on|born|birthday|birth date)(?:\s+(?:is|was))?[\s:,]*)({_DATE})", re.I)),
]
_SSN_SHAPE = re.compile(r"\b\d{3}[-\s.]\d{2}[-\s.]\d{4}\b")
_CARD_SHAPE = re.compile(r"\b(?:\d[\s-]?){13,19}\b")
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
PLACEHOLDER = re.compile(r"\[(SSN|CARD|DOB|EMAIL)\]")


def _luhn(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d = d * 2 - 9 if d > 4 else d * 2
        total, alt = total + d, not alt
    return total % 10 == 0


def redact(text: str) -> tuple[str, list[str]]:
    """Return the text with identifiers replaced, and which kinds were found."""
    found: list[str] = []

    def sub(kind: str):
        def repl(m: re.Match) -> str:
            found.append(kind)
            tail = m.group(2)
            trail = tail[len(tail.rstrip(" ,.-")):]
            return f"{m.group(1)}[{kind}]{trail}"
        return repl

    for kind, pat in _LABELED:
        text = pat.sub(sub(kind), text)

    def card(m: re.Match) -> str:
        digits = re.sub(r"\D", "", m.group(0))
        if 13 <= len(digits) <= 19 and _luhn(digits):
            found.append("CARD")
            return "[CARD]"
        return m.group(0)

    text = _CARD_SHAPE.sub(card, text)

    def ssn(_: re.Match) -> str:
        found.append("SSN")
        return "[SSN]"

    text = _SSN_SHAPE.sub(ssn, text)

    def email(_: re.Match) -> str:
        found.append("EMAIL")
        return "[EMAIL]"

    text = _EMAIL.sub(email, text)
    return text, sorted(set(found))


def scrub(text: str) -> str:
    return redact(text)[0]


def scrub_all(obj):
    """Scrub every string in a JSON-like payload (audit records, notes)."""
    if isinstance(obj, str):
        return scrub(obj)
    if isinstance(obj, dict):
        return {k: scrub_all(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [scrub_all(v) for v in obj]
    return obj


def kinds_in(text: str) -> list[str]:
    """Placeholders already in (redacted) text."""
    return sorted(set(PLACEHOLDER.findall(text or "")))


LABELS = {"SSN": "Social Security number", "CARD": "card number", "DOB": "date of birth", "EMAIL": "email address"}
