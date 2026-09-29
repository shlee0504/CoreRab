"""Build HWP 5 body records (paragraphs, tables, pictures) in the template's style."""
import struct

from .hwp5 import (Record, TAG_PARA_HEADER, TAG_PARA_TEXT, TAG_PARA_CHAR_SHAPE,
                   TAG_PARA_LINE_SEG, TAG_CTRL_HEADER, TAG_LIST_HEADER, TAG_TABLE,
                   TAG_SHAPE_COMPONENT, TAG_SHAPE_COMPONENT_PICTURE)
from . import layout

CTRL_TBL = b' lbt'      # 'tbl ' as stored (little-endian ctrl id)
CTRL_GSO = b' osg'      # 'gso '
PIC_ID = b'cip$'        # '$pic'

LAST_PARA = 0x80000000
LINE_FIRST = 0x00060000
LINE_NEXT = 0x00160000
LINE_HEAD = 0x00200000


class Ctx:
    """Shared state while generating one section."""

    def __init__(self, docinfo):
        self.docinfo = docinfo
        self._inst = 0x3A5C0000
        self._z = 100
        self.bindata = []            # (id, ext, bytes)
        self._cs_cache = {}
        self._ps_cache = {}

    def inst_id(self):
        self._inst += 7
        return self._inst & 0x7FFFFFFF

    def z_order(self):
        self._z += 1
        return self._z

    def cs_info(self, cs):
        if cs not in self._cs_cache:
            self._cs_cache[cs] = self.docinfo.char_shape_info(cs)
        return self._cs_cache[cs]

    def ps_info(self, ps):
        if ps not in self._ps_cache:
            self._ps_cache[ps] = self.docinfo.para_shape_info(ps)
        return self._ps_cache[ps]

    def add_image(self, data, ext):
        for bid, e, d in self.bindata:
            if d == data:
                return bid
        bid = self.docinfo.add_bindata(ext)
        self.bindata.append((bid, ext, data))
        return bid


# ------------------------------------------------------------------ columns

class ColumnDef:
    """Column definition control (단 설정) taken from the template, with a new
    column count; placed at the start of a paragraph to change the layout."""
    char_code = 0x0002
    width = height = 0

    def __init__(self, template_data, count):
        d = bytearray(template_data)
        self.ctrl_id = bytes(d[:4])
        attr, = struct.unpack_from('<H', d, 4)
        struct.pack_into('<H', d, 4, (attr & ~(0xFF << 2)) | (count << 2))
        self.data = bytes(d)

    def build(self, ctx, level):
        return [Record(TAG_CTRL_HEADER, level, self.data)]


# ------------------------------------------------------------------ paragraph

class Para:
    """A paragraph: list of segments ('t', text, cs) or ('c', control, cs)."""

    def __init__(self, ps, style, segments=None, width=27780, cs=7):
        self.ps = ps
        self.style = style
        self.segments = segments or []
        self.width = width          # available line width (HWPUNIT)
        self.cs = cs                # char shape of the paragraph mark
        self.split = 0              # 0x04: page break before

    def add_text(self, text, cs):
        if text:
            self.segments.append(('t', text, cs))
        return self

    def add_ctrl(self, ctrl, cs):
        self.segments.append(('c', ctrl, cs))
        return self

    def plain_text(self):
        return ''.join(s[1] for s in self.segments if s[0] == 't')

    # -- encoding
    def _encode(self):
        units = []        # list of (utf16 code units, cs) per logical char
        mask = 0
        for seg in self.segments:
            if seg[0] == 't':
                for ch in seg[1]:
                    if ch == '\n':
                        units.append(([0x000A], seg[2]))
                        mask |= 1 << 0x0A
                    elif ch == '\t':
                        units.append(([0x0009, 0x0FA0, 0, 0x0100, 0, 0, 0, 0x0009], seg[2]))
                        mask |= 1 << 0x09
                    else:
                        b = ch.encode('utf-16-le')
                        units.append((list(struct.unpack('<%dH' % (len(b) // 2), b)), seg[2]))
            else:
                ctrl = seg[1]
                code = getattr(ctrl, 'char_code', 0x000B)
                lo, hi = struct.unpack('<HH', ctrl.ctrl_id)
                units.append(([code, lo, hi, 0, 0, 0, 0, code], seg[2]))
                mask |= 1 << code
        return units, mask

    def build(self, ctx, level, last=False, y0=0):
        # controls first: their size feeds the line layout of this paragraph
        ctrl_recs = []
        for seg in self.segments:
            if seg[0] == 'c':
                ctrl_recs.extend(seg[1].build(ctx, level + 1))
        units, mask = self._encode()
        end_cs = self.segments[-1][2] if self.segments else self.cs
        code_units = []
        char_shapes = []
        pos = 0
        char_starts = []           # code unit offset of each logical char
        for u, cs in units:
            if not char_shapes or char_shapes[-1][1] != cs:
                char_shapes.append((pos, cs))
            char_starts.append(pos)
            code_units.extend(u)
            pos += len(u)
        code_units.append(0x000D)
        if not char_shapes:
            char_shapes.append((0, end_cs))
        nchars = len(code_units)
        lines, height = self._line_segs(ctx, char_starts, pos, y0)
        recs = []
        hdr = struct.pack('<IIHBBHHHIH', nchars | (LAST_PARA if last else 0), mask,
                          self.ps, self.style, self.split, len(char_shapes), 0, len(lines),
                          0x80000000, 0)
        recs.append(Record(TAG_PARA_HEADER, level, hdr))
        if nchars > 1:
            recs.append(Record(TAG_PARA_TEXT, level + 1,
                               struct.pack('<%dH' % len(code_units), *code_units)))
        recs.append(Record(TAG_PARA_CHAR_SHAPE, level + 1,
                           b''.join(struct.pack('<II', p, c) for p, c in char_shapes)))
        recs.append(Record(TAG_PARA_LINE_SEG, level + 1, b''.join(lines)))
        recs.extend(ctrl_recs)
        return recs, height

    def _line_segs(self, ctx, char_starts, total_units, y0):
        info = ctx.ps_info(self.ps)
        indent = info['indent'] // 2
        left = info['left'] // 2
        right = info['right'] // 2
        ls_ratio = info['linespacing'] if info['linespacing_type'] == 0 else 160
        width = max(1000, self.width - left - right)
        numbered = info['heading'] != 0
        first_w = width - max(0, indent) - (1650 if numbered else 0)
        other_w = width + min(0, indent)
        # measurable pieces (controls measured as their width)
        pieces = []
        heights = []
        for seg in self.segments:
            ci = ctx.cs_info(seg[2])
            if seg[0] == 't':
                text = seg[1]
                pieces.append((text, ci['size'], ci['ratio'], ci['spacing']))
                heights.extend([ci['size']] * len(text))
            else:
                pieces.append(('￼', 0, 100, 0))
                heights.append(max(ci['size'], seg[1].height))
        ctrl_widths = [s[1].width for s in self.segments if s[0] == 'c']
        if ctrl_widths:
            # objects occupy their own width on the line
            chars = []
            ci_iter = iter(ctrl_widths)
            for text, size, ratio, spacing in pieces:
                if text == '￼':
                    chars.append(('￼', next(ci_iter)))
                else:
                    for ch in text:
                        chars.append((ch, layout.char_em(ch) * size * ratio / 100.0 + size * spacing / 100.0))
            lines = _break_measured(chars, first_w, other_w)
        else:
            lines = layout.break_lines(pieces, first_w, other_w)
        if not heights:
            heights = [ctx.cs_info(self.segments[-1][2] if self.segments else self.cs)['size']]
        segs = []
        y = y0
        spacing = 0
        for k, (a, b) in enumerate(lines):
            hs = heights[a:b] if b > a else [heights[min(a, len(heights) - 1)]]
            h = max(hs)
            if k == 0 and numbered:
                h = max(h, 1100)
            text_h = h
            base = int(round(h * 0.85))
            spacing = int(round(h * (ls_ratio - 100) / 100.0)) if h < 3000 else int(round(900 * (ls_ratio - 100) / 100.0))
            start_unit = char_starts[a] if a < len(char_starts) else total_units
            tag = LINE_FIRST | (LINE_HEAD if (k == 0 and numbered) else 0) if k == 0 else LINE_NEXT
            segs.append(struct.pack('<IiiiiiiiI', start_unit, y, h, text_h, base, spacing,
                                    0, width, tag))
            y += h + spacing
        # 한글 does not count the spacing below the last line of a cell
        self.last_spacing = spacing
        return segs, y - y0


def para_spacing(ctx, p):
    """Space above and below a paragraph (문단 위/아래 간격)."""
    info = ctx.ps_info(p.ps)
    extra = 0
    for seg in p.segments:
        outer = getattr(seg[1], 'outer', None) if seg[0] == 'c' else None
        if outer:
            extra += outer[2] + outer[3]
    return (info['prev'] + info['next']) // 2 + extra


def _break_measured(chars, first_w, other_w):
    lines = []
    start, n = 0, len(chars)
    while start < n:
        width = first_w if not lines else other_w
        acc = 0.0
        i = start
        while i < n:
            ch, w = chars[i]
            if ch != ' ' and acc + w > width and i > start:
                break
            acc += w
            i += 1
        lines.append((start, i))
        start = i
    return lines or [(0, 0)]


# ------------------------------------------------------------------ controls

def common_obj_header(ctrl_id, props, width, height, z, margins, inst):
    return (ctrl_id + struct.pack('<IiiIIi', props, 0, 0, width, height, z)
            + struct.pack('<4H', *margins) + struct.pack('<Ii', inst, 0) + struct.pack('<H', 0))


class Cell:
    def __init__(self, row, col, width, paras, rowspan=1, colspan=1, bf=3,
                 flags=0x01000020, margins=(510, 510, 141, 141), min_height=282,
                 tail=b'\x00' * 9):
        self.row, self.col = row, col
        self.rowspan, self.colspan = rowspan, colspan
        self.width = width
        self.paras = paras
        self.bf = bf
        self.flags = flags
        self.margins = margins
        self.min_height = min_height
        self.tail = tail
        for p in paras:
            p.width = width - margins[0] - margins[1]


class Table:
    ctrl_id = CTRL_TBL

    def __init__(self, n_rows, n_cols, cells, hdr_props=0x082A2311, tbl_props=0x04000006,
                 inner=(510, 510, 141, 141), bf=3, outer=(283, 283, 283, 283), row_extra=0):
        self.row_extra = row_extra
        self.n_rows, self.n_cols = n_rows, n_cols
        self.cells = sorted(cells, key=lambda c: (c.row, c.col))
        self.hdr_props = hdr_props
        self.tbl_props = tbl_props
        self.inner = inner
        self.bf = bf
        self.outer = outer
        self.width = sum(c.width for c in self.cells if c.row == self.cells[0].row)
        self._height = None

    @property
    def height(self):
        return self._height or 2000

    def build(self, ctx, level):
        # cell content first (to know heights)
        cell_recs = []
        row_need = {}
        for c in self.cells:
            recs = []
            y = 0
            for i, p in enumerate(c.paras):
                r, h = p.build(ctx, level + 1, last=(i == len(c.paras) - 1), y0=y)
                recs.extend(r)
                y += h + para_spacing(ctx, p)
            if c.paras:
                y -= getattr(c.paras[-1], 'last_spacing', 0)
            need = y + c.margins[2] + c.margins[3] + self.row_extra * c.rowspan
            cell_recs.append((c, recs, need))
            if c.rowspan == 1:
                row_need[c.row] = max(row_need.get(c.row, 0), need, c.min_height)
        for c, recs, need in cell_recs:
            if c.rowspan > 1:
                have = sum(row_need.get(r, c.min_height) for r in range(c.row, c.row + c.rowspan))
                if need > have:
                    last = c.row + c.rowspan - 1
                    row_need[last] = row_need.get(last, 0) + need - have
        self._height = sum(row_need.get(r, 282) for r in range(self.n_rows))
        out = [Record(TAG_CTRL_HEADER, level,
                      common_obj_header(CTRL_TBL, self.hdr_props, self.width, self._height,
                                        ctx.z_order(), self.outer, ctx.inst_id()))]
        row_counts = [sum(1 for c in self.cells if c.row == r) for r in range(self.n_rows)]
        tbl = struct.pack('<IHHH4H', self.tbl_props, self.n_rows, self.n_cols, 0, *self.inner)
        tbl += struct.pack('<%dH' % self.n_rows, *row_counts)
        tbl += struct.pack('<HH', self.bf, 0)
        out.append(Record(TAG_TABLE, level + 1, tbl))
        for c, recs, need in cell_recs:
            lh = struct.pack('<iI', len(c.paras), c.flags)
            lh += struct.pack('<4H', c.col, c.row, c.colspan, c.rowspan)
            lh += struct.pack('<II', c.width, c.min_height)
            lh += struct.pack('<4H', *c.margins)
            lh += struct.pack('<H', c.bf)
            lh += struct.pack('<I', c.width)
            lh += c.tail
            out.append(Record(TAG_LIST_HEADER, level + 1, lh))
            out.extend(recs)
        return out


class Picture:
    ctrl_id = CTRL_GSO

    def __init__(self, bin_id, width, height, ori_w, ori_h, clip, props=0x042A2311):
        self.bin_id = bin_id
        self.width, self.height = int(width), int(height)
        self.ori_w, self.ori_h = int(ori_w), int(ori_h)
        self.clip = clip
        self.props = props

    def build(self, ctx, level):
        inst = ctx.inst_id()
        out = [Record(TAG_CTRL_HEADER, level,
                      common_obj_header(CTRL_GSO, self.props, self.width, self.height,
                                        ctx.z_order(), (0, 0, 0, 0), inst))]
        sx = self.width / float(self.ori_w)
        sy = self.height / float(self.ori_h)
        sc = PIC_ID + PIC_ID + struct.pack('<iiHHIIII', 0, 0, 0, 1, self.ori_w, self.ori_h,
                                           self.width, self.height)
        sc += struct.pack('<I', 0x24080000)
        sc += struct.pack('<hii', 0, self.width // 2, self.height // 2)
        sc += struct.pack('<H', 1)
        sc += struct.pack('<6d', 1.0, 0.0, 0.0, 0.0, 1.0, 0.0)
        sc += struct.pack('<6d', sx, 0.0, 0.0, 0.0, sy, 0.0)
        sc += struct.pack('<6d', 1.0, 0.0, 0.0, 0.0, 1.0, 0.0)
        out.append(Record(TAG_SHAPE_COMPONENT, level + 1, sc))
        l, t, r, b = self.clip
        pic = struct.pack('<IiI', 0, 0, 0)
        pic += struct.pack('<8i', 0, 0, self.ori_w, 0, self.ori_w, self.ori_h, 0, self.ori_h)
        pic += struct.pack('<4i', l, t, r, b)
        pic += struct.pack('<4H', 0, 0, 0, 0)
        pic += struct.pack('<bbBH', 0, 0, 0, self.bin_id)
        pic += struct.pack('<B', 0)
        pic += struct.pack('<I', ctx.inst_id())
        pic += struct.pack('<I', 0)
        pic += struct.pack('<II', self.ori_w, self.ori_h)
        pic += b'\x00'
        out.append(Record(TAG_SHAPE_COMPONENT_PICTURE, level + 2, pic))
        return out


# ------------------------------------------------------------------ template tables

def split_paragraphs(recs, level):
    """Group a flat record list into paragraphs starting at PARA_HEADER(level)."""
    groups = []
    for r in recs:
        if r.tag == TAG_PARA_HEADER and r.level == level:
            groups.append([r])
        else:
            groups[-1].append(r)
    return groups


class TemplateTable:
    """A table copied from the template; chosen cells get new paragraphs."""
    ctrl_id = CTRL_TBL

    def __init__(self, records, replacements=None, width=None):
        # records: CTRL_HEADER(tbl) and all descendants, as in the template
        self.base_level = records[0].level
        self.header = records[0]
        self.table = records[1]
        assert self.table.tag == TAG_TABLE
        self.cells = []            # [LIST_HEADER record, [paragraph groups]]
        lvl = self.base_level + 1
        body = records[2:]
        cur = None
        for r in body:
            if r.tag == TAG_LIST_HEADER and r.level == lvl:
                cur = [r, []]
                self.cells.append(cur)
            else:
                cur[1].append(r)
        for c in self.cells:
            c[1] = split_paragraphs(c[1], lvl)
        self.replacements = replacements or {}   # (row, col) -> [Para]
        self._height = struct.unpack_from('<I', self.header.data, 20)[0]
        self.width = struct.unpack_from('<I', self.header.data, 16)[0]

    @staticmethod
    def cell_pos(list_header):
        col, row, cspan, rspan = struct.unpack_from('<4H', list_header.data, 8)
        return row, col

    @property
    def height(self):
        return self._height

    def cell_texts(self):
        out = {}
        for lh, paras in self.cells:
            t = ''
            for g in paras:
                for r in g:
                    if r.tag == TAG_PARA_TEXT:
                        t += r.data.decode('utf-16-le', 'replace')
            out[self.cell_pos(lh)] = t
        return out

    def build(self, ctx, level):
        delta = level - self.base_level
        body = []
        row_need = {}
        spans = []
        for lh, paras in self.cells:
            row, col = self.cell_pos(lh)
            cspan, rspan = struct.unpack_from('<HH', lh.data, 12)
            cell_w, stored_h = struct.unpack_from('<II', lh.data, 16)
            ml, mr, mt, mb = struct.unpack_from('<4H', lh.data, 24)
            need = stored_h
            if (row, col) in self.replacements:
                new = self.replacements[(row, col)]
                lhd = bytearray(lh.data)
                struct.pack_into('<i', lhd, 0, len(new))
                body.append(Record(TAG_LIST_HEADER, lh.level + delta, lhd))
                y = 0
                for i, p in enumerate(new):
                    p.width = cell_w - ml - mr
                    recs, h = p.build(ctx, level + 1, last=(i == len(new) - 1), y0=y)
                    body.extend(recs)
                    y += h + para_spacing(ctx, p)
                if new:
                    y -= getattr(new[-1], 'last_spacing', 0)
                need = y + mt + mb
            else:
                body.append(lh.copy(delta))
                for g in paras:
                    body.extend(r.copy(delta) for r in g)
            if rspan == 1:
                row_need[row] = max(row_need.get(row, 0), need)
            else:
                spans.append((row, rspan, need))
        for row, rspan, need in spans:
            have = sum(row_need.get(r, 0) for r in range(row, row + rspan))
            if need > have:
                last = row + rspan - 1
                row_need[last] = row_need.get(last, 0) + need - have
        self._height = sum(row_need.values())
        hd = bytearray(self.header.data)
        struct.pack_into('<I', hd, 20, self._height)
        struct.pack_into('<i', hd, 24, ctx.z_order())
        struct.pack_into('<I', hd, 36, ctx.inst_id())
        out = [Record(TAG_CTRL_HEADER, level, hd), self.table.copy(delta)]
        out.extend(body)
        return out

    # -- geometry
    def column_widths(self):
        cells = []
        for lh, _ in self.cells:
            col, row, cspan, rspan = struct.unpack_from('<4H', lh.data, 8)
            width = struct.unpack_from('<I', lh.data, 16)[0]
            cells.append((col, cspan, width))
        n = max(c + s for c, s, _ in cells)
        cols = [None] * n
        changed = True
        while changed:
            changed = False
            for col, span, width in cells:
                unknown = [k for k in range(col, col + span) if cols[k] is None]
                if len(unknown) == 1:
                    known = sum(cols[k] for k in range(col, col + span) if cols[k] is not None)
                    cols[unknown[0]] = width - known
                    changed = True
        return cols

    def set_column_widths(self, cols):
        for idx, (lh, paras) in enumerate(self.cells):
            col, row, cspan, rspan = struct.unpack_from('<4H', lh.data, 8)
            w = sum(cols[col:col + cspan])
            d = bytearray(lh.data)
            struct.pack_into('<I', d, 16, w)
            if len(d) >= 38:
                struct.pack_into('<I', d, 34, w)
            self.cells[idx][0] = Record(lh.tag, lh.level, d)
