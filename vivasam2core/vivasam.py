"""Parse a Vivasam (비바샘) question-bank export into a neutral problem model.

The exports are HWPML 2.x documents (XML) saved with a .hwp extension. The
server sometimes appends an ASP error page after </HWPML>; that tail is
ignored. Problems are marked with the private attribute
_assetName="PROBLEM_ITEM" (except the very first one, which shares the
paragraph holding the section definition), shared passages with
"ITEM_PARAGRAPH" and answers with "ITEM_ANSWER".
"""
import base64
import re
import zlib
from dataclasses import dataclass, field
from typing import List, Optional

import lxml.etree as ET

CIRCLED = '①②③④⑤⑥⑦⑧⑨⑩'
PAREN_NUM = '⑴⑵⑶⑷⑸⑹'
EXAMPLE_MARK = '\U000F0124'   # Vivasam PUA glyph used before model answers


# ---------------------------------------------------------------- model

@dataclass
class Run:
    text: str
    fmt: frozenset = frozenset()   # {'u', 'b', 'sub', 'sup', 'box', 'small'}


@dataclass
class Para:
    runs: List[Run]
    align: str = 'justify'
    hanging: bool = False          # source paragraph used a hanging indent

    @property
    def text(self):
        return ''.join(r.text for r in self.runs)


@dataclass
class Picture:
    data: bytes
    ext: str
    width: int          # displayed size (HWPUNIT)
    height: int
    ori_width: int      # native image size (HWPUNIT)
    ori_height: int
    clip: tuple         # (left, top, right, bottom) in native units


@dataclass
class Cell:
    row: int
    col: int
    rowspan: int
    colspan: int
    width: int
    height: int
    blocks: list
    shaded: bool = False


@dataclass
class Table:
    rows: int
    cols: int
    cells: List[Cell]
    width: int

    def cell(self, r, c):
        for x in self.cells:
            if x.row == r and x.col == c:
                return x
        return None


@dataclass
class Element:
    kind: str           # passage | bogi | keybox | choices | inline_choices | match_table | subq | space | text
    blocks: list = field(default_factory=list)   # passage content / paragraphs
    label: str = ''
    items: list = field(default_factory=list)    # choices / bogi items (Para)
    table: Optional[Table] = None
    count: int = 0


@dataclass
class Problem:
    stem: Para
    elements: List[Element]
    shared: Optional[list] = None   # elements of an ITEM_PARAGRAPH preceding it
    shared_intro: Optional[Para] = None
    answer: str = ''
    explanation: str = ''


# ---------------------------------------------------------------- parsing

def load_xml(path):
    data = open(path, 'rb').read()
    end = data.rfind(b'</HWPML>')
    if end < 0:
        raise ValueError('not an HWPML document: %s' % path)
    return ET.fromstring(data[:end + len(b'</HWPML>')])


def _sniff_ext(raw, declared):
    if raw[:8] == b'\x89PNG\r\n\x1a\n':
        return 'png'
    if raw[:3] == b'\xff\xd8\xff':
        return 'jpg'
    if raw[:2] == b'BM':
        return 'bmp'
    if raw[:6] in (b'GIF87a', b'GIF89a'):
        return 'gif'
    return (declared or 'png').lower()


class VivasamDoc:
    def __init__(self, path):
        self.root = load_xml(path)
        mt = self.root.find('HEAD/MAPPINGTABLE')
        self.charshapes = mt.find('CHARSHAPELIST')
        self.parashapes = mt.find('PARASHAPELIST')
        self.borderfills = mt.find('BORDERFILLLIST')
        self.numberings = mt.find('NUMBERINGLIST')
        self.bindata = {}
        declared = {b.get('BinData'): b.get('Format') for b in mt.find('BINDATALIST')}
        for b in self.root.find('TAIL/BINDATASTORAGE'):
            raw = base64.b64decode(b.text or '')
            if b.get('Compress') == 'true':
                raw = zlib.decompress(raw, -15)
            self.bindata[b.get('Id')] = (raw, _sniff_ext(raw, declared.get(b.get('Id'))))
        self._fmt_cache = {}
        self.title = ''

    # -- styles
    def _borderfill(self, bf_id):
        bf = self.borderfills[int(bf_id) - 1]
        assert bf.get('Id') == str(bf_id)
        return bf

    def char_fmt(self, cs_id):
        if cs_id in self._fmt_cache:
            return self._fmt_cache[cs_id]
        cs = self.charshapes[int(cs_id)]
        f = set()
        for ch in cs:
            if ch.tag == 'UNDERLINE' and ch.get('Type', 'None') != 'None':
                f.add('u')
            elif ch.tag == 'BOLD':
                f.add('b')
            elif ch.tag == 'SUBSCRIPT':
                f.add('sub')
            elif ch.tag == 'SUPERSCRIPT':
                f.add('sup')
        bf = cs.get('BorderFillId')
        if bf and bf != '0':
            node = self._borderfill(bf)
            if any(node.find(s) is not None and node.find(s).get('Type', 'None') != 'None'
                   for s in ('LEFTBORDER', 'RIGHTBORDER', 'TOPBORDER', 'BOTTOMBORDER')):
                f.add('box')
        if int(cs.get('Height', '1000')) < 950:
            f.add('small')
        res = frozenset(f)
        self._fmt_cache[cs_id] = res
        return res

    def is_numbered_choice(self, p):
        ps = self.parashapes[int(p.get('ParaShape'))]
        if ps.get('HeadingType') != 'Number':
            return False
        num = self.numberings[int(ps.get('Heading')) - 1]
        head = num.find('PARAHEAD')
        return head is not None and head.get('NumFormat') == 'CircledDigit'

    def para_align(self, p):
        ps = self.parashapes[int(p.get('ParaShape'))]
        return {'Center': 'center', 'Right': 'right', 'Left': 'left'}.get(ps.get('Align'), 'justify')

    def cell_shaded(self, cell):
        bf = self._borderfill(cell.get('BorderFill'))
        wb = bf.find('FILLBRUSH/WINDOWBRUSH')
        if wb is None:
            return False
        face = int(wb.get('FaceColor', '4294967295'))
        return face not in (4294967295, 0xFFFFFF)

    # -- content
    def runs_and_controls(self, p):
        """Yield Run or control elements in document order."""
        out = []
        for t in p.findall('TEXT'):
            fmt = self.char_fmt(t.get('CharShape'))
            for c in t:
                if c.tag == 'CHAR':
                    if c.text:
                        out.append(Run(c.text, fmt))
                else:
                    out.append(c)
        return out

    def para_from(self, p, runs=None):
        if runs is None:
            runs = [x for x in self.runs_and_controls(p) if isinstance(x, Run)]
        ps = self.parashapes[int(p.get('ParaShape'))]
        pm = ps.find('PARAMARGIN')
        hanging = pm is not None and int(pm.get('Indent', '0')) < 0
        return Para(_merge_runs(runs), self.para_align(p), hanging)

    def picture(self, pic):
        img = pic.find('IMAGE')
        raw, ext = self.bindata[img.get('BinItem')]
        size = pic.find('SHAPEOBJECT/SIZE')
        dim = pic.find('IMAGEDIM')
        clip = pic.find('IMAGECLIP')
        sc = pic.find('SHAPECOMPONENT')
        w, h = int(size.get('Width')), int(size.get('Height'))
        if dim is not None:
            ow, oh = int(dim.get('Width')), int(dim.get('Height'))
        else:
            ow, oh = int(sc.get('OriWidth')), int(sc.get('OriHeight'))
        cl = (int(clip.get('Left')), int(clip.get('Top')), int(clip.get('Right')), int(clip.get('Bottom'))) \
            if clip is not None else (0, 0, ow, oh)
        return Picture(raw, ext, w, h, ow, oh, cl)

    def blocks_from_paralist(self, plist):
        """Paragraph list (cell / text box) -> list of Para | Picture | Table."""
        blocks = []
        for p in plist.findall('P'):
            pending = []
            for x in self.runs_and_controls(p):
                if isinstance(x, Run):
                    pending.append(x)
                    continue
                if x.tag == 'PICTURE':
                    if ''.join(r.text for r in pending).strip():
                        blocks.append(self.para_from(p, pending))
                    pending = []
                    blocks.append(self.picture(x))
                elif x.tag == 'TABLE':
                    if ''.join(r.text for r in pending).strip():
                        blocks.append(self.para_from(p, pending))
                    pending = []
                    blocks.append(self.table(x))
                # COLDEF and other layout controls carry no content
            if pending or not blocks or not isinstance(blocks[-1], (Picture, Table)):
                para = self.para_from(p, pending)
                blocks.append(para)
        # trim empty paragraphs at both ends
        while blocks and isinstance(blocks[0], Para) and not blocks[0].text.strip():
            blocks.pop(0)
        while blocks and isinstance(blocks[-1], Para) and not blocks[-1].text.strip():
            blocks.pop()
        return blocks

    def table(self, tbl):
        cells = []
        for row in tbl.findall('ROW'):
            for c in row.findall('CELL'):
                cells.append(Cell(int(c.get('RowAddr')), int(c.get('ColAddr')),
                                  int(c.get('RowSpan')), int(c.get('ColSpan')),
                                  int(c.get('Width')), int(c.get('Height')),
                                  self.blocks_from_paralist(c.find('PARALIST')),
                                  self.cell_shaded(c)))
        size = tbl.find('SHAPEOBJECT/SIZE')
        return Table(int(tbl.get('RowCount')), int(tbl.get('ColCount')), cells, int(size.get('Width')))


def _merge_runs(runs):
    out = []
    for r in runs:
        if not r.text:
            continue
        if out and out[-1].fmt == r.fmt:
            out[-1] = Run(out[-1].text + r.text, r.fmt)
        else:
            out.append(Run(r.text, r.fmt))
    return out


def para_text(blocks):
    return '\n'.join(b.text for b in blocks if isinstance(b, Para))


def cell_text(cell):
    return ' '.join(b.text for b in cell.blocks if isinstance(b, Para)).strip()


LABEL_RE = re.compile(r'^\s*<\s*([^<>]{1,12}?)\s*>\s*$')


def classify_table(t: Table):
    """Return (kind, info) for a top-level table inside a problem."""
    # labelled box: 2x1 with "<label>" in first row
    if t.cols == 1 and t.rows == 2:
        m = LABEL_RE.match(cell_text(t.cell(0, 0)))
        if m:
            label = re.sub(r'\s+', '', m.group(1))
            body = t.cell(1, 0).blocks
            return ('bogi' if label == '보기' else 'keybox'), (m.group(1).strip(), body)
    texts = {(c.row, c.col): cell_text(c) for c in t.cells}
    # matching table: first column holds ①..⑤ below a header row
    col0 = [texts.get((r, 0), None) for r in range(t.rows)]
    if t.rows >= 6 and col0[1:6] == list(CIRCLED[:5]) and not (col0[0] or '').strip():
        return 'match_table', t
    # inline choice grid: alternating (marker, content) cells
    markers = [(k, v) for k, v in texts.items() if len(v) == 1 and v in CIRCLED[:5]]
    if len(markers) == 5 and t.cols % 2 == 0 and all(k[1] % 2 == 0 for k, _ in markers):
        items = []
        for (r, c), v in sorted(markers, key=lambda kv: CIRCLED.index(kv[1])):
            content = t.cell(r, c + 1)
            runs = []
            for b in content.blocks:
                if isinstance(b, Para):
                    runs.extend(b.runs)
            items.append(Para(_merge_runs(runs)))
        return 'inline_choices', items
    return 'passage', t


def split_label_rows(t: Table):
    """(가)/(나) style 2-column table -> [(label, blocks)] or None."""
    if t.cols != 2:
        return None
    rows = []
    for r in range(t.rows):
        a, b = t.cell(r, 0), t.cell(r, 1)
        if a is None or b is None or a.colspan != 1 or b.colspan != 1:
            return None
        label = cell_text(a)
        if not re.fullmatch(r'\(?[가-하A-Z]\)?|[㈎-㈛]|[갑을병정무]', label):
            return None
        rows.append((label, b.blocks))
    return rows


class Parser:
    def __init__(self, path):
        self.doc = VivasamDoc(path)

    def parse(self):
        d = self.doc
        paras = d.root.find('BODY/SECTION').findall('P')
        groups = []          # (kind, [P...])
        answers = []
        in_answers = False
        for i, p in enumerate(paras):
            items = d.runs_and_controls(p)
            text = ''.join(x.text for x in items if isinstance(x, Run))
            asset = p.get('_assetName')
            if i == 0:
                title = self._title_from_container(items)
                if title:
                    d.title = title
            if not in_answers and text.lstrip().startswith('(답)'):
                in_answers = True
            if in_answers:
                if text.lstrip().startswith('(답)'):
                    answers.append([p])
                elif answers:
                    answers[-1].append(p)
                continue
            if i == 0 or asset == 'PROBLEM_ITEM':
                groups.append(['problem', [p]])
            elif asset == 'ITEM_PARAGRAPH':
                groups.append(['shared', [p]])
            elif groups:
                groups[-1][1].append(p)
        problems = []
        shared = None
        for kind, ps in groups:
            if kind == 'shared':
                shared = (d.para_from(ps[0]), self._elements(ps[1:]))
                continue
            prob = Problem(d.para_from(ps[0]), self._elements(ps[1:]))
            if shared is not None:
                prob.shared_intro, prob.shared = shared
                shared = None
            problems.append(prob)
        for prob, ans in zip(problems, answers):
            prob.answer, prob.explanation = self._answer_text(ans)
        if len(answers) != len(problems):
            raise ValueError('problem/answer count mismatch: %d vs %d' % (len(problems), len(answers)))
        return problems

    def _title_from_container(self, items):
        for x in items:
            if not isinstance(x, Run) and x.tag == 'CONTAINER':
                texts = [''.join(c.text or '' for c in dt.iter('CHAR')).strip()
                         for dt in x.iter('DRAWTEXT')]
                texts = [t for t in texts if t and not re.fullmatch(r'[반번\s]*|이름:?', t)]
                for t in texts:
                    m = re.search(r'([^\s:]+(?:\s[^\s:()]+)*\([^)]*\))\s*$', t)
                    if m:
                        return m.group(1)
                if texts:
                    return texts[-1]
        return ''

    def _elements(self, ps):
        d = self.doc
        els = []
        for p in ps:
            items = d.runs_and_controls(p)
            controls = [x for x in items if not isinstance(x, Run)]
            runs = [x for x in items if isinstance(x, Run)]
            text = ''.join(r.text for r in runs)
            tables = [c for c in controls if c.tag == 'TABLE']
            pics = [c for c in controls if c.tag == 'PICTURE']
            if tables or pics:
                for c in controls:
                    if c.tag == 'TABLE':
                        t = d.table(c)
                        kind, info = classify_table(t)
                        if kind == 'bogi':
                            els.append(Element('bogi', label=info[0], items=[b for b in info[1] if isinstance(b, Para)]))
                        elif kind == 'keybox':
                            els.append(Element('keybox', label=info[0], blocks=info[1]))
                        elif kind == 'inline_choices':
                            els.append(Element('inline_choices', items=info))
                        elif kind == 'match_table':
                            els.append(Element('match_table', table=info))
                        else:
                            els.append(Element('passage', table=t))
                    elif c.tag == 'PICTURE':
                        els.append(Element('passage', blocks=[d.picture(c)]))
                if text.strip():
                    els.append(Element('text', blocks=[d.para_from(p, runs)]))
                continue
            stripped = text.strip()
            if d.is_numbered_choice(p):
                els.append(Element('choice', items=[d.para_from(p, runs)]))
            elif stripped and stripped[0] in CIRCLED[:5] and (len(stripped) == 1 or stripped[1] in '  '):
                para = d.para_from(p, runs)
                _strip_prefix(para, 2 if len(stripped) > 1 else 1)
                els.append(Element('choice', items=[para]))
            elif stripped and stripped[0] in PAREN_NUM:
                els.append(Element('subq', blocks=[d.para_from(p, runs)]))
            elif not stripped:
                els.append(Element('space', count=1))
            else:
                els.append(Element('text', blocks=[d.para_from(p, runs)]))
        # merge consecutive choices / spaces
        merged = []
        for e in els:
            if merged and e.kind == 'choice' and merged[-1].kind == 'choices':
                merged[-1].items.extend(e.items)
            elif e.kind == 'choice':
                merged.append(Element('choices', items=list(e.items)))
            elif merged and e.kind == 'space' and merged[-1].kind == 'space':
                merged[-1].count += 1
            else:
                merged.append(e)
        while merged and merged[-1].kind == 'space':
            merged.pop()
        return merged

    def _answer_text(self, ps):
        d = self.doc
        answer, expl = '', []
        for p in ps:
            text = ''.join(x.text for x in d.runs_and_controls(p) if isinstance(x, Run)).strip()
            if text.startswith('(답)'):
                answer = text[len('(답)'):].strip()
            elif text.startswith('(해설)'):
                expl.append(text[len('(해설)'):].strip())
            elif text and expl:
                expl.append(text)
        return answer, '\n'.join(expl)


def _strip_prefix(para, n):
    """Remove the first n characters (e.g. '① ') from a paragraph."""
    while n and para.runs:
        r = para.runs[0]
        if len(r.text) <= n:
            n -= len(r.text)
            para.runs.pop(0)
        else:
            para.runs[0] = Run(r.text[n:], r.fmt)
            n = 0
    if para.runs:
        para.runs[0] = Run(para.runs[0].text.lstrip('  '), para.runs[0].fmt)


def parse(path):
    p = Parser(path)
    problems = p.parse()
    return p.doc, problems
