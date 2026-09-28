"""Delete problems marked "(삭제)" from a converted answer sheet, together with
their answers and explanations, and renumber what remains.

Works on the HWP file as saved by 한글 (so edits made there are kept).
Question numbers are automatic (outline numbering) and renumber by
themselves; the answer tables, "N. 정답" explanation lines and the
"■ 단원 (a~b번)" ranges are text and are rewritten here.

usage: python -m vivasam2core.prune INPUT.hwp OUTPUT.hwp
"""
import re
import struct
import sys

from . import hwp5
from .hwp5 import Record, TAG_PARA_HEADER, TAG_PARA_TEXT, TAG_PARA_CHAR_SHAPE, TAG_PARA_LINE_SEG, TAG_LIST_HEADER, TAG_TABLE

PS_STEM = 27
MARK = '삭제'
LAST = 0x80000000


def para_spans(sec, level=0):
    """[(start, end)] of paragraphs at `level` (end exclusive)."""
    starts = [i for i, r in enumerate(sec) if r.tag == TAG_PARA_HEADER and r.level == level]
    spans = []
    for k, s in enumerate(starts):
        e = starts[k + 1] if k + 1 < len(starts) else len(sec)
        # stop at a record shallower than this paragraph (end of list)
        j = s + 1
        while j < e and sec[j].level > level:
            j += 1
        spans.append((s, j))
    return spans


def text_of(sec, s):
    r = sec[s + 1] if s + 1 < len(sec) else None
    if r is not None and r.tag == TAG_PARA_TEXT and r.level == sec[s].level + 1:
        return r.data.decode('utf-16-le', 'replace')
    return ''


def replace_prefix(sec, s, old, new):
    """Replace leading text `old` with `new` in a text-only paragraph at s."""
    hdr, txt = sec[s], sec[s + 1]
    t = txt.data.decode('utf-16-le')
    assert t.startswith(old), (t[:20], old)
    delta = len(new) - len(old)
    t = new + t[len(old):]
    sec[s + 1] = Record(txt.tag, txt.level, t.encode('utf-16-le'))
    d = bytearray(hdr.data)
    n, = struct.unpack_from('<I', d, 0)
    struct.pack_into('<I', d, 0, (n & LAST) | ((n & ~LAST) + delta))
    sec[s] = Record(hdr.tag, hdr.level, d)
    for j in range(s + 2, min(s + 5, len(sec))):
        r = sec[j]
        if r.level != hdr.level + 1:
            break
        if r.tag == TAG_PARA_CHAR_SHAPE:
            v = list(struct.unpack('<%dI' % (len(r.data) // 4), r.data))
            for k in range(0, len(v), 2):
                if v[k] >= len(old) and v[k] > 0:
                    v[k] += delta
            sec[j] = Record(r.tag, r.level, struct.pack('<%dI' % len(v), *v))
        elif r.tag == TAG_PARA_LINE_SEG:
            d = bytearray(r.data)
            for k in range(0, len(d), 36):
                p, = struct.unpack_from('<I', d, k)
                if p > 0:
                    struct.pack_into('<I', d, k, p + delta)
            sec[j] = Record(r.tag, r.level, d)


def set_cell_text(sec, s, new):
    """Set the whole text of a (short, single-line) cell paragraph."""
    old = text_of(sec, s).rstrip('\r')
    if old == new:
        return
    if not old and new:
        # insert a PARA_TEXT record
        hdr = sec[s]
        sec.insert(s + 1, Record(TAG_PARA_TEXT, hdr.level + 1, (new + '\r').encode('utf-16-le')))
        d = bytearray(hdr.data)
        n, = struct.unpack_from('<I', d, 0)
        struct.pack_into('<I', d, 0, (n & LAST) | (len(new) + 1))
        sec[s] = Record(hdr.tag, hdr.level, d)
        return 1
    if old and not new:
        hdr = sec[s]
        del sec[s + 1]
        d = bytearray(hdr.data)
        n, = struct.unpack_from('<I', d, 0)
        struct.pack_into('<I', d, 0, (n & LAST) | 1)
        sec[s] = Record(hdr.tag, hdr.level, d)
        return -1
    replace_prefix(sec, s, old, new)
    return 0


def prune(src, dst):
    streams, clsid = hwp5.load_streams(src)
    sec = hwp5.parse_records(hwp5.inflate(streams['BodyText/Section0']))
    spans = para_spans(sec)
    # locate the answer section (first paragraph with a page break after the preamble)
    tail = next(s for s, e in spans[1:] if sec[s].data[11] & 0x04)
    # problems in the body
    stems = []
    sections = []
    for s, e in spans:
        if s >= tail:
            break
        ps = struct.unpack_from('<H', sec[s].data, 8)[0]
        t = text_of(sec, s)
        if ps == PS_STEM:
            stems.append(s)
        elif t.lstrip().startswith('■'):
            sections.append((s, len(stems)))
    n = len(stems)
    delete = {i + 1 for i, s in enumerate(stems) if MARK in text_of(sec, s)}
    keep = [k for k in range(1, n + 1) if k not in delete]
    new_no = {old: i + 1 for i, old in enumerate(keep)}

    ranges = []
    # body: stem .. next stem / section header / tail
    boundaries = sorted(set(stems + [s for s, _ in sections] + [tail]))
    for k in delete:
        s = stems[k - 1]
        e = next(b for b in boundaries if b > s)
        ranges.append((s, e))

    # tail: explanations "N. 정답 ..."
    expl = {}
    tail_sections = []
    for s, e in spans:
        if s < tail:
            continue
        t = text_of(sec, s)
        m = re.match(r'^(\d+)\s*\.\s*정답', t)
        if m:
            expl[int(m.group(1))] = (s, e)
        elif t.lstrip().startswith('■'):
            tail_sections.append(s)
    for k in delete:
        if k in expl:
            ranges.append(expl[k])

    # section ranges (a~b번)
    def section_range(first_old, last_old):
        nums = [new_no[k] for k in range(first_old, last_old + 1) if k in new_no]
        if not nums:
            return None
        return '%d~%d번' % (nums[0], nums[-1]) if len(nums) > 1 else '%d번' % nums[0]

    edits = []   # (index, kind, payload) applied bottom-up
    for s in [x for x, _ in sections] + tail_sections:
        t = text_of(sec, s)
        m = re.search(r'\((\d+)~(\d+)번\)', t)
        if m:
            rng = section_range(int(m.group(1)), int(m.group(2)))
            if rng:
                edits.append((s, 'section', (t, m, rng)))
    for old, (s, e) in expl.items():
        if old in new_no and new_no[old] != old:
            edits.append((s, 'prefix', ('%d.' % old, '%d.' % new_no[old])))

    # answer tables (문항/정답 rows): rewrite cells in reading order
    answers = {}
    for old, (s, e) in expl.items():
        m = re.match(r'^\d+\s*\.\s*정답\s*(\S+(?:\s*,\s*\S+)*)', text_of(sec, s))
        answers[old] = m.group(1) if m else ''
    tables = []
    for s, e in spans:
        if s < tail:
            continue
        tbl = next((j for j in range(s, e) if sec[j].tag == TAG_TABLE), None)
        if tbl is not None:
            tables.append((s, e, tbl))
    new_answers = [(new_no[k], answers.get(k, '')) for k in keep]
    slot = 0
    table_edits = []
    for s, e, tbl in tables:
        rows, cols = struct.unpack_from('<HH', sec[tbl].data, 4)
        cells = {}
        for j in range(tbl, e):
            if sec[j].tag == TAG_LIST_HEADER:
                col, row = struct.unpack_from('<HH', sec[j].data, 8)
                cells[(row, col)] = j + 1        # first paragraph of the cell
        for c in range(1, cols):
            num, ans = new_answers[slot] if slot < len(new_answers) else ('', '')
            slot += 1
            table_edits.append((cells[(0, c)], str(num)))
            table_edits.append((cells[(1, c)], ans))

    # apply: text edits first (indices stay valid while lengths of record
    # lists do not change), then cell edits bottom-up, then deletions bottom-up
    for s, kind, payload in edits:
        if kind == 'prefix':
            replace_prefix(sec, s, *payload)
    for s, kind, payload in edits:
        if kind == 'section':
            t, m, rng = payload
            old_body = t.rstrip('\r')
            new_body = old_body[:m.start()] + '(' + rng + ')' + old_body[m.end():]
            # rewrite as prefix replacement of the whole text
            replace_prefix(sec, s, old_body, new_body)
    ops = sorted([(i, 'cell', v) for i, v in table_edits] + [(s, 'del', e) for s, e in ranges],
                 key=lambda x: -x[0])
    for i, kind, v in ops:
        if kind == 'cell':
            set_cell_text(sec, i, v)
        else:
            del sec[i:v]
    # last paragraph flag
    last = [i for i, r in enumerate(sec) if r.tag == TAG_PARA_HEADER and r.level == 0][-1]
    d = bytearray(sec[last].data)
    nch, = struct.unpack_from('<I', d, 0)
    struct.pack_into('<I', d, 0, nch | LAST)
    sec[last] = Record(sec[last].tag, 0, d)

    streams['BodyText/Section0'] = hwp5.deflate(hwp5.serialize_records(sec))
    streams.pop('PrvImage', None)
    order = ['FileHeader', 'DocInfo', 'BodyText/Section0'] + [k for k in streams if k not in ('FileHeader', 'DocInfo', 'BodyText/Section0')]
    hwp5.write_hwp(dst, streams, clsid, order=order)
    return sorted(delete), len(keep)


if __name__ == '__main__':
    deleted, kept = prune(sys.argv[1], sys.argv[2])
    print('삭제한 문항:', deleted, '/ 남은 문항:', kept)
