"""Convert a Vivasam question-bank export into the CORE workbook layout.

The layout (page setup, header, master page, fonts, box styles, numbering)
comes from a template HWP made by the teacher, e.g. "2-1 교정적 정의(심화)(답지)":

* question stems use the template's auto-numbered paragraph style
* passages go into the template's 1x1 box; (가)/(나) passages into its
  label table; <보기>, <핵심어>, <조건> boxes reuse its "< 보기 >" box
* <보기> questions are "select all that apply" (no combination choices)
* in answer mode (답지) the correct choice / <보기> items are red and model
  answers of written questions are printed in red; explanations are omitted
"""
import re
import struct

from . import hwp5
from .hwp5 import (Record, TAG_PARA_HEADER, TAG_PARA_TEXT, TAG_CTRL_HEADER,
                   TAG_SHAPE_COMPONENT_PICTURE)
from .docinfo import DocInfo, CS_UNDERLINE, CS_SUBSCRIPT, CS_SUPERSCRIPT, CS_BOLD
from .hwpbuild import Ctx, Para, Table, Cell, Picture, TemplateTable, CTRL_TBL
from . import layout
from . import vivasam as V

# template ids (2-1 교정적 정의(심화)(답지).hwp)
CS_TEXT = 7          # 9pt 학교안심 바른돋움 B
CS_TABLE_TEXT = 9
CS_CHOICE = 10       # 95% width, -3% spacing
CS_RED_CHOICE = 8
CS_RED_TEXT = 32
PS_STEM = 27         # outline-numbered question stem
PS_BODY = 0
PS_BODY2 = 1
PS_CHOICE = 4
PS_BOGI = 3
PS_CENTER = 12
PS_TIGHT = 49        # 60% line spacing, used under tables
ST_BASIC = 11        # 기본 스타일
ST_NORMAL = 0        # 바탕글
ST_BOGI = 1          # 보기 ㄱ, ㄴ, ㄷ
ST_CHOICE = 2        # 5행답항
BF_BOX = 3
BF_LABEL = 16

BOX_WIDTH = 27256
LABEL_COL = 2853
COLUMN_WIDTH = 27780

RED_OF = {CS_TEXT: CS_RED_TEXT, CS_TABLE_TEXT: CS_RED_TEXT, CS_CHOICE: CS_RED_CHOICE}


class TemplateParts:
    """Pieces lifted from the teacher's template document."""

    def __init__(self, path):
        self.streams, self.clsid = hwp5.load_streams(path)
        self.docinfo_records = hwp5.parse_records(hwp5.inflate(self.streams['DocInfo']))
        self.section = hwp5.parse_records(hwp5.inflate(self.streams['BodyText/Section0']))
        sec = self.section
        self.first_end = next(i for i in range(1, len(sec))
                              if sec[i].tag == TAG_PARA_HEADER and sec[i].level == 0)
        self.bogi_table = None
        for i, r in enumerate(sec):
            if r.tag == TAG_CTRL_HEADER and r.data[:4] == CTRL_TBL and r.level == 1 and i > self.first_end:
                span = self._subtree(i)
                t = TemplateTable(span)
                if any('< 보' in v and '기 >' in v for v in t.cell_texts().values()):
                    self.bogi_table = span
                    break
        if self.bogi_table is None:
            raise ValueError('template has no "< 보기 >" box')

    def _subtree(self, i):
        lvl = self.section[i].level
        j = i + 1
        while j < len(self.section) and self.section[j].level > lvl:
            j += 1
        return self.section[i:j]

    def preamble(self, title):
        """First paragraph (section/column definition, headers, footers)."""
        recs = [r.copy() for r in self.section[:self.first_end]]
        for k, r in enumerate(recs):
            if r.tag != TAG_PARA_TEXT:
                continue
            text = r.data.decode('utf-16-le', 'replace')
            if text.startswith('실전 문제'):
                new_text = '실전 문제 ' + title + '\r'
                recs[k] = Record(r.tag, r.level, new_text.encode('utf-16-le'))
                hdr = recs[k - 1]
                assert hdr.tag == TAG_PARA_HEADER
                d = bytearray(hdr.data)
                n, = struct.unpack_from('<I', d, 0)
                struct.pack_into('<I', d, 0, (n & 0x80000000) | len(new_text))
                recs[k - 1] = Record(hdr.tag, hdr.level, d)
        return recs

    def header_bindata_ids(self):
        ids = set()
        for r in self.section[:self.first_end]:
            if r.tag == TAG_SHAPE_COMPONENT_PICTURE:
                ids.add(struct.unpack_from('<H', r.data, 71)[0])
        return ids


# ------------------------------------------------------------------ helpers

PREFIX_RE = re.compile(r'^(\s*(?:•|·|○|-|※)\s*|\s*[갑을병정무]\s*[:.]\s*|\s*[가-힣]{1,3}\s*:\s*|'
                       r'\s*\((?:[가-하])\)\s*|\s*[㈎-㈛]\s*|\s*[ㄱ-ㅎ]\.\s*|\s*[A-Z]\.\s*|\s*[①-⑩⑴-⑽]\s*)')
LABEL_START_RE = re.compile(r'^\s*(\([가-하]\)|[㈎-㈛])\s*')
BOGI_LABEL_RE = re.compile(r'^\s*([ㄱ-ㅎ]|[갑을병정무])\s*[.．]')


def hanging_indent(text, size=900, ratio=100, spacing=0):
    m = PREFIX_RE.match(text)
    if not m or len(m.group(0)) > 8:
        return 0
    w = layout.text_width(m.group(0), size, ratio, spacing)
    return -int(round(w * 2))


def split_answer_parts(answer):
    """'⑴ A ⑵ B' -> {1: 'A', 2: 'B'}; plain answers -> {0: answer}."""
    parts = re.split(r'([⑴⑵⑶⑷⑸⑹])', answer)
    if len(parts) < 3:
        return {0: answer.strip()}
    out = {}
    for k in range(1, len(parts), 2):
        out[V.PAREN_NUM.index(parts[k]) + 1] = parts[k + 1].strip()
    return out


def answer_line(text, explanation=''):
    t = text.strip()
    if t.replace(' ', '') in ('해설참조', '해설참고') and explanation:
        t = explanation.strip()
    if t.startswith(V.EXAMPLE_MARK):
        return '예시 답안: ' + t[len(V.EXAMPLE_MARK):].strip()
    return '답: ' + t


def choice_index(answer):
    a = answer.strip()
    if a and a[0] in V.CIRCLED:
        return V.CIRCLED.index(a[0]) + 1
    return None


# ------------------------------------------------------------------ converter

class Converter:
    def __init__(self, template_path, answer_mode=True):
        self.tpl = TemplateParts(template_path)
        self.answer_mode = answer_mode
        self.di = DocInfo(self.tpl.docinfo_records)
        keep = self.tpl.header_bindata_ids()
        mapping = self.di.keep_bindata(keep)
        if any(k != v for k, v in mapping.items()):
            raise ValueError('header pictures would need renumbering: %r' % mapping)
        self.kept_bin = sorted(mapping)
        self.di.reset_caret()
        self.ctx = Ctx(self.di)
        self.warnings = []

    # -- char / para shapes
    def cs(self, base, fmt=frozenset(), red=False):
        if red and self.answer_mode:
            base = RED_OF.get(base, base)
        bits = 0
        if 'u' in fmt:
            bits |= CS_UNDERLINE
        if 'sub' in fmt:
            bits |= CS_SUBSCRIPT
        if 'sup' in fmt:
            bits |= CS_SUPERSCRIPT
        if 'b' in fmt:
            bits |= CS_BOLD
        bf = BF_BOX if 'box' in fmt else None
        if not bits and bf is None:
            return base
        return self.di.derive_char_shape(base, set_bits=bits, borderfill=bf)

    def ps_box(self, para, base=PS_BODY, size=900):
        ind = hanging_indent(para.text, size) if para.hanging else 0
        return self.di.derive_para_shape(base, indent=ind)

    def runs_para(self, runs, ps, style, base_cs, red=False, width=COLUMN_WIDTH, prefix=''):
        p = Para(ps, style, width=width, cs=self.cs(base_cs, red=red))
        if prefix:
            p.add_text(prefix, self.cs(base_cs, red=red))
        for r in runs:
            text = r.text.replace('\r', '').replace('\n', ' ')
            if V.EXAMPLE_MARK in text:
                text = text.replace(V.EXAMPLE_MARK, '')
            p.add_text(text, self.cs(base_cs, r.fmt, red=red))
        return p

    def text_para(self, text, ps, style, cs, width=COLUMN_WIDTH):
        return Para(ps, style, width=width, cs=cs).add_text(text, cs)

    def blank(self, ps=PS_BODY2, style=ST_NORMAL, cs=CS_TEXT):
        return Para(ps, style, cs=cs)

    # -- content blocks
    def picture_para(self, pic, max_width, ps=PS_CENTER, style=ST_NORMAL):
        bid = self.ctx.add_image(pic.data, pic.ext)
        w, h = pic.width, pic.height
        if w > max_width:
            h = int(h * max_width / float(w))
            w = max_width
        ctrl = Picture(bid, w, h, pic.ori_width, pic.ori_height, pic.clip)
        return Para(ps, style, cs=CS_TEXT).add_ctrl(ctrl, CS_TEXT)

    def cell_paras(self, blocks, width, base_cs=CS_TEXT, base_ps=PS_BODY, style=ST_BASIC, center=False):
        paras = []
        for b in blocks:
            if isinstance(b, V.Picture):
                paras.append(self.picture_para(b, width - 1020))
            elif isinstance(b, V.Table):
                paras.append(Para(PS_CENTER, ST_NORMAL, cs=base_cs).add_ctrl(
                    self.grid_table(b, width - 1020 - 600), base_cs))
            else:
                if center or b.align == 'center':
                    ps = PS_CENTER
                else:
                    ps = self.ps_box(b, base_ps)
                paras.append(self.runs_para(b.runs, ps, style if ps != PS_CENTER else ST_NORMAL, base_cs))
        if not paras:
            paras.append(self.blank(PS_BODY, ST_BASIC))
        return paras

    def box_table(self, blocks, width=BOX_WIDTH):
        cell = Cell(0, 0, width, self.cell_paras(blocks, width))
        return Table(1, 1, [cell], hdr_props=0x082A2311, tbl_props=0x04000006, bf=BF_BOX)

    def label_table(self, rows, width=BOX_WIDTH - 42):
        cells = []
        content_w = width - LABEL_COL
        for r, (label, blocks) in enumerate(rows):
            lp = [self.text_para(label, PS_CENTER, ST_NORMAL, CS_TABLE_TEXT)]
            cells.append(Cell(r, 0, LABEL_COL, lp, bf=BF_LABEL, flags=0x01000020))
            cells.append(Cell(r, 1, content_w,
                              self.cell_paras(blocks, content_w, CS_TABLE_TEXT, 35, ST_NORMAL),
                              bf=BF_BOX, flags=0x05000020))
        return Table(len(rows), 2, cells, hdr_props=0x082A2211, tbl_props=0x04000006, bf=BF_BOX)

    def grid_table(self, t, width=BOX_WIDTH, red_row=None, header_rows=0):
        # column widths from the first cells that do not span
        colw = {}
        for c in t.cells:
            if c.colspan == 1:
                colw.setdefault(c.col, c.width)
        for c in t.cells:
            if c.colspan > 1:
                missing = [k for k in range(c.col, c.col + c.colspan) if k not in colw]
                for k in missing:
                    colw[k] = c.width // c.colspan
        total = sum(colw.get(k, 1000) for k in range(t.cols))
        scale = width / float(total)
        widths = [int(colw.get(k, 1000) * scale) for k in range(t.cols)]
        widths[-1] += width - sum(widths)
        cells = []
        for c in t.cells:
            w = sum(widths[c.col:c.col + c.colspan])
            red = red_row is not None and c.row == red_row
            base = CS_CHOICE
            paras = []
            for b in c.blocks:
                if isinstance(b, V.Para):
                    paras.append(self.runs_para(b.runs, PS_CENTER, ST_NORMAL, base, red=red))
                elif isinstance(b, V.Picture):
                    paras.append(self.picture_para(b, w - 400))
            if not paras:
                paras.append(Para(PS_CENTER, ST_NORMAL, cs=base))
            shaded = c.row < header_rows or c.shaded
            cells.append(Cell(c.row, c.col, w, paras, rowspan=c.rowspan, colspan=c.colspan,
                              bf=BF_LABEL if shaded else BF_BOX, flags=0x01000020,
                              margins=(200, 200, 141, 141)))
        return Table(t.rows, t.cols, cells, hdr_props=0x082A2211, tbl_props=0x04000006, bf=BF_BOX)

    def boxed_list(self, label, paras):
        """The template's "< 보기 >" box with a new label and content."""
        tt = TemplateTable([r.copy() for r in self.tpl.bogi_table])
        texts = tt.cell_texts()
        label_pos = next(pos for pos, t in texts.items() if '<' in t and '보' in t and '기' in t)
        # content cell: the cell holding the template's own <보기> sentences
        content_pos = max((pos for pos in texts if pos != label_pos),
                          key=lambda pos: len(texts[pos].strip()))
        text = '< %s >' % label
        # widen the label column when the title would not fit on one line
        cols = tt.column_widths()
        lc = label_pos[1]
        need = int(layout.text_width(text, 900, 100, 0) * 1.08) + 1020 + 300
        if need > cols[lc] and 0 < lc < len(cols) - 1:
            extra = need - cols[lc]
            cols[lc] += extra
            cols[lc - 1] -= extra - extra // 2
            cols[lc + 1] -= extra // 2
            tt.set_column_widths(cols)
        tt.replacements[label_pos] = [self.text_para(text, 28, ST_BASIC, CS_TEXT)]
        tt.replacements[content_pos] = paras
        return tt

    # -- passages
    def passage_paras(self, el):
        """Return list of Para for a passage element (box, label table, grid)."""
        if el.table is None:
            return self.boxed_blocks(el.blocks)
        t = el.table
        rows = V.split_label_rows(t)
        if rows:
            return [Para(PS_BODY, ST_BASIC, cs=CS_TEXT).add_ctrl(self.label_table(rows), CS_TEXT)]
        # stack all cells of simple 1xN / Nx1 layouts into one box
        if t.rows == 1 or t.cols == 1:
            blocks = []
            for c in sorted(t.cells, key=lambda c: (c.row, c.col)):
                blocks.extend(c.blocks)
            lab = self.labelled_rows(blocks)
            if lab:
                return [Para(PS_BODY, ST_BASIC, cs=CS_TEXT).add_ctrl(self.label_table(lab), CS_TEXT)]
            return self.boxed_blocks(blocks)
        return [Para(PS_BODY2, ST_NORMAL, cs=CS_TEXT).add_ctrl(self.grid_table(t), CS_TEXT)]

    def boxed_blocks(self, blocks):
        """Text goes into the passage box; pictures stand alone (no frame)."""
        out = []
        pending = []

        def flush():
            if any(not isinstance(b, V.Para) or b.text.strip() for b in pending):
                out.append(Para(PS_BODY2, ST_NORMAL, cs=CS_TEXT).add_ctrl(self.box_table(pending), CS_TEXT))
            pending.clear()

        for b in blocks:
            if isinstance(b, V.Picture):
                flush()
                out.append(self.picture_para(b, COLUMN_WIDTH))
            else:
                pending.append(b)
        flush()
        return out

    def labelled_rows(self, blocks):
        """Paragraphs starting with (가)/(나) or ㈎/㈏ -> [(label, blocks)]."""
        rows = []
        for b in blocks:
            if isinstance(b, V.Para):
                m = LABEL_START_RE.match(b.text)
                if m:
                    para = V.Para(list(b.runs), b.align)
                    V._strip_prefix(para, m.end())
                    rows.append((m.group(1), [para]))
                    continue
            if not rows:
                return None
            rows[-1][1].append(b)
        if len(rows) < 2:
            return None
        labels = [r[0] for r in rows]
        if len(set(labels)) != len(labels):
            return None
        return rows

    # -- problem
    def convert_problem(self, prob, number, out):
        answer_idx = choice_index(prob.answer)
        bogi_el = next((e for e in prob.elements if e.kind == 'bogi'), None)
        combo = next((e for e in prob.elements if e.kind == 'inline_choices'), None)
        red_labels = set()
        drop_combo = False
        if bogi_el is not None and combo is not None:
            item_labels = [BOGI_LABEL_RE.match(p.text).group(1) if BOGI_LABEL_RE.match(p.text) else None
                           for p in bogi_el.items]
            if answer_idx and answer_idx <= len(combo.items):
                want = [x.strip() for x in re.split(r'[,，]', combo.items[answer_idx - 1].text) if x.strip()]
                if want and all(w in item_labels for w in want):
                    red_labels = set(want)
                    drop_combo = True
            if not drop_combo:
                self.warnings.append('%d번: <보기> 정답 조합을 해석하지 못해 선지를 유지함' % number)
        direct_labels = False
        if bogi_el is not None and combo is None and answer_idx is None:
            want = [x.strip() for x in re.split(r'[,，]', prob.answer) if x.strip()]
            if want and all(re.fullmatch(r'[ㄱ-ㅎ]|[갑을병정무]', w) for w in want):
                red_labels = set(want)
                direct_labels = True

        # shared passage (ITEM_PARAGRAPH)
        if prob.shared is not None:
            intro = prob.shared_intro
            out.append(self.runs_para(intro.runs, PS_BODY, ST_BASIC, CS_TEXT))
            for el in prob.shared:
                if el.kind == 'passage':
                    out.extend(self.passage_paras(el))
                elif el.kind == 'text':
                    out.append(self.runs_para(el.blocks[0].runs, PS_BODY, ST_BASIC, CS_TEXT))
            out.append(self.blank())

        # stem
        stem_runs = list(prob.stem.runs)
        if drop_combo:
            stem_runs = self.select_all_stem(stem_runs)
        out.append(self.runs_para(stem_runs, PS_STEM, ST_BASIC, CS_TEXT))

        sub_answers = split_answer_parts(prob.answer) if answer_idx is None and not direct_labels else {}
        current_sub = 0
        self._answer_left = 0
        placed = set()
        had_choices = False
        elements = prob.elements
        for k, el in enumerate(elements):
            nxt = elements[k + 1] if k + 1 < len(elements) else None
            if el.kind == 'passage':
                paras = self.passage_paras(el)
                if nxt is not None and nxt.kind in ('bogi', 'keybox'):
                    paras[-1].ps = PS_TIGHT
                    paras[-1].style = ST_BASIC
                out.extend(paras)
            elif el.kind == 'bogi':
                items = []
                for p in el.items:
                    m = BOGI_LABEL_RE.match(p.text)
                    red = bool(m and m.group(1) in red_labels)
                    items.append(self.runs_para(p.runs, PS_BOGI, ST_BOGI, CS_CHOICE, red=red))
                out.append(Para(PS_BODY, ST_BASIC, cs=CS_TEXT).add_ctrl(self.boxed_list('보기', items), CS_TEXT))
            elif el.kind == 'keybox':
                label = re.sub(r'\s+', ' ', el.label)
                paras = self.cell_paras(el.blocks, 26364, CS_CHOICE, PS_BOGI, ST_BOGI)
                out.append(Para(PS_BODY, ST_BASIC, cs=CS_TEXT).add_ctrl(self.boxed_list(label, paras), CS_TEXT))
            elif el.kind == 'choices':
                had_choices = True
                out.extend(self.choice_paras(el.items, answer_idx))
            elif el.kind == 'inline_choices':
                if drop_combo:
                    continue
                had_choices = True
                out.extend(self.choice_paras(el.items, answer_idx))
            elif el.kind == 'match_table':
                had_choices = True
                red_row = answer_idx if answer_idx else None
                t = self.grid_table(el.table, BOX_WIDTH, red_row=red_row, header_rows=1)
                out.append(Para(PS_BODY2, ST_NORMAL, cs=CS_TEXT).add_ctrl(t, CS_TEXT))
            elif el.kind == 'subq':
                if self.answer_mode and current_sub and current_sub not in placed:
                    self.place_answer(sub_answers.get(current_sub), prob, out)
                    placed.add(current_sub)
                current_sub += 1
                para = el.blocks[0]
                ind = hanging_indent(para.text)
                ps = self.di.derive_para_shape(PS_BODY, indent=ind)
                out.append(self.runs_para(para.runs, ps, ST_BASIC, CS_TEXT))
                self._answer_left = -ind
            elif el.kind == 'space':
                if self.answer_mode:
                    key = current_sub if current_sub else 0
                    if key not in placed and (key in sub_answers):
                        self.place_answer(sub_answers.get(key), prob, out)
                        placed.add(key)
                else:
                    for _ in range(min(el.count, 3)):
                        out.append(self.blank(PS_BODY, ST_BASIC))
            elif el.kind == 'text':
                out.append(self.runs_para(el.blocks[0].runs, PS_BODY, ST_BASIC, CS_TEXT))
        if self.answer_mode:
            if answer_idx is None:
                for key in sorted(sub_answers):
                    if key not in placed:
                        self.place_answer(sub_answers[key], prob, out)
                        placed.add(key)
            elif not had_choices and not drop_combo and not red_labels:
                out.append(self.text_para('정답: %s  (원본 자료에 선지가 없습니다.)' % prob.answer.strip(),
                                          PS_BODY, ST_BASIC, self.cs(CS_TEXT, red=True)))
                self.warnings.append('%d번: 원본에 선지가 없어 정답만 표시함' % number)
        elif not had_choices and answer_idx is None and not any(e.kind == 'space' for e in elements):
            for _ in range(3):
                out.append(self.blank(PS_BODY, ST_BASIC))
        out.append(self.blank())

    def place_answer(self, text, prob, out):
        if not text:
            return
        line = answer_line(text, prob.explanation)
        ps = self.di.derive_para_shape(PS_BODY, left=self._answer_left) if self._answer_left else PS_BODY
        para = Para(ps, ST_BASIC, cs=self.cs(CS_TEXT, red=True))
        para.add_text(line, self.cs(CS_TEXT, red=True))
        out.append(para)

    def select_all_stem(self, runs):
        text = ''.join(r.text for r in runs)
        if '있는 대로' in text:
            return runs
        out = []
        done = False
        for r in runs:
            if not done and re.search(r'<\s*보\s*기\s*>에서\s*고른', r.text):
                r = V.Run(re.sub(r'(<\s*보\s*기\s*>에서)\s*고른', r'\1 있는 대로 고른', r.text, count=1), r.fmt)
                done = True
            out.append(r)
        return out

    def choice_paras(self, items, answer_idx):
        texts = [p.text.strip() for p in items]
        widths = [layout.text_width('① ' + t, 900, 95, -3) for t in texts]
        paras = []
        if len(items) == 5 and max(widths) < 4200:
            gap = (COLUMN_WIDTH - 600 - sum(widths)) / 4.0
            nsp = max(2, min(10, int(gap / layout.text_width(' ', 900, 95, -3))))
            p = Para(PS_CHOICE, ST_CHOICE, cs=CS_CHOICE)
            for i, item in enumerate(items, 1):
                red = (i == answer_idx)
                if i > 1:
                    p.add_text(' ' * nsp, self.cs(CS_CHOICE))
                p.add_text(V.CIRCLED[i - 1] + ' ', self.cs(CS_CHOICE, red=red))
                for r in item.runs:
                    p.add_text(r.text.strip() if len(item.runs) == 1 else r.text,
                               self.cs(CS_CHOICE, r.fmt, red=red))
            return [p]
        for i, item in enumerate(items, 1):
            red = (i == answer_idx)
            paras.append(self.runs_para(item.runs, PS_CHOICE, ST_CHOICE, CS_CHOICE, red=red,
                                        prefix=V.CIRCLED[i - 1] + ' '))
        return paras

    # -- document
    def build_section(self, problems, title):
        recs = list(self.tpl.preamble(title))
        paras = []
        for n, prob in enumerate(problems, 1):
            self.convert_problem(prob, n, paras)
        # drop trailing blanks
        while len(paras) > 1 and not paras[-1].segments:
            paras.pop()
        for i, p in enumerate(paras):
            r, _ = p.build(self.ctx, 0, last=(i == len(paras) - 1))
            recs.extend(r)
        return recs

    def prv_text(self, problems):
        out = []
        for n, p in enumerate(problems, 1):
            out.append('%d. %s' % (n, p.stem.text.strip()))
        text = '\r\n'.join(out)[:1000]
        return text.encode('utf-16-le')

    def write(self, problems, title, out_path):
        section = self.build_section(problems, title)
        streams = {}
        for name, data in self.tpl.streams.items():
            if name.startswith('BinData/') or name in ('PrvImage',):
                continue
            streams[name] = data
        # template pictures still referenced by header / master page
        kept = {bid: ext for bid, ext, _ in self.di.bindata_entries() if bid in self.kept_bin}
        for bid, ext in kept.items():
            src = [n for n in self.tpl.streams if n.lower() == ('BinData/BIN%04X.%s' % (bid, ext)).lower()]
            streams['BinData/BIN%04X.%s' % (bid, ext)] = self.tpl.streams[src[0]]
        for bid, ext, data in self.ctx.bindata:
            streams['BinData/BIN%04X.%s' % (bid, ext)] = hwp5.deflate(data)
        streams['DocInfo'] = hwp5.deflate(hwp5.serialize_records(self.di.records))
        streams['BodyText/Section0'] = hwp5.deflate(hwp5.serialize_records(section))
        streams['PrvText'] = self.prv_text(problems)
        order = ['FileHeader', 'DocInfo', 'BodyText/Section0'] + \
                sorted(n for n in streams if n.startswith('BinData/')) + \
                [n for n in streams if n not in ('FileHeader', 'DocInfo', 'BodyText/Section0')
                 and not n.startswith('BinData/')]
        return hwp5.write_hwp(out_path, streams, self.tpl.clsid, order=order)


def parse_source(source):
    if source.lower().endswith('.docx'):
        from . import docx
        return docx.parse(source)
    return V.parse(source)


def convert(template, source, out_path, answer_mode=True, title=None):
    doc, problems = parse_source(source)
    if title is None:
        title = re.sub(r'\s*\([^)]*\)\s*$', '', doc.title or '').strip() or '실전 문제'
    conv = Converter(template, answer_mode=answer_mode)
    size = conv.write(problems, title, out_path)
    return dict(problems=len(problems), title=title, size=size, warnings=conv.warnings,
                images=len(conv.ctx.bindata))
