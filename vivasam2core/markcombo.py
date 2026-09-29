"""Replace label-combination choices (① A, B  ② A, D …) in an answer sheet
with red marks on the passage lines themselves ("A: …", "갑: …").

For stems like "…학생만을 고른 것은?" without a <보기> box, the choices only
list combinations of the passage labels.  They are deleted, the stem becomes
"…만을 있는 대로 고른 것은?", the chosen lines are coloured red and the answer
table / "N. 정답" line show the labels (e.g. "A, D") instead of the number.
If the choices were already removed by hand, lines marked with an underline
are taken as the answer.

Works on the HWP file as saved by 한글 (so edits made there are kept).

usage: python -m vivasam2core.markcombo INPUT.hwp OUTPUT.hwp
"""
import re
import struct
import sys

from . import hwp5
from .docinfo import DocInfo
from .hwp5 import Record, TAG_PARA_HEADER, TAG_PARA_CHAR_SHAPE, TAG_PARA_LINE_SEG, TAG_LIST_HEADER, TAG_TABLE
from .prune import para_spans, text_of, set_cell_text, LAST, PS_STEM

CIRCLED = '①②③④⑤'
LABEL = r'(?:[A-E]|[갑을병정무])'
LINE_RE = re.compile(r'^\s*(' + LABEL + r')\s*:')
CHOICES_RE = re.compile(r'^\s*①\s*' + LABEL + r'(?:\s*,\s*' + LABEL + r')*(?:\s*[②-⑤]\s*' + LABEL
                        + r'(?:\s*,\s*' + LABEL + r')*)+\s*$')
ORDER = 'ABCDE갑을병정무'
RED = 0x000000FF


def insert_text(sec, s, pos, new):
    """Insert `new` at character position `pos` of the paragraph at s."""
    hdr, txt = sec[s], sec[s + 1]
    t = txt.data.decode('utf-16-le')
    delta = len(new)
    sec[s + 1] = Record(txt.tag, txt.level, (t[:pos] + new + t[pos:]).encode('utf-16-le'))
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
                if v[k] > pos:
                    v[k] += delta
            sec[j] = Record(r.tag, r.level, struct.pack('<%dI' % len(v), *v))
        elif r.tag == TAG_PARA_LINE_SEG:
            d = bytearray(r.data)
            for k in range(0, len(d), 36):
                p, = struct.unpack_from('<I', d, k)
                if p > pos:
                    struct.pack_into('<I', d, k, p + delta)
            sec[j] = Record(r.tag, r.level, d)


def char_shapes(sec, s):
    for j in range(s + 1, min(s + 5, len(sec))):
        if sec[j].level != sec[s].level + 1:
            break
        if sec[j].tag == TAG_PARA_CHAR_SHAPE:
            return j
    return None


def paint_red(sec, s, di):
    j = char_shapes(sec, s)
    v = list(struct.unpack('<%dI' % (len(sec[j].data) // 4), sec[j].data))
    for k in range(1, len(v), 2):
        v[k] = di.derive_char_shape(v[k], color=RED)
    sec[j] = Record(sec[j].tag, sec[j].level, struct.pack('<%dI' % len(v), *v))


def is_red(sec, s, di):
    j = char_shapes(sec, s)
    v = struct.unpack('<%dI' % (len(sec[j].data) // 4), sec[j].data)
    return all(struct.unpack_from('<I', di.char_shape(v[k]), 52)[0] == RED for k in range(1, len(v), 2))


def mark(src, dst):
    streams, clsid = hwp5.load_streams(src)
    di_recs = hwp5.parse_records(hwp5.inflate(streams['DocInfo']))
    di = DocInfo(di_recs)
    sec = hwp5.parse_records(hwp5.inflate(streams['BodyText/Section0']))
    spans = para_spans(sec)
    tail = next(s for s, e in spans[1:] if sec[s].data[11] & 0x04)
    body = [(s, e) for s, e in spans if s < tail]
    stems = [s for s, e in body if struct.unpack_from('<H', sec[s].data, 8)[0] == PS_STEM]
    heads = [s for s, e in body if text_of(sec, s).lstrip().startswith(('■', '▣'))]
    bounds = sorted(set(stems + heads + [tail]))

    expl = {}
    for s, e in spans:
        if s >= tail:
            m = re.match(r'^(\d+)\s*\.\s*정답\s*([①-⑤])', text_of(sec, s))
            if m:
                expl[int(m.group(1))] = (s, m.group(2))

    done = []
    deletes = []       # (start, end) of choice paragraphs
    stem_edits = []    # (s, pos)
    answers = {}       # number -> 'A, D'
    for no, s in enumerate(stems, 1):
        stem = text_of(sec, s)
        if '고른 것은' not in stem or '보기' in stem:
            continue
        e = next(b for b in bounds if b > s)
        lines, choice = [], None
        for j in range(s + 1, e):
            if sec[j].tag != TAG_PARA_HEADER:
                continue
            t = text_of(sec, j)
            if sec[j].level > 0 and LINE_RE.match(t):
                lines.append((j, LINE_RE.match(t).group(1)))
            elif sec[j].level == 0 and CHOICES_RE.match(t.rstrip('\r')):
                choice = (j, t)
        if not lines:
            continue
        if choice is not None:
            if no not in expl:
                continue
            k = CIRCLED.index(expl[no][1])
            parts = re.split(r'\s*[①-⑤]\s*', choice[1].strip())[1:]
            want = {x.strip() for x in parts[k].split(',')}
            ce = next(j for j in range(choice[0] + 1, len(sec) + 1)
                      if j == len(sec) or (sec[j].tag == TAG_PARA_HEADER and sec[j].level == 0))
            deletes.append((choice[0], ce))
        else:
            want = {lab for j, lab in lines if is_red(sec, j, di)}
            if not want or '만을' not in stem:
                continue
        labels = {lab for j, lab in lines}
        if not want <= labels:
            continue
        for j, lab in lines:
            if lab in want:
                paint_red(sec, j, di)
        if '있는 대로' not in stem:
            m = re.search(r'고른 것은', stem)
            stem_edits.append((s, m.start()))
        answers[no] = ', '.join(sorted(want, key=ORDER.index))
        done.append((no, answers[no]))

    # answer tables: row 0 numbers, row 1 answers
    cell_edits = []
    for s, e in spans:
        if s < tail:
            continue
        tbl = next((j for j in range(s, e) if sec[j].tag == TAG_TABLE), None)
        if tbl is None:
            continue
        cells = {}
        for j in range(tbl, e):
            if sec[j].tag == TAG_LIST_HEADER:
                col, row = struct.unpack_from('<HH', sec[j].data, 8)
                cells[(row, col)] = j + 1
        for (row, col), j in cells.items():
            if row == 0 and text_of(sec, j).strip().isdecimal():
                no = int(text_of(sec, j))
                if no in answers and (1, col) in cells:
                    cell_edits.append((cells[(1, col)], answers[no].replace(', ', ',')))

    ops = [(s, 'stem', p) for s, p in stem_edits]
    ops += [(s, 'cell', v) for s, v in cell_edits]
    ops += [(expl[no][0], 'expl', no) for no in answers if no in expl]
    ops += [(s, 'del', e) for s, e in deletes]
    for i, kind, v in sorted(ops, key=lambda x: -x[0]):
        if kind == 'stem':
            insert_text(sec, i, v, '있는 대로 ')
        elif kind == 'cell':
            set_cell_text(sec, i, v)
        elif kind == 'expl':
            t = text_of(sec, i)
            p = t.index(expl[v][1])
            # replace the circled number by the labels
            insert_text(sec, i, p + 1, answers[v])
            _delete_char(sec, i, p)
        else:
            del sec[i:v]

    streams['DocInfo'] = hwp5.deflate(hwp5.serialize_records(di.records))
    streams['BodyText/Section0'] = hwp5.deflate(hwp5.serialize_records(sec))
    streams.pop('PrvImage', None)
    order = ['FileHeader', 'DocInfo', 'BodyText/Section0'] + [k for k in streams if k not in ('FileHeader', 'DocInfo', 'BodyText/Section0')]
    hwp5.write_hwp(dst, streams, clsid, order=order)
    return done


def _delete_char(sec, s, pos):
    """Delete one character at pos (plain text char, not a control)."""
    hdr, txt = sec[s], sec[s + 1]
    t = txt.data.decode('utf-16-le')
    sec[s + 1] = Record(txt.tag, txt.level, (t[:pos] + t[pos + 1:]).encode('utf-16-le'))
    d = bytearray(hdr.data)
    n, = struct.unpack_from('<I', d, 0)
    struct.pack_into('<I', d, 0, (n & LAST) | ((n & ~LAST) - 1))
    sec[s] = Record(hdr.tag, hdr.level, d)
    for j in range(s + 2, min(s + 5, len(sec))):
        r = sec[j]
        if r.level != hdr.level + 1:
            break
        if r.tag == TAG_PARA_CHAR_SHAPE:
            v = list(struct.unpack('<%dI' % (len(r.data) // 4), r.data))
            for k in range(0, len(v), 2):
                if v[k] > pos:
                    v[k] -= 1
            sec[j] = Record(r.tag, r.level, struct.pack('<%dI' % len(v), *v))
        elif r.tag == TAG_PARA_LINE_SEG:
            d = bytearray(r.data)
            for k in range(0, len(d), 36):
                p, = struct.unpack_from('<I', d, k)
                if p > pos:
                    struct.pack_into('<I', d, k, p - 1)
            sec[j] = Record(r.tag, r.level, d)


if __name__ == '__main__':
    for no, ans in mark(sys.argv[1], sys.argv[2]):
        print('%d번: 선지 삭제/정답 표시 → %s' % (no, ans))
