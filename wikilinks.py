import html
import re
from urllib.parse import quote

from filenames import safe_filename


# [[target]]
# [[target|alias]]
WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")


def extract_wikilink_targets(markdown_text: str) -> set[str]:
    """
    Parse wikilinks from markdown and return a set of canonical targets.

    Supported:
      [[Note]]
      [[Note|Alias]]
      [[Note#Heading]]
      [[Note^block]]

    Returned targets are normalized via safe_filename().
    """
    targets: set[str] = set()

    if not markdown_text:
        return targets

    for match in WIKILINK_RE.finditer(markdown_text):
        inner = (match.group(1) or "").strip()
        if not inner:
            continue

        base = _extract_base_target(inner)
        canonical = safe_filename(base)

        if canonical:
            targets.add(canonical)

    return targets


def rewrite_wikilinks_targets(
    markdown_text: str,
    *,
    old_stem: str,
    new_stem: str,
) -> tuple[str, bool]:
    """
    Rewrite wikilinks in markdown from old_stem → new_stem.

    Handles:
      [[Old]]
      [[Old|Alias]]
      [[Old#Heading]]
      [[Old^block]]

    Comparison is done on canonical (safe_filename) names.
    """
    if not markdown_text:
        return markdown_text, False

    old_canon = safe_filename(old_stem)
    new_canon = safe_filename(new_stem)

    if not old_canon or not new_canon or old_canon == new_canon:
        return markdown_text, False

    changed = False

    def replacer(match: re.Match) -> str:
        nonlocal changed

        inner = (match.group(1) or "").strip()
        if not inner:
            return match.group(0)

        target, alias = _split_alias(inner)
        base, suffix = _split_suffix(target)

        if safe_filename(base) == old_canon:
            changed = True
            target = f"{new_canon}{suffix}"

        if alias is not None:
            return f"[[{target}|{alias}]]"
        return f"[[{target}]]"

    rewritten = WIKILINK_RE.sub(replacer, markdown_text)
    return rewritten, changed


def wikilinks_to_html(markdown_text: str) -> str:
    """
    Convert wikilinks into HTML <a> tags.

    [[Note]]        → <a href="note://Note">Note</a>
    [[Note|Alias]]  → <a href="note://Note">Alias</a>

    - label is HTML-escaped
    - href uses canonical safe_filename + URL encoding
    """
    if not markdown_text:
        return markdown_text

    def replacer(match: re.Match) -> str:
        inner = (match.group(1) or "").strip()
        if not inner:
            return ""

        target, alias = _split_alias(inner)
        label = alias if alias is not None else target

        # Handle Obsidian-like suffixes:
        #   [[Note#Heading]]  -> note://Note#Heading  (fragment)
        #   [[Note^block]]    -> note://Note#^block   (fragment)
        base, suffix = _split_suffix(target)

        canonical_base = safe_filename(base)
        href = "note://" + quote(canonical_base, safe="")

        # Preserve heading/block as URL fragment so the interceptor does NOT treat it
        # as part of the note title (prevents creating "Note#Heading" / "Note^block" notes).
        if suffix:
            if suffix.startswith("#"):
                frag = suffix[1:]
                href += "#" + quote(frag, safe="")
            elif suffix.startswith("^"):
                # Put block id into fragment too; keep leading '^' for future handling.
                frag = suffix
                href += "#" + quote(frag, safe="")

        return f'<a href="{href}">{html.escape(label, quote=False)}</a>'

    return WIKILINK_RE.sub(replacer, markdown_text)


# ───────────────────────── helpers ─────────────────────────


def _split_alias(raw: str) -> tuple[str, str | None]:
    """
    Split 'target|alias' → (target, alias)
    """
    if "|" in raw:
        target, alias = raw.split("|", 1)
        return target.strip(), alias.strip()
    return raw.strip(), None


def _split_suffix(target: str) -> tuple[str, str]:
    """
    Split Obsidian-style suffixes:
      Note#Heading
      Note^block
    """
    for sep in ("#", "^"):
        if sep in target:
            base, rest = target.split(sep, 1)
            return base.strip(), sep + rest
    return target.strip(), ""


def _extract_base_target(raw: str) -> str:
    """
    Extract base note name from full wikilink inner content.
    """
    target, _ = _split_alias(raw)
    base, _ = _split_suffix(target)
    return base