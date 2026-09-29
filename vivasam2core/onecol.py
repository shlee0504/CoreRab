"""Move the answer/explanation part of an answer sheet (everything from the
first page break on) into a new section laid out in one column.

Works on the HWP file as saved by 한글 (so edits made there are kept).

usage: python -m vivasam2core.onecol INPUT.hwp OUTPUT.hwp
"""
import struct
import sys

from . import hwp5
from .docinfo import DocInfo
from .hwp5 import Record, TAG_PARA_HEADER, TAG_CTRL_HEADER
from .hwpbuild import ColumnDef
from .prune import LAST


def set_last(sec, i, last):
    d = bytearray(sec[i].data)
    n, = struct.unpack_from('<I', d, 0)
    struct.pack_into('<I', d, 0, (n | LAST) if last else (n & ~LAST))
    sec[i] = Record(sec[i].tag, sec[i].level, d)


def onecol(src, dst):
    streams, clsid = hwp5.load_streams(src)
    if 'BodyText/Section1' in streams:
        raise SystemExit('이미 구역이 나뉘어 있습니다: ' + src)
    sec = hwp5.parse_records(hwp5.inflate(streams['BodyText/Section0']))
    heads = [i for i, r in enumerate(sec) if r.tag == TAG_PARA_HEADER and r.level == 0]
    first_end = heads[1]
    tail = next(i for i in heads[1:] if sec[i].data[11] & 0x04)
    preamble = [r.copy() for r in sec[:first_end]]
    for k, r in enumerate(preamble):
        if r.tag == TAG_CTRL_HEADER and r.data[:4] == b'dloc':
            preamble[k] = Record(r.tag, r.level, ColumnDef(r.data, 1).data)
    set_last(preamble, 0, False)
    body, rest = sec[:tail], sec[tail:]
    # the new section starts on a new page by itself
    d = bytearray(rest[0].data)
    d[11] &= ~0x04
    rest[0] = Record(rest[0].tag, rest[0].level, d)
    last0 = max(i for i, r in enumerate(body) if r.tag == TAG_PARA_HEADER and r.level == 0)
    set_last(body, last0, True)
    sec1 = preamble + rest
    last1 = max(i for i, r in enumerate(sec1) if r.tag == TAG_PARA_HEADER and r.level == 0)
    set_last(sec1, last1, True)

    di = DocInfo(hwp5.parse_records(hwp5.inflate(streams['DocInfo'])))
    di.set_section_count(2)
    streams['DocInfo'] = hwp5.deflate(hwp5.serialize_records(di.records))
    streams['BodyText/Section0'] = hwp5.deflate(hwp5.serialize_records(body))
    streams['BodyText/Section1'] = hwp5.deflate(hwp5.serialize_records(sec1))
    streams.pop('PrvImage', None)
    first = ['FileHeader', 'DocInfo', 'BodyText/Section0', 'BodyText/Section1']
    hwp5.write_hwp(dst, streams, clsid, order=first + [k for k in streams if k not in first])


if __name__ == '__main__':
    onecol(sys.argv[1], sys.argv[2])
