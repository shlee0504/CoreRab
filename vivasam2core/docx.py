"""Parse a Word (.docx) problem set into the same problem model as vivasam.py.

Expected layout (as in "서양 철학 사상 실전 문제"): each problem sits in a
one-cell outer table holding the stem "N. ...", nested tables for passages,
(가)/(나) tables, pictures and the "< 보기 >" box, then "① ..." choices.
An answer table ("정답표": 번호/정답/채점 columns) follows the problems.
"""
import io
import re
import zipfile

import lxml.etree as ET

from . import vivasam as V

W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
A = '{http://schemas.openxmlformats.org/drawingml/2006/main}'
R = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'
WP = '{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}'

STEM_RE = re.compile(r'^\s*(\d{1,3})\s*\.\s*')
PREFIX_HINT = re.compile(r'^\s*(?:[갑을병정무]\s*:|•|[ㄱ-ㅎ]\.)')


class DocxDoc:
    def __init__(self, path):
        self.zip = zipfile.ZipFile(path)
        self.root = ET.fromstring(self.zip.read('word/document.xml'))
        rels = ET.fromstring(self.zip.read('word/_rels/document.xml.rels'))
        self.rels = {r.get('Id'): r.get('Target') for r in rels}
        self.title = ''

    # -- runs / paragraphs
    def runs(self, p):
        out = []
        for r in p.iter(W + 'r'):
            fmt = set()
            rp = r.find(W + 'rPr')
            if rp is not None:
                u = rp.find(W + 'u')
                if u is not None and u.get(W + 'val', 'single') != 'none':
                    fmt.add('u')
                b = rp.find(W + 'b')
                if b is not None and b.get(W + 'val', 'true') not in ('0', 'false'):
                    pass   # bold is not carried over (template has no bold emphasis)
                va = rp.find(W + 'vertAlign')
                if va is not None:
                    v = va.get(W + 'val')
                    if v == 'subscript':
                        fmt.add('sub')
                    elif v == 'superscript':
                        fmt.add('sup')
            text = ''
            for x in r:
                if x.tag == W + 't':
                    text += x.text or ''
                elif x.tag == W + 'tab':
                    text += ' '
            if text:
                out.append(V.Run(text, frozenset(fmt)))
        return V._merge_runs(out)

    def para(self, p):
        pp = p.find(W + 'pPr')
        align = 'justify'
        hanging = False
        if pp is not None:
            jc = pp.find(W + 'jc')
            if jc is not None and jc.get(W + 'val') == 'center':
                align = 'center'
            ind = pp.find(W + 'ind')
            if ind is not None and ind.get(W + 'hanging'):
                hanging = True
        para = V.Para(self.runs(p), align, hanging)
        if not hanging and PREFIX_HINT.match(para.text):
            para.hanging = True
        return para

    def pictures(self, p):
        pics = []
        for d in p.iter(WP + 'inline', WP + 'anchor'):
            blip = next(d.iter(A + 'blip'), None)
            ext = d.find(WP + 'extent')
            if blip is None or ext is None:
                continue
            target = self.rels[blip.get(R + 'embed')]
            data = self.zip.read('word/' + target.lstrip('/'))
            w = int(int(ext.get('cx')) / 127)
            h = int(int(ext.get('cy')) / 127)
            try:
                from PIL import Image
                im = Image.open(io.BytesIO(data))
                ow, oh = im.size[0] * 75, im.size[1] * 75
            except Exception:
                ow, oh = w, h
            pics.append(V.Picture(data, V._sniff_ext(data, 'png'), w, h, ow, oh, (0, 0, ow, oh)))
        return pics

    def blocks(self, container):
        blocks = []
        for c in container:
            if c.tag == W + 'p':
                pics = self.pictures(c)
                para = self.para(c)
                if para.text.strip():
                    blocks.append(para)
                blocks.extend(pics)
                if not pics and not para.text.strip():
                    blocks.append(para)
            elif c.tag == W + 'tbl':
                blocks.append(self.table(c))
        while blocks and isinstance(blocks[0], V.Para) and not blocks[0].text.strip():
            blocks.pop(0)
        while blocks and isinstance(blocks[-1], V.Para) and not blocks[-1].text.strip():
            blocks.pop()
        return blocks

    def table(self, tbl):
        cells = []
        rows = tbl.findall(W + 'tr')
        occupied = {}
        for ri, tr in enumerate(rows):
            ci = 0
            for tc in tr.findall(W + 'tc'):
                while (ri, ci) in occupied:
                    ci += 1
                pr = tc.find(W + 'tcPr')
                span = 1
                width = 2000
                vmerge = None
                if pr is not None:
                    gs = pr.find(W + 'gridSpan')
                    if gs is not None:
                        span = int(gs.get(W + 'val'))
                    tw = pr.find(W + 'tcW')
                    if tw is not None and tw.get(W + 'type') == 'dxa':
                        width = int(int(tw.get(W + 'w')) * 5)   # twips -> HWPUNIT
                    vm = pr.find(W + 'vMerge')
                    if vm is not None:
                        vmerge = vm.get(W + 'val', 'continue')
                if vmerge == 'continue':
                    for c in cells:
                        if c.col == ci and c.row + c.rowspan == ri:
                            c.rowspan += 1
                    ci += span
                    continue
                cells.append(V.Cell(ri, ci, 1, span, width, 1000, self.blocks(tc)))
                ci += span
        ncols = max((c.col + c.colspan for c in cells), default=1)
        width = sum(c.width for c in cells if c.row == 0)
        return V.Table(len(rows), ncols, cells, width)


def _bogi_items(blocks):
    """<보기> items; items laid out in a table (ㄱ | ㄴ / ㄷ | ㄹ) are read
    row by row."""
    items = []
    for x in blocks:
        if isinstance(x, V.Para):
            if x.text.strip():
                items.append(x)
        elif isinstance(x, V.Table):
            for c in sorted(x.cells, key=lambda c: (c.row, c.col)):
                items.extend(_bogi_items(c.blocks))
    return items


def _elements(doc, blocks):
    els = []
    for b in blocks:
        if isinstance(b, V.Table):
            kind, info = V.classify_table(b)
            if kind == 'passage' and b.rows == 1 and b.cols == 1:
                inner = b.cells[0].blocks
                first = next((x for x in inner if isinstance(x, V.Para)), None)
                m = V.LABEL_RE.match(first.text) if first is not None else None
                if m:
                    label = re.sub(r'\s+', '', m.group(1))
                    body = inner[inner.index(first) + 1:]
                    if label == '보기':
                        els.append(V.Element('bogi', label='보기', items=_bogi_items(body)))
                    else:
                        els.append(V.Element('keybox', label=m.group(1).strip(), blocks=body))
                    continue
            if kind == 'bogi':
                els.append(V.Element('bogi', label=info[0], items=_bogi_items(info[1])))
            elif kind == 'keybox':
                els.append(V.Element('keybox', label=info[0], blocks=info[1]))
            elif kind == 'inline_choices':
                els.append(V.Element('inline_choices', items=info))
            elif kind == 'match_table':
                els.append(V.Element('match_table', table=info))
            else:
                els.append(V.Element('passage', table=b))
        elif isinstance(b, V.Picture):
            els.append(V.Element('passage', blocks=[b]))
        else:
            t = b.text.strip()
            if not t:
                continue
            elif t[0] in V.CIRCLED[:5]:
                parts = [x.strip() for x in re.split(r'[①②③④⑤]', t) if x.strip()]
                if len(parts) > 1:
                    items = [V.Para([V.Run(x)]) for x in parts]
                    if els and els[-1].kind == 'inline_choices':
                        els[-1].items.extend(items)
                    else:
                        els.append(V.Element('inline_choices', items=items))
                    continue
                para = V.Para(list(b.runs), b.align)
                V._strip_prefix(para, 2 if len(t) > 1 and t[1] in ' \u00a0' else 1)
                para = V.Para([V.Run(re.sub(r' {2,}', '   ', r.text), r.fmt) for r in para.runs], para.align)
                if els and els[-1].kind == 'choices':
                    els[-1].items.append(para)
                else:
                    els.append(V.Element('choices', items=[para]))
            elif t[0] in V.PAREN_NUM:
                els.append(V.Element('subq', blocks=[b]))
            else:
                els.append(V.Element('text', blocks=[b]))
    merged = []
    for e in els:
        if merged and e.kind == 'space' and merged[-1].kind == 'space':
            merged[-1].count += 1
        else:
            merged.append(e)
    while merged and merged[-1].kind == 'space':
        merged.pop()
    while merged and merged[0].kind == 'space':
        merged.pop(0)
    return merged


def _answer_table(doc, tbl):
    """번호/정답/채점 triples -> {number: answer}."""
    answers = {}
    for tr in tbl.findall(W + 'tr'):
        texts = [''.join(t.text or '' for t in tc.iter(W + 't')).strip() for tc in tr.findall(W + 'tc')]
        for k in range(0, len(texts) - 1, 3):
            if texts[k].isdecimal() and texts[k + 1]:
                answers[int(texts[k])] = texts[k + 1]
    return answers


def parse(path):
    doc = DocxDoc(path)
    body = doc.root.find(W + 'body')
    problems = {}
    answers = {}
    shared = None
    doc.tail = []           # 정답표 / 해설 blocks after the problems
    in_tail = False
    for child in body:
        if child.tag in (W + 'p', W + 'tbl'):
            text = ''.join(t.text or '' for t in child.iter(W + 't')).strip()
            if not in_tail and child.tag == W + 'p' and text in ('정답표', '정답', '정답 및 해설', '해설'):
                in_tail = True
            if in_tail:
                if child.tag == W + 'tbl':
                    answers.update(_answer_table(doc, child))
                    doc.tail.append(doc.table(child))
                else:
                    doc.tail.append(doc.para(child))
                continue
        if child.tag == W + 'p':
            text = ''.join(t.text or '' for t in child.iter(W + 't')).strip()
            if not doc.title and text:
                doc.title = re.sub(r'\s*실전\s*문제\s*', ' ', text).strip().lstrip('■▣ ')
            continue
        if child.tag != W + 'tbl':
            continue
        rows = child.findall(W + 'tr')
        first_text = ''.join(t.text or '' for t in child.iter(W + 't'))
        if '정답' in first_text[:40] and '번호' in first_text[:40]:
            answers.update(_answer_table(doc, child))
            continue
        if len(rows) != 1 or len(rows[0].findall(W + 'tc')) != 1:
            continue
        blocks = doc.blocks(rows[0].find(W + 'tc'))
        # optional shared intro "[20~21] ..." before the stem
        idx = next((i for i, b in enumerate(blocks)
                    if isinstance(b, V.Para) and STEM_RE.match(b.text)), None)
        if idx is None:
            continue
        pre = blocks[:idx]
        stem = blocks[idx]
        num = int(STEM_RE.match(stem.text).group(1))
        para = V.Para(list(stem.runs), stem.align)
        V._strip_prefix(para, STEM_RE.match(stem.text).end())
        prob = V.Problem(para, _elements(doc, blocks[idx + 1:]))
        if pre and isinstance(pre[0], V.Para) and pre[0].text.strip().startswith('['):
            shared = (pre[0], _elements(doc, pre[1:]))
        if shared is not None:
            prob.shared_intro, prob.shared = shared
            shared = None
        problems[num] = prob
    if not problems:
        return parse_flat(doc)
    out = []
    for num in sorted(problems):
        p = problems[num]
        p.answer = answers.get(num, '')
        out.append(p)
    return doc, out


TAIL_START = re.compile(r'^\s*(정답\s*및\s*해설|정답표|해설)\s*$')
SECTION_RE = re.compile(r'^\s*[■▣]')
ANSWER_LINE = re.compile(r'^\s*(\d{1,3})\s*\.\s*정답\s*([①-⑤](?:\s*,\s*[①-⑤])*|[ㄱ-ㅎ](?:\s*,\s*[ㄱ-ㅎ])*)')


def parse_flat(doc):
    """Problems written as plain paragraphs: 'N. stem', passage tables, choices."""
    body = doc.root.find(W + 'body')
    doc.tail = []
    doc.title = ''
    problems = []
    cur = None
    section = None
    in_tail = False
    started = False
    for child in body:
        if child.tag not in (W + 'p', W + 'tbl'):
            continue
        if child.tag == W + 'p':
            para = doc.para(child)
            text = para.text.strip()
            if not doc.title and text:
                doc.title = re.sub(r'\s*실전\s*문제\s*', ' ', text).strip().lstrip('■▣ ')
            if not in_tail and started and TAIL_START.match(text):
                in_tail = True
            if in_tail:
                doc.tail.append(para)
                continue
            if SECTION_RE.match(text):
                started = True
                section = para
                continue
            m = STEM_RE.match(text)
            if m and started:
                stem = V.Para(list(para.runs), para.align)
                V._strip_prefix(stem, m.end())
                cur = [int(m.group(1)), stem, [], section]
                section = None
                problems.append(cur)
                continue
            if cur is not None:
                if text:
                    cur[2].append(para)
                cur[2].extend(doc.pictures(child))
        else:
            if in_tail:
                doc.tail.append(doc.table(child))
            elif cur is not None:
                cur[2].append(doc.table(child))
    answers = {}
    for b in doc.tail:
        if isinstance(b, V.Para):
            m = ANSWER_LINE.match(b.text)
            if m:
                answers[int(m.group(1))] = m.group(2)
    out = []
    for num, stem, blocks, sec in problems:
        prob = V.Problem(stem, _elements(doc, blocks))
        prob.answer = answers.get(num, '')
        prob.section = sec
        out.append(prob)
    if not out and any(ESSAY_RE.match(b.text) for b in doc.tail + _all_paras(doc)):
        return parse_essay(doc)
    return doc, out


ESSAY_RE = re.compile(r'^\s*서술형\s*(\d+)\s+')
SUB_RE = re.compile(r'^\s*\((\d)\)\s*')
SET_RE = re.compile(r'모의고사\s*(\d+)\s*회')


def _all_paras(doc):
    body = doc.root.find(W + 'body')
    return [doc.para(c) for c in body if c.tag == W + 'p']


def parse_essay(doc):
    """서술형 sets: '서술형 N  stem', passage paragraphs, '(1) …' questions and
    a '모범 답안' block with '(1)\t…' answers.  Several sets ('모의고사 N회')
    may follow one another; each becomes a section."""
    paras = [p for p in _all_paras(doc) if p.text.strip()]
    doc.tail = []
    doc.title = ''
    heads = []
    out = []
    cur = None
    section = None
    mode = None                       # passage | sub | answer
    for para in paras:
        text = para.text.strip()
        if re.match(r'^\d{4}\s*학년도', text):      # running header of each set
            continue
        m = SET_RE.search(text)
        if m and not ESSAY_RE.match(text):
            name = re.sub(r'\s+', ' ', text)
            heads.append(name)
            section = V.Para([V.Run('▣ ' + name)])
            continue
        m = ESSAY_RE.match(text)
        if m:
            stem = V.Para(list(para.runs), para.align)
            V._strip_prefix(stem, m.end())
            cur = dict(stem=stem, passage=[], subs=[], answers=[], section=section)
            section = None
            out.append(cur)
            mode = 'passage'
            continue
        if cur is None:
            continue
        if text.replace(' ', '') in ('모범답안', '예시답안', '답안'):
            mode = 'answer'
            continue
        m = SUB_RE.match(text)
        if mode == 'answer':
            if m:
                cur['answers'].append([text[m.end():].strip()])
            elif cur['answers']:
                cur['answers'][-1].append(text)
            continue
        if m:
            mode = 'sub'
            sub = V.Para(list(para.runs), para.align)
            V._strip_prefix(sub, m.end())
            sub = V.Para([V.Run(V.PAREN_NUM[int(m.group(1)) - 1] + ' ')] + sub.runs, sub.align)
            cur['subs'].append(sub)
        elif mode == 'passage':
            cur['passage'].append(V.Para([V.Run(r.text.replace('\t', ' '), r.fmt) for r in para.runs], para.align))
    # title: the set name without its number, e.g. '국제경제 모의고사 서술형'
    if heads:
        base = re.sub(r'\s*\d+\s*회', '', heads[0])
        nums = [int(SET_RE.search(h).group(1)) for h in heads]
        doc.title = '%s(%d~%d회)' % (base, min(nums), max(nums)) if len(nums) > 1 else heads[0]
    problems = []
    for c in out:
        els = []
        if c['passage']:
            els.append(V.Element('passage', blocks=c['passage']))
        for sub in c['subs']:
            els.append(V.Element('subq', blocks=[sub]))
            els.append(V.Element('space', count=3))
        prob = V.Problem(c['stem'], els)
        prob.answer = ' '.join('%s%s%s' % (V.PAREN_NUM[i], V.EXAMPLE_MARK, '\n'.join(a))
                               for i, a in enumerate(c['answers']))
        prob.section = c['section']
        problems.append(prob)
    return doc, problems
