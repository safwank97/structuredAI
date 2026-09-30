"""
Upload content validation -- an extension allowlist plus a lightweight
signature ("magic bytes") check on the actual file bytes, not just the
client-supplied filename extension or Content-Type header. Both of those are
attacker-controlled and trivially spoofable (rename anything.exe to
anything.pdf, or set whatever Content-Type header you like) -- the same
reason app/core/storage.py never trusts the client's filename as a path
component, this module never trusts it as a type declaration either.

Deliberately signature-only, not a full structural parse, for every format
here -- proportionate to what the app actually does with an upload today
(see app/api/v1/conversations.py's module docstring: there's no plan-review
or agent subsystem built yet, an upload is just stored and recorded in the
conversation timeline). A signature check is enough to catch "this isn't
really what it claims to be" without pretending to validate a file's full
internal structure.

DXF gets the same treatment even though it's a text format with no fixed
magic number: every ASCII DXF file's first two non-blank lines are always
the group code "0" followed by the value "SECTION" (the binary DXF variant
instead starts with the literal string "AutoCAD Binary DXF") -- checking for
that opening shape is a real content check, not a rename-the-extension
bypass. DXF is also a genuinely open, published format: the open-source
`ezdxf` library can fully parse and validate it, which is the natural next
step for whenever DXF content actually gets reviewed by something -- that's
future work, not implemented here, and deliberately not added as a new
dependency in this round.

DWG does not get even a "we can parse this later" story -- see
UNPARSEABLE_NOTE below.
"""
from __future__ import annotations


class UploadRejected(Exception):
    """Raised with a user-facing reason; the API layer turns this into a 415."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def _looks_like_pdf(data: bytes) -> bool:
    return data.startswith(b"%PDF-")


def _looks_like_png(data: bytes) -> bool:
    return data.startswith(b"\x89PNG\r\n\x1a\n")


def _looks_like_jpeg(data: bytes) -> bool:
    return data.startswith(b"\xff\xd8\xff")


def _looks_like_dxf(data: bytes) -> bool:
    if data.startswith(b"AutoCAD Binary DXF"):
        return True  # the (rare) binary DXF variant
    try:
        text = data[:400].decode("ascii", errors="ignore")
    except Exception:
        return False
    tokens = [line.strip() for line in text.splitlines() if line.strip()]
    return len(tokens) >= 2 and tokens[0] == "0" and tokens[1] == "SECTION"


def _looks_like_dwg(data: bytes) -> bool:
    # Every DWG version tag from AutoCAD R13 through the current format
    # starts with "AC10" (e.g. "AC1032" = AutoCAD 2018+) -- this version
    # prefix is part of DWG's published version-signature scheme even
    # though the rest of the binary format is not publicly documented.
    return data[:4] == b"AC10"


# extension -> signature checker. An extension missing from this dict is
# rejected outright, before its signature is even inspected.
_VALIDATORS = {
    ".pdf": _looks_like_pdf,
    ".png": _looks_like_png,
    ".jpg": _looks_like_jpeg,
    ".jpeg": _looks_like_jpeg,
    ".dxf": _looks_like_dxf,
    ".dwg": _looks_like_dwg,
}

ALLOWED_EXTENSIONS = tuple(_VALIDATORS.keys())

# Accepted and stored, but not reviewable by anything in this app yet. DWG's
# real binary structure is undocumented by Autodesk -- every tool that
# actually opens one either licenses the Open Design Alliance's Teigha/ODA
# SDK, licenses Autodesk's own RealDWG SDK, or calls Autodesk's paid Forge /
# Platform Services cloud conversion API. There is no free/open path here
# the way there is for DXF. Rather than silently pretend to support DWG
# review, that gap is surfaced to the user directly and left as an
# explicitly future, likely paid-tier capability -- a real product decision,
# not a corner cut quietly.
UNPARSEABLE_EXTENSIONS = frozenset({".dwg"})
UNPARSEABLE_NOTE = (
    "Note: DWG parsing isn't available in this build. Native AutoCAD DWG is "
    "a closed, undocumented binary format -- reading it for real requires a "
    "licensed conversion service (Autodesk Forge, or the Open Design "
    "Alliance SDK). The file is stored as uploaded; DWG review is planned "
    "as a future capability, likely gated behind a paid tier given the "
    "licensing cost involved."
)


def extension_of(filename: str) -> str:
    return "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def validate_upload(ext: str, data: bytes) -> None:
    """Raises UploadRejected if `ext` isn't an allowed extension, or if
    `data`'s actual content doesn't match what that extension claims to be."""
    checker = _VALIDATORS.get(ext)
    if checker is None:
        allowed = ", ".join(sorted(ALLOWED_EXTENSIONS))
        raise UploadRejected(
            f"Unsupported file type '{ext or '(no extension)'}'. Allowed types: {allowed}."
        )
    if not checker(data):
        raise UploadRejected(
            f"This file doesn't look like a valid {ext} file -- its content "
            "doesn't match the expected format. It may be corrupted, or "
            "renamed from a different file type."
        )
