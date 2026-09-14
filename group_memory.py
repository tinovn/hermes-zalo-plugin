"""Per-group memory ("sổ tay nhóm") — pure logic, no I/O.

Owner-only append-only fact/rule store per Zalo chat, separate from the
per-chat persona (`zalo_set_chat_persona`), which is a `mission`/`personality`
blob the owner overwrites wholesale. The notebook exists because that
overwrite pattern makes the model drop existing facts on almost every edit —
appending one line should never require reading back and resending the rest.

``adapter.py`` owns the I/O (chat_settings.json read/write); everything here
is deterministic and unit-testable without a Hermes/Zalo install.
"""

from typing import Any, List, Optional, Tuple

MAX_ENTRIES = 30
MAX_CHARS = 2000


def coerce_lines(value: Any) -> List[str]:
    """Normalise a stored ``group_memory`` value into a list of lines.

    Tolerates the legacy shape (a single newline-joined string) alongside the
    current list-of-strings shape.
    """
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    if isinstance(value, str) and value.strip():
        return [ln.strip() for ln in value.split("\n") if ln.strip()]
    return []


def cap(lines: List[str]) -> Tuple[List[str], List[str]]:
    """Enforce MAX_ENTRIES / MAX_CHARS, keeping the newest content.

    Entry cap drops the oldest lines. Char cap then drops trailing lines
    (from the end of what's left) until the joined length fits.
    """
    warnings: List[str] = []
    out = list(lines)

    if len(out) > MAX_ENTRIES:
        dropped = len(out) - MAX_ENTRIES
        out = out[-MAX_ENTRIES:]
        warnings.append(
            f"Đã bỏ {dropped} dòng cũ nhất do sổ tay vượt giới hạn {MAX_ENTRIES} dòng."
        )

    dropped_for_chars = 0
    while out and len("\n".join(out)) > MAX_CHARS:
        out = out[:-1]
        dropped_for_chars += 1
    if dropped_for_chars:
        warnings.append(
            f"Đã bỏ {dropped_for_chars} dòng cuối do sổ tay vượt giới hạn {MAX_CHARS} ký tự."
        )

    return out, warnings


def dedup_append(current: List[str], add_text: str) -> List[str]:
    """Append lines from ``add_text`` (newline-separated) to ``current``,
    skipping any that already exist (case-insensitive, exact-line match)."""
    out = list(current)
    seen = {ln.lower() for ln in out}
    for raw in add_text.split("\n"):
        line = raw.strip()
        if not line or line.lower() in seen:
            continue
        out.append(line)
        seen.add(line.lower())
    return out


def remove_line(lines: List[str], remove: str) -> Tuple[List[str], Optional[str]]:
    """Remove one line by 1-based index (e.g. "2") or by substring match
    (case-insensitive, first hit). Returns (new_lines, removed_line); the
    removed line is ``None`` and ``new_lines`` is unchanged if nothing matched.
    """
    remove = remove.strip()
    if remove.isdigit():
        idx = int(remove) - 1
        if 0 <= idx < len(lines):
            return lines[:idx] + lines[idx + 1:], lines[idx]
        return list(lines), None

    needle = remove.lower()
    for i, ln in enumerate(lines):
        if needle in ln.lower():
            return lines[:i] + lines[i + 1:], ln
    return list(lines), None


def render_block(lines: List[str]) -> str:
    """Render the prompt-injection block, or "" when the notebook is empty."""
    if not lines:
        return ""
    numbered = "\n".join(f"{i}. {ln}" for i, ln in enumerate(lines, start=1))
    return (
        "═══ GROUP NOTEBOOK — RULES & FIXED FACTS (set by the owner) ═══\n"
        f"{numbered}\n"
        "→ These are facts/rules the OWNER recorded for this group. Treat as "
        "TRUTH and follow ABSOLUTELY for EVERYONE in the group (including "
        "strangers). If there is an addressing rule, apply it. If this "
        "conflicts with your own inference, the notebook wins. For anything "
        "not covered here, say you don't know — do NOT invent. Only the "
        "owner can edit this notebook.\n"
        "═════════════════════════════════════════════"
    )
