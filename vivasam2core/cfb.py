"""Minimal writer for OLE2 compound files (MS-CFB, version 3, 512-byte sectors).

HWP 5.x documents are compound files. olefile can read them but cannot create
new streams, so this module builds the container from scratch: header, FAT,
DIFAT, mini stream / mini FAT and a red-black directory tree per storage.
"""
import struct
import time

ENDOFCHAIN = 0xFFFFFFFE
FREESECT = 0xFFFFFFFF
FATSECT = 0xFFFFFFFD
DIFSECT = 0xFFFFFFFC
NOSTREAM = 0xFFFFFFFF

SECTOR = 512
MINI_SECTOR = 64
MINI_CUTOFF = 4096

STORAGE = 1
STREAM = 2
ROOT = 5


def filetime_now():
    return int((time.time() + 11644473600) * 10_000_000)


class Entry:
    def __init__(self, name, kind, data=b'', clsid=b'\0' * 16, ctime=0, mtime=0):
        if len(name) > 31:
            raise ValueError('entry name too long: %r' % name)
        self.name = name
        self.kind = kind
        self.data = data
        self.clsid = clsid
        self.ctime = ctime
        self.mtime = mtime
        self.children = []
        self.left = self.right = self.child = NOSTREAM
        self.black = True
        self.sid = None
        self.start = 0
        self.size = 0


def _key(name):
    # MS-CFB 2.6.4: shorter names first, then upper-cased UTF-16 comparison
    return (len(name), name.upper())


def _build_tree(nodes):
    """Balanced BST over sorted siblings; deepest level red when not perfect."""
    nodes = sorted(nodes, key=lambda e: _key(e.name))
    depth = {}

    def build(lo, hi, d):
        if lo >= hi:
            return None
        mid = (lo + hi) // 2
        node = nodes[mid]
        depth[id(node)] = d
        left = build(lo, mid, d + 1)
        right = build(mid + 1, hi, d + 1)
        node.left = left.sid if left else NOSTREAM
        node.right = right.sid if right else NOSTREAM
        node._l, node._r = left, right
        return node

    root = build(0, len(nodes), 1)
    if root is None:
        return NOSTREAM
    # depth of nodes owning a null link
    null_depths = [depth[id(n)] for n in nodes if n._l is None or n._r is None]
    lo_d, hi_d = min(null_depths), max(null_depths)
    assert hi_d - lo_d <= 1, 'tree not balanced enough for colouring'
    for n in nodes:
        n.black = not (hi_d != lo_d and depth[id(n)] == hi_d)
    _check_rb(root)
    return root.sid


def _check_rb(node):
    def walk(n, parent_red):
        if n is None:
            return 1
        if parent_red and not n.black:
            raise AssertionError('red node with red parent')
        lh = walk(n._l, not n.black)
        rh = walk(n._r, not n.black)
        if lh != rh:
            raise AssertionError('unequal black height')
        return lh + (1 if n.black else 0)
    walk(node, False)


def build_compound_file(tree, root_clsid=b'\0' * 16):
    """tree: dict name -> bytes (stream) or dict (storage). Returns file bytes."""
    now = filetime_now()
    root = Entry('Root Entry', ROOT, clsid=root_clsid, ctime=0, mtime=now)
    entries = [root]

    def add(parent, mapping):
        for name, value in mapping.items():
            if isinstance(value, dict):
                e = Entry(name, STORAGE, ctime=now, mtime=now)
                e.sid = len(entries)
                entries.append(e)
                parent.children.append(e)
                add(e, value)
            else:
                e = Entry(name, STREAM, data=bytes(value))
                e.sid = len(entries)
                entries.append(e)
                parent.children.append(e)

    root.sid = 0
    add(root, tree)
    for e in entries:
        if e.kind in (ROOT, STORAGE):
            e.child = _build_tree(e.children)

    # --- mini stream for small streams
    mini = bytearray()
    minifat = []
    for e in entries:
        if e.kind != STREAM:
            continue
        e.size = len(e.data)
        if e.size < MINI_CUTOFF:
            if e.size == 0:
                e.start = ENDOFCHAIN
                continue
            first = len(mini) // MINI_SECTOR
            n = (e.size + MINI_SECTOR - 1) // MINI_SECTOR
            mini += e.data + b'\0' * (n * MINI_SECTOR - e.size)
            for i in range(n):
                minifat.append(first + i + 1 if i < n - 1 else ENDOFCHAIN)
            e.start = first
            e.small = True
        else:
            e.small = False

    # --- regular sector allocation
    fat = []
    sectors = []  # list of bytes chunks, each SECTOR long

    def alloc_chain(data):
        n = (len(data) + SECTOR - 1) // SECTOR
        if n == 0:
            return ENDOFCHAIN
        first = len(sectors)
        padded = data + b'\0' * (n * SECTOR - len(data))
        for i in range(n):
            sectors.append(padded[i * SECTOR:(i + 1) * SECTOR])
            fat.append(first + i + 1 if i < n - 1 else ENDOFCHAIN)
        return first

    for e in entries:
        if e.kind == STREAM and not e.small:
            e.start = alloc_chain(e.data)
    root.size = len(mini)
    root.start = alloc_chain(bytes(mini)) if mini else ENDOFCHAIN

    minifat_bytes = b''.join(struct.pack('<I', v) for v in minifat)
    if minifat_bytes:
        pad = (-len(minifat_bytes)) % SECTOR
        minifat_bytes += struct.pack('<I', FREESECT) * (pad // 4)
    minifat_start = alloc_chain(minifat_bytes) if minifat_bytes else ENDOFCHAIN
    n_minifat = len(minifat_bytes) // SECTOR

    # directory
    dir_bytes = bytearray()
    for e in entries:
        name16 = e.name.encode('utf-16-le') + b'\0\0'
        dir_bytes += name16.ljust(64, b'\0')
        dir_bytes += struct.pack('<HBB', len(name16), e.kind, 1 if e.black else 0)
        dir_bytes += struct.pack('<III', e.left, e.right, e.child)
        dir_bytes += e.clsid
        dir_bytes += struct.pack('<I', 0)
        dir_bytes += struct.pack('<QQ', e.ctime, e.mtime)
        start = e.start if e.kind in (STREAM, ROOT) else 0
        size = e.size if e.kind in (STREAM, ROOT) else 0
        dir_bytes += struct.pack('<IQ', start, size)
    while len(dir_bytes) % SECTOR:
        # unused directory slots
        dir_bytes += b'\0' * 64 + struct.pack('<HBB', 0, 0, 0) + struct.pack('<III', NOSTREAM, NOSTREAM, NOSTREAM) + b'\0' * 36 + struct.pack('<IQ', 0, 0)
    dir_start = alloc_chain(bytes(dir_bytes))

    # FAT + DIFAT sizing
    n_other = len(sectors)
    n_fat = n_difat = 0
    while True:
        total = n_other + n_fat + n_difat
        need_fat = (total + 127) // 128
        need_difat = max(0, (need_fat - 109 + 126) // 127)
        if need_fat == n_fat and need_difat == n_difat:
            break
        n_fat, n_difat = need_fat, need_difat
    fat_start = len(sectors)
    fat_secs = list(range(fat_start, fat_start + n_fat))
    difat_secs = list(range(fat_start + n_fat, fat_start + n_fat + n_difat))
    fat += [FATSECT] * n_fat + [DIFSECT] * n_difat
    fat += [FREESECT] * (n_fat * 128 - len(fat))
    fat_bytes = b''.join(struct.pack('<I', v) for v in fat)
    for i in range(n_fat):
        sectors.append(fat_bytes[i * SECTOR:(i + 1) * SECTOR])
    # DIFAT sectors hold FAT sector ids beyond the first 109
    rest = fat_secs[109:]
    for i, s in enumerate(difat_secs):
        chunk = rest[i * 127:(i + 1) * 127]
        vals = chunk + [FREESECT] * (127 - len(chunk))
        nxt = difat_secs[i + 1] if i + 1 < len(difat_secs) else ENDOFCHAIN
        sectors.append(b''.join(struct.pack('<I', v) for v in vals) + struct.pack('<I', nxt))

    header = bytearray()
    header += b'\xD0\xCF\x11\xE0\xA1\xB1\x1A\xE1'
    header += b'\0' * 16
    header += struct.pack('<HHHHH', 0x003E, 0x0003, 0xFFFE, 9, 6)
    header += b'\0' * 6
    header += struct.pack('<I', 0)          # directory sectors (v3: 0)
    header += struct.pack('<I', n_fat)
    header += struct.pack('<I', dir_start)
    header += struct.pack('<I', 0)          # transaction signature
    header += struct.pack('<I', MINI_CUTOFF)
    header += struct.pack('<I', minifat_start)
    header += struct.pack('<I', n_minifat)
    header += struct.pack('<I', difat_secs[0] if difat_secs else ENDOFCHAIN)
    header += struct.pack('<I', n_difat)
    first109 = fat_secs[:109] + [FREESECT] * (109 - min(109, len(fat_secs)))
    header += b''.join(struct.pack('<I', v) for v in first109)
    assert len(header) == SECTOR
    return bytes(header) + b''.join(sectors)
