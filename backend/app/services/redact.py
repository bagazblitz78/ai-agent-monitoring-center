"""Redaction helpers for data that must never leave the process in the clear.

M8 ships the full ``redaction.py`` (payloads, logs, SSE). M3 only needs the two
primitive helpers the monitors call: masking e-mail addresses in git author
fields and masking secret-looking values in process command lines.
"""

from __future__ import annotations

__all__ = ["redact_cmdline", "redact_email"]

#: Flags whose value is always a secret, whatever it looks like.
SENSITIVE_FLAGS = frozenset(
    {
        "-p",
        "--p",
        "--pass",
        "--password",
        "--passwd",
        "--token",
        "--key",
        "--secret",
        "--api-key",
        "--apikey",
        "--auth",
        "--authorization",
        "--credential",
        "--credentials",
        "--private-key",
        "--access-key",
        "--session-key",
        "--client-secret",
    }
)

#: Opaque strings at least this long are treated as tokens even without a flag.
OPAQUE_MIN_LENGTH = 20

#: Anything containing a path or URL separator is structure, not a secret.
_STRUCTURE_SEPARATORS = ("/", "\\")


def _mask(value: str) -> str:
    return f"{value[0]}***" if value else "***"


def redact_email(value: str) -> str:
    """Return ``b***@example.com`` for ``bob@example.com``.

    The local part is never preserved; the domain is kept so that debugging
    still shows *which* account was involved.
    """
    text = (value or "").strip()
    if "@" not in text:
        return _mask(text)
    local, _, domain = text.rpartition("@")
    if not local:
        return f"***@{domain}"
    return f"{local[0]}***@{domain}"


def _looks_opaque(arg: str) -> bool:
    if len(arg) < OPAQUE_MIN_LENGTH:
        return False
    if arg.startswith("-"):
        return False
    if any(ch.isspace() for ch in arg):
        return False
    return not any(sep in arg for sep in _STRUCTURE_SEPARATORS)


def redact_cmdline(args: list[str]) -> str:
    """Join ``args`` into a display string with secret-looking values masked.

    Masks the value of secret-named flags (``--token x`` and ``--token=x``) and
    any standalone opaque string of 20+ characters. Paths, URLs and ordinary
    short arguments are left intact — the monitors persist this string, so it
    must stay readable enough to identify the process.
    """
    out: list[str] = []
    mask_next = False
    for arg in args:
        if mask_next:
            out.append("***")
            mask_next = False
            continue
        flag, sep, _value = arg.partition("=")
        if flag.lower() in SENSITIVE_FLAGS:
            if sep:
                out.append(f"{flag}=***")
            else:
                out.append(flag)
                mask_next = True
            continue
        out.append("***" if _looks_opaque(arg) else arg)
    return " ".join(out)
