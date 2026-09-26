"""Approximate text measurement used to fill PARA_LINE_SEG / cell heights.

Hancom re-flows paragraphs when it opens a document, so these values only
need to be plausible; they matter for viewers that trust the cached layout.
"""
import unicodedata


def char_em(ch):
    o = ord(ch)
    if ch in (' ', '\u00a0'):
        return 0.36
    if 0xAC00 <= o <= 0xD7A3 or 0x3130 <= o <= 0x318F or 0x1100 <= o <= 0x11FF:
        return 1.0
    if 0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF or 0xF900 <= o <= 0xFAFF:
        return 1.0
    if 0x2460 <= o <= 0x24FF or 0x3200 <= o <= 0x32FF or 0x2776 <= o <= 0x2793:
        return 1.0
    if 0x25A0 <= o <= 0x25FF or 0x2600 <= o <= 0x26FF or 0x2190 <= o <= 0x21FF:
        return 1.0
    if 0xFF01 <= o <= 0xFF60 or 0x3000 <= o <= 0x303F:
        return 1.0
    if ch in '…「」『』※○●◎□■△▲▽▼◇◆':
        return 1.0
    if ch in '‘’‚':
        return 0.30
    if ch in '“”':
        return 0.45
    if ch == '•':
        return 0.55
    if ch == '·':
        return 0.40
    if ch in '.,:;!|\'`':
        return 0.28
    if ch in '()[]{}':
        return 0.36
    if ch.isdigit():
        return 0.56
    if 'A' <= ch <= 'Z':
        return 0.66
    if 'a' <= ch <= 'z':
        return 0.52
    if ch in '-–':
        return 0.40
    if ch in '—~':
        return 0.70
    if ch in '%+=<>':
        return 0.60
    eaw = unicodedata.east_asian_width(ch)
    return 1.0 if eaw in ('W', 'F') else 0.55


def text_width(text, size=900, ratio=100, spacing=0):
    """Width in HWPUNIT of text rendered with the given char shape values."""
    total = 0.0
    for ch in text:
        total += char_em(ch) * size * ratio / 100.0 + size * spacing / 100.0
    return total


NO_LINE_START = set('.,:;!?)]}%’”」』·')


def _is_word_char(ch):
    return ch.isascii() and (ch.isalnum() or ch in "'-_")


def break_lines(pieces, first_width, other_width):
    """pieces: list of (text, size, ratio, spacing) -> list of (start, end) char
    offsets per line (offsets count characters of the concatenated text).

    Hangul may break between any two syllables (the template's paragraph
    shapes use character-based Korean line breaking); Latin words and
    numbers stay together; trailing spaces hang past the right edge."""
    chars = []
    for text, size, ratio, spacing in pieces:
        for ch in text:
            chars.append((ch, char_em(ch) * size * ratio / 100.0 + size * spacing / 100.0))
    n = len(chars)
    lines = []
    start = 0
    while start < n:
        width = first_width if not lines else other_width
        acc = 0.0
        i = start
        while i < n:
            ch, w = chars[i]
            if ch == '\n':
                break
            if ch != ' ' and acc + w > width and i > start:
                break
            acc += w
            i += 1
        if i < n and chars[i][0] == '\n':
            lines.append((start, i + 1))
            start = i + 1
            continue
        if i >= n:
            lines.append((start, n))
            break
        end = i
        # keep latin words / numbers together
        if _is_word_char(chars[end][0]):
            j = end
            while j > start and _is_word_char(chars[j - 1][0]):
                j -= 1
            if j > start:
                end = j
        # avoid starting a line with closing punctuation
        while end > start + 1 and chars[end][0] in NO_LINE_START:
            end -= 1
        lines.append((start, end))
        start = end
    if not lines:
        lines.append((0, 0))
    return lines
