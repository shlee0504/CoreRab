"""Editable view over a template's DocInfo stream (styles, fonts, bin data)."""
import struct

from .hwp5 import (Record, TAG_ID_MAPPINGS, TAG_BIN_DATA, TAG_CHAR_SHAPE,
                   TAG_PARA_SHAPE, TAG_DOCUMENT_PROPERTIES, TAG_BORDER_FILL)

# index of each list inside HWPTAG_ID_MAPPINGS
IDX_BINDATA = 0
IDX_BORDERFILL = 8
IDX_CHARSHAPE = 9
IDX_PARASHAPE = 13

# CHAR_SHAPE property bits
CS_ITALIC = 1 << 0
CS_BOLD = 1 << 1
CS_UNDERLINE = 1 << 2        # underline type 1 (below) in bits 2-3
CS_SUPERSCRIPT = 1 << 15
CS_SUBSCRIPT = 1 << 16


class DocInfo:
    def __init__(self, records):
        self.records = list(records)
        self._derived = {}

    # -- helpers
    def _idm(self):
        return next(r for r in self.records if r.tag == TAG_ID_MAPPINGS)

    def counts(self):
        d = self._idm().data
        return list(struct.unpack('<%di' % (len(d) // 4), d))

    def _set_count(self, idx, value):
        c = self.counts()
        c[idx] = value
        idm = self._idm()
        i = self.records.index(idm)
        self.records[i] = Record(idm.tag, idm.level, struct.pack('<%di' % len(c), *c))

    def items(self, tag):
        return [r for r in self.records if r.tag == tag]

    def _append(self, tag, data, idx):
        same = [i for i, r in enumerate(self.records) if r.tag == tag]
        pos = same[-1] + 1
        level = self.records[same[-1]].level
        self.records.insert(pos, Record(tag, level, data))
        self._set_count(idx, len(same) + 1)
        return len(same)          # zero-based index of the new item

    def derive_border_fill(self, base_id, fill_color):
        """Copy of border fill `base_id` (1-based) with a solid fill colour
        (0xBBGGRR); returns the new 1-based id."""
        key = ('bf', base_id, fill_color)
        if key in self._derived:
            return self._derived[key]
        d = bytearray(self.items(TAG_BORDER_FILL)[base_id - 1].data)
        assert struct.unpack_from('<I', d, 32)[0] & 1, 'base border fill has no solid fill'
        struct.pack_into('<I', d, 36, fill_color)
        d = bytes(d)
        for i, r in enumerate(self.items(TAG_BORDER_FILL)):
            if r.data == d:
                self._derived[key] = i + 1
                return i + 1
        new = self._append(TAG_BORDER_FILL, d, IDX_BORDERFILL) + 1
        self._derived[key] = new
        return new

    # -- document properties
    def reset_caret(self):
        r = next(r for r in self.records if r.tag == TAG_DOCUMENT_PROPERTIES)
        d = bytearray(r.data)
        if len(d) >= 26:
            struct.pack_into('<III', d, 14, 0, 0, 0)
        self.records[self.records.index(r)] = Record(r.tag, r.level, d)

    def set_section_count(self, n):
        r = next(r for r in self.records if r.tag == TAG_DOCUMENT_PROPERTIES)
        d = bytearray(r.data)
        struct.pack_into('<H', d, 0, n)
        self.records[self.records.index(r)] = Record(r.tag, r.level, d)

    # -- bin data
    def keep_bindata(self, keep_ids):
        """Drop BIN_DATA entries not in keep_ids; returns old->new id map."""
        bins = self.items(TAG_BIN_DATA)
        mapping = {}
        new_id = 0
        for i, r in enumerate(bins, start=1):
            if i in keep_ids:
                new_id += 1
                mapping[i] = new_id
                d = bytearray(r.data)
                struct.pack_into('<H', d, 2, new_id)
                self.records[self.records.index(r)] = Record(r.tag, r.level, d)
            else:
                self.records.remove(r)
        self._set_count(IDX_BINDATA, new_id)
        return mapping

    def bindata_entries(self):
        out = []
        for r in self.items(TAG_BIN_DATA):
            props, bid, n = struct.unpack_from('<HHH', r.data, 0)
            ext = r.data[6:6 + 2 * n].decode('utf-16-le')
            out.append((bid, ext, props))
        return out

    def add_bindata(self, ext):
        bins = self.items(TAG_BIN_DATA)
        bid = len(bins) + 1
        ext16 = ext.encode('utf-16-le')
        data = struct.pack('<HHH', 0x0001, bid, len(ext)) + ext16
        if bins:
            self._append(TAG_BIN_DATA, data, IDX_BINDATA)
        else:
            raise RuntimeError('template has no BIN_DATA list to extend')
        return bid

    # -- char shapes
    def char_shape(self, cs_id):
        return self.items(TAG_CHAR_SHAPE)[cs_id].data

    def derive_char_shape(self, base_id, set_bits=0, clear_bits=0, color=None,
                          size=None, borderfill=None):
        key = ('cs', base_id, set_bits, clear_bits, color, size, borderfill)
        cache = self._derived
        if key in cache:
            return cache[key]
        d = bytearray(self.char_shape(base_id))
        prop, = struct.unpack_from('<I', d, 46)
        prop = (prop | set_bits) & ~clear_bits
        struct.pack_into('<I', d, 46, prop)
        if color is not None:
            struct.pack_into('<I', d, 52, color)
        if size is not None:
            struct.pack_into('<i', d, 42, size)
        if borderfill is not None:
            struct.pack_into('<H', d, 68, borderfill)
        d = bytes(d)
        # reuse an identical existing shape if there is one
        for i, r in enumerate(self.items(TAG_CHAR_SHAPE)):
            if r.data == d:
                cache[key] = i
                return i
        new = self._append(TAG_CHAR_SHAPE, d, IDX_CHARSHAPE)
        cache[key] = new
        return new

    # -- para shapes
    def para_shape(self, ps_id):
        return self.items(TAG_PARA_SHAPE)[ps_id].data

    def derive_para_shape(self, base_id, align=None, indent=None, left=None,
                          right=None, prev=None, next_=None, linespacing=None):
        key = ('ps', base_id, align, indent, left, right, prev, next_, linespacing)
        cache = self._derived
        if key in cache:
            return cache[key]
        d = bytearray(self.para_shape(base_id))
        if align is not None:
            code = {'justify': 0, 'left': 1, 'right': 2, 'center': 3}[align]
            prop, = struct.unpack_from('<I', d, 0)
            prop = (prop & ~(0x7 << 2)) | (code << 2)
            struct.pack_into('<I', d, 0, prop)
        for off, val in ((4, left), (8, right), (12, indent), (16, prev), (20, next_)):
            if val is not None:
                struct.pack_into('<i', d, off, val)
        if linespacing is not None:
            struct.pack_into('<i', d, 24, linespacing)
            struct.pack_into('<I', d, 50, linespacing)
        d = bytes(d)
        for i, r in enumerate(self.items(TAG_PARA_SHAPE)):
            if r.data == d:
                cache[key] = i
                return i
        new = self._append(TAG_PARA_SHAPE, d, IDX_PARASHAPE)
        cache[key] = new
        return new

    def para_shape_info(self, ps_id):
        d = self.para_shape(ps_id)
        prop, left, right, indent, prev, nxt, ls = struct.unpack_from('<Iiiiiii', d, 0)
        align = {0: 'justify', 1: 'left', 2: 'right', 3: 'center'}.get((prop >> 2) & 7, 'justify')
        heading = (prop >> 23) & 3
        return dict(align=align, left=left, right=right, indent=indent, prev=prev,
                    next=nxt, linespacing=ls, linespacing_type=prop & 3, heading=heading)

    def char_shape_info(self, cs_id):
        d = self.char_shape(cs_id)
        ratio = d[14]
        spacing = struct.unpack_from('<b', d, 21)[0]
        size, prop = struct.unpack_from('<iI', d, 42)
        return dict(size=size, ratio=ratio, spacing=spacing, prop=prop)
