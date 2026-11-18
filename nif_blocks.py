"""Minimal FO4 (BS stream 130) NIF block editor: read every block, edit node fields, delete blocks with every
link renumbered, replace block bytes, write the file back. It knows the block types of statics, plants and
buildings; any other type makes delete() refuse (ValueError) instead of guessing, so callers can leave the file
alone. Used by nif_roundtrip.patch_restore_shared_collision (2026-11-16)."""
import struct

NODE_TYPES = ("NiNode", "BSFadeNode", "BSLeafAnimNode", "BSOrderedNode", "BSMultiBoundNode",
              # added 2026-11-17 (GSFungusTree04): a NiNode plus switch flags (u16) and index (u32) after the children,
              # which node() keeps in 'rest'; its block links are the NiNode ones
              "NiSwitchNode")
SHAPE_TYPES = ("BSTriShape", "BSMeshLODTriShape", "BSSubIndexTriShape", "BSDynamicTriShape")


class Nif:
    def __init__(self, data: bytes):
        o = data.index(b'\n') + 1
        self.line = data[:o]
        self.ver, self.endian, self.user, nb = struct.unpack_from('<IBII', data, o); o += 13
        self.bsver = struct.unpack_from('<I', data, o)[0]; o += 4
        s0 = o
        for _ in range(4 if self.bsver >= 103 else 3):
            o += 1 + data[o]
        self.export_info = data[s0:o]
        nt = struct.unpack_from('<H', data, o)[0]; o += 2
        self.types = []
        for _ in range(nt):
            n = struct.unpack_from('<I', data, o)[0]; self.types.append(data[o + 4:o + 4 + n].decode('latin1')); o += 4 + n
        tidx = struct.unpack_from(f'<{nb}H', data, o); o += 2 * nb
        sizes = struct.unpack_from(f'<{nb}I', data, o); o += 4 * nb
        s0 = o
        ns = struct.unpack_from('<I', data, o)[0]; o += 8
        for _ in range(ns):
            o += 4 + struct.unpack_from('<I', data, o)[0]
        ng = struct.unpack_from('<I', data, o)[0]; o += 4 + 4 * ng
        self.strings_and_groups = data[s0:o]
        self.blocks = []
        for t, sz in zip(tidx, sizes):
            self.blocks.append([self.types[t], bytearray(data[o:o + sz])]); o += sz
        nr = struct.unpack_from('<I', data, o)[0]
        self.roots = list(struct.unpack_from(f'<{nr}i', data, o + 4))
        self.tail = data[o + 4 + 4 * nr:]

    # ---- string table
    def strings(self):
        d = self.strings_and_groups; ns = struct.unpack_from('<I', d, 0)[0]; o = 8; out = []
        for _ in range(ns):
            n = struct.unpack_from('<I', d, o)[0]; out.append(d[o + 4:o + 4 + n].decode('latin1')); o += 4 + n
        return out

    def name(self, i):
        return self.strings()[struct.unpack_from('<i', self.blocks[i][1], 0 if self.blocks[i][0] != 'BSLightingShaderProperty' else 4)[0]]

    # ---- node fields
    def node(self, i):
        d = self.blocks[i][1]
        ne = struct.unpack_from('<I', d, 4)[0]
        extras = list(struct.unpack_from(f'<{ne}i', d, 8))
        q = 8 + 4 * ne
        ctrl, flags = struct.unpack_from('<iI', d, q)
        xf = bytes(d[q + 8:q + 60])
        coll = struct.unpack_from('<i', d, q + 60)[0]
        nc = struct.unpack_from('<I', d, q + 64)[0]
        kids = list(struct.unpack_from(f'<{nc}i', d, q + 68))
        rest = bytes(d[q + 68 + 4 * nc:])
        return dict(name=struct.unpack_from('<i', d, 0)[0], extras=extras, ctrl=ctrl, flags=flags, xf=xf,
                    coll=coll, kids=kids, rest=rest)

    def set_node(self, i, f):
        b = struct.pack('<iI', f['name'], len(f['extras'])) + struct.pack(f"<{len(f['extras'])}i", *f['extras'])
        b += struct.pack('<iI', f['ctrl'], f['flags']) + f['xf'] + struct.pack('<iI', f['coll'], len(f['kids']))
        b += struct.pack(f"<{len(f['kids'])}i", *f['kids']) + f['rest']
        self.blocks[i][1] = bytearray(b)

    def set_type(self, i, t):
        if t not in self.types:
            self.types.append(t)
        self.blocks[i][0] = t

    # ---- refs per block: list of (offset, kind) kind 'ref' i32
    def ref_offsets(self, i):
        t, d = self.blocks[i]
        offs = []
        if t in NODE_TYPES:
            ne = struct.unpack_from('<I', d, 4)[0]
            offs += [8 + 4 * k for k in range(ne)]
            q = 8 + 4 * ne
            offs.append(q)                       # controller
            offs.append(q + 60)                  # collision
            nc = struct.unpack_from('<I', d, q + 64)[0]
            offs += [q + 68 + 4 * k for k in range(nc)]
        elif t in SHAPE_TYPES:
            ne = struct.unpack_from('<I', d, 4)[0]
            offs += [8 + 4 * k for k in range(ne)]
            q = 8 + 4 * ne
            offs.append(q)                       # controller
            offs.append(q + 60)                  # collision
            p = q + 64 + 16                      # bounds
            offs += [p, p + 4, p + 8]            # skin, shader, alpha
        elif t == 'BSLightingShaderProperty':
            ne = struct.unpack_from('<I', d, 8)[0]
            offs += [12 + 4 * k for k in range(ne)]
            q = 12 + 4 * ne
            offs.append(q)                       # controller
            offs.append(q + 4 + 8 + 8 + 8)       # texture set (after flags1/2, uv offset, uv scale)
        elif t in ('NiAlphaProperty', 'BSEffectShaderProperty'):
            # name, extra data list, controller; FO4 effect shaders hold their
            # textures as strings, so no other block links
            ne = struct.unpack_from('<I', d, 4)[0]
            offs += [8 + 4 * k for k in range(ne)]
            offs.append(8 + 4 * ne)
        elif t == 'bhkNPCollisionObject':
            offs += [0, 6]                       # target, data
        elif t in ('BSXFlags', 'BSShaderTextureSet', 'bhkPhysicsSystem',
                   # extra data that holds only strings and numbers, no block links
                   'BSConnectPoint::Parents', 'BSConnectPoint::Children', 'NiStringExtraData',
                   'NiIntegerExtraData', 'BSBound', 'BSInvMarker'):
            pass
        else:
            raise ValueError(f'block type {t} not handled')
        return offs

    def delete(self, kill):
        kill = sorted(set(kill))
        new_index = {}
        k = 0
        for i in range(len(self.blocks)):
            if i in kill:
                continue
            new_index[i] = k; k += 1
        def remap(r):
            if r < 0:
                return r
            if r in kill:
                return -1
            return new_index[r]
        for i in range(len(self.blocks)):
            if i in kill:
                continue
            d = self.blocks[i][1]
            for off in self.ref_offsets(i):
                r = struct.unpack_from('<i', d, off)[0]
                struct.pack_into('<i', d, off, remap(r))
        # nodes: drop -1 entries from extras/children lists
        self.blocks = [b for i, b in enumerate(self.blocks) if i not in kill]
        for i, (t, d) in enumerate(self.blocks):
            if t in NODE_TYPES:
                f = self.node(i)
                f['extras'] = [e for e in f['extras'] if e >= 0]
                f['kids'] = [c for c in f['kids'] if c >= 0]
                self.set_node(i, f)
        self.roots = [remap(r) for r in self.roots]

    def to_bytes(self):
        used = sorted({t for t, _ in self.blocks}, key=lambda t: self.types.index(t))
        types = [t for t in self.types if t in used]
        out = bytearray(self.line)
        out += struct.pack('<IBII', self.ver, self.endian, self.user, len(self.blocks))
        out += struct.pack('<I', self.bsver) + self.export_info
        out += struct.pack('<H', len(types))
        for t in types:
            out += struct.pack('<I', len(t)) + t.encode('latin1')
        out += struct.pack(f'<{len(self.blocks)}H', *[types.index(t) for t, _ in self.blocks])
        out += struct.pack(f'<{len(self.blocks)}I', *[len(d) for _, d in self.blocks])
        out += self.strings_and_groups
        for _, d in self.blocks:
            out += d
        out += struct.pack('<I', len(self.roots)) + struct.pack(f'<{len(self.roots)}i', *self.roots) + self.tail
        return bytes(out)

    # ---- strings and appending blocks from another file
    def _parse_strings(self):
        d = self.strings_and_groups
        ns, maxlen = struct.unpack_from('<II', d, 0); o = 8; out = []
        for _ in range(ns):
            n = struct.unpack_from('<I', d, o)[0]; out.append(d[o + 4:o + 4 + n].decode('latin1')); o += 4 + n
        return out, d[o:]

    def add_string(self, s):
        lst, groups = self._parse_strings()
        if s in lst:
            return lst.index(s)
        lst.append(s)
        b = struct.pack('<II', len(lst), max(len(x) for x in lst))
        for x in lst:
            b += struct.pack('<I', len(x)) + x.encode('latin1')
        self.strings_and_groups = b + groups
        return len(lst) - 1

    STRING_AT = {'BSTriShape': [0], 'BSMeshLODTriShape': [0], 'NiAlphaProperty': [0], 'NiNode': [0],
                 'BSLeafAnimNode': [0], 'BSLightingShaderProperty': [4]}

    def append_from(self, src, indices):
        """Copy blocks *indices* of Nif *src* to the end; refs among them are remapped, refs to
        anything else become -1, string indices are re-added. Returns {src index: new index}."""
        base = len(self.blocks)
        m = {s: base + k for k, s in enumerate(indices)}
        sstr = src.strings()
        for s in indices:
            t, d = src.blocks[s][0], bytearray(src.blocks[s][1])
            for off in src.ref_offsets(s):
                r = struct.unpack_from('<i', d, off)[0]
                struct.pack_into('<i', d, off, m.get(r, -1))
            for off in self.STRING_AT.get(t, []):
                si = struct.unpack_from('<i', d, off)[0]
                struct.pack_into('<i', d, off, self.add_string(sstr[si]) if si >= 0 else -1)
            if t not in self.types:
                self.types.append(t)
            self.blocks.append([t, d])
        return m
