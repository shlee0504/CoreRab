"""Low-level helpers for HWP 5.x binary documents (record streams, OLE I/O)."""
import struct
import uuid
import zlib

import olefile

from .cfb import build_compound_file

# HWPTAG_BEGIN = 0x10
TAG_DOCUMENT_PROPERTIES = 16
TAG_ID_MAPPINGS = 17
TAG_BIN_DATA = 18
TAG_FACE_NAME = 19
TAG_BORDER_FILL = 20
TAG_CHAR_SHAPE = 21
TAG_TAB_DEF = 22
TAG_NUMBERING = 23
TAG_BULLET = 24
TAG_PARA_SHAPE = 25
TAG_STYLE = 26

TAG_PARA_HEADER = 66
TAG_PARA_TEXT = 67
TAG_PARA_CHAR_SHAPE = 68
TAG_PARA_LINE_SEG = 69
TAG_PARA_RANGE_TAG = 70
TAG_CTRL_HEADER = 71
TAG_LIST_HEADER = 72
TAG_PAGE_DEF = 73
TAG_SHAPE_COMPONENT = 76
TAG_TABLE = 77
TAG_SHAPE_COMPONENT_PICTURE = 85


class Record:
    __slots__ = ('tag', 'level', 'data')

    def __init__(self, tag, level, data):
        self.tag = tag
        self.level = level
        self.data = bytes(data)

    def copy(self, level_delta=0):
        return Record(self.tag, self.level + level_delta, self.data)

    def __repr__(self):
        return 'Record(%d, %d, %d bytes)' % (self.tag, self.level, len(self.data))


def parse_records(buf):
    recs = []
    i, n = 0, len(buf)
    while i < n:
        h, = struct.unpack_from('<I', buf, i)
        i += 4
        tag, level, size = h & 0x3FF, (h >> 10) & 0x3FF, (h >> 20) & 0xFFF
        if size == 0xFFF:
            size, = struct.unpack_from('<I', buf, i)
            i += 4
        recs.append(Record(tag, level, buf[i:i + size]))
        i += size
    return recs


def serialize_records(recs):
    out = bytearray()
    for r in recs:
        size = len(r.data)
        if size >= 0xFFF:
            out += struct.pack('<II', r.tag | (r.level << 10) | (0xFFF << 20), size)
        else:
            out += struct.pack('<I', r.tag | (r.level << 10) | (size << 20))
        out += r.data
    return bytes(out)


def inflate(data):
    return zlib.decompress(data, -15)


def deflate(data):
    c = zlib.compressobj(9, zlib.DEFLATED, -15)
    return c.compress(data) + c.flush()


def load_streams(path):
    """Return (streams, root_clsid) where streams maps 'A/B' -> bytes."""
    ole = olefile.OleFileIO(path)
    streams = {}
    for entry in ole.listdir(streams=True, storages=False):
        name = '/'.join(entry)
        streams[name] = ole.openstream(entry).read()
    clsid = ole.root.clsid
    ole.close()
    # olefile renders the CLSID in GUID text form; convert back to bytes
    raw = uuid.UUID(clsid).bytes_le if clsid else b'\0' * 16
    return streams, raw


def write_hwp(path, streams, root_clsid=b'\0' * 16, order=None):
    """streams: mapping 'A/B' -> bytes. Storages are created implicitly."""
    tree = {}
    names = order if order else list(streams)
    for name in names:
        parts = name.split('/')
        node = tree
        for p in parts[:-1]:
            node = node.setdefault(p, {})
        node[parts[-1]] = streams[name]
    data = build_compound_file(tree, root_clsid)
    with open(path, 'wb') as f:
        f.write(data)
    return len(data)
