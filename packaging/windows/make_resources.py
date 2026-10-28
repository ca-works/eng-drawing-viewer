# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright © 2026 ca-works
"""Write the launcher's Windows resources (icon, version information, manifest) as a .syso file.

    python packaging/windows/make_resources.py ICON.ico VERSION OUTPUT.syso

Go links any ``*.syso`` object in a package folder into the executable. This writes a COFF object
for x64 with one ``.rsrc`` section, so building the launcher needs no resource compiler and no
third-party tool. The output depends only on the inputs (no timestamps).
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
RT_ICON, RT_GROUP_ICON, RT_VERSION, RT_MANIFEST = 3, 14, 16, 24
LANG_EN_US = 0x0409
COMPANY = "ca-works"
PRODUCT = "ENG Drawing Viewer"


def version_tuple(version: str) -> tuple[int, int, int, int]:
    parts = [int(p) for p in version.split(".") if p.isdigit()][:4]
    return tuple(parts + [0] * (4 - len(parts)))  # type: ignore[return-value]


def manifest(version: str) -> bytes:
    text = (HERE / "app.manifest").read_text(encoding="utf-8")
    return text.replace("@VERSION4@", ".".join(map(str, version_tuple(version)))).encode("utf-8")


def icon_resources(ico: bytes) -> tuple[list[bytes], bytes]:
    """Split an .ico file into RT_ICON images and the RT_GROUP_ICON directory that lists them."""
    reserved, kind, count = struct.unpack_from("<HHH", ico, 0)
    if reserved != 0 or kind != 1 or count == 0:
        raise ValueError("not an .ico file")
    images, group = [], struct.pack("<HHH", 0, 1, count)
    for i in range(count):
        w, h, colours, res, planes, bits, size, offset = struct.unpack_from("<BBBBHHII", ico, 6 + 16 * i)
        images.append(ico[offset:offset + size])
        group += struct.pack("<BBBBHHIH", w, h, colours, res, planes, bits, size, i + 1)
    return images, group


def _pad4(data: bytes) -> bytes:
    return data + b"\0" * (-len(data) % 4)


def _block(key: str, value: bytes = b"", value_len: int | None = None, text: bool = False,
           children: list[bytes] = ()) -> bytes:
    """One VS_VERSIONINFO node: length, value length, type, key, value and 32-bit aligned children."""
    body = _pad4(struct.pack("<HHH", 0, 0, 0) + (key + "\0").encode("utf-16-le"))
    if value:
        body += value
    for child in children:
        body = _pad4(body) + child
    vlen = value_len if value_len is not None else len(value)
    return struct.pack("<HHH", len(body), vlen, 1 if text else 0) + body[6:]


def version_info(version: str, description: str, filename: str) -> bytes:
    a, b, c, d = version_tuple(version)
    ms, ls = (a << 16) | b, (c << 16) | d
    fixed = struct.pack("<13I", 0xFEEF04BD, 0x00010000, ms, ls, ms, ls, 0x3F, 0, 0x00040004, 1, 0, 0, 0)
    strings = {
        "CompanyName": COMPANY,
        "FileDescription": description,
        "FileVersion": version,
        "InternalName": Path(filename).stem,
        "LegalCopyright": "Copyright © 2026 ca-works. GNU AGPL v3 or later.",
        "OriginalFilename": filename,
        "ProductName": PRODUCT,
        "ProductVersion": version,
    }
    table = [_block(k, (v + "\0").encode("utf-16-le"), len(v) + 1, text=True) for k, v in strings.items()]
    string_info = _block("StringFileInfo", text=True, children=[_block("040904B0", text=True, children=table)])
    var_info = _block("VarFileInfo", text=True,
                      children=[_block("Translation", struct.pack("<HH", LANG_EN_US, 1200))])
    return _block("VS_VERSION_INFO", fixed, children=[string_info, var_info])


def coff(resources: dict[int, dict[int, bytes]]) -> bytes:
    """A COFF object whose .rsrc section holds a three-level resource tree (type, id, language)."""
    types = sorted(resources)
    # Sizes of the directory tables, laid out first: root, one table per type, one per (type, id).
    def table(n: int) -> int:
        return 16 + 8 * n

    offset = table(len(types))
    type_dirs, name_dirs = {}, {}
    for t in types:
        type_dirs[t] = offset
        offset += table(len(resources[t]))
    for t in types:
        for rid in sorted(resources[t]):
            name_dirs[t, rid] = offset
            offset += table(1)
    entries = {}
    for t in types:
        for rid in sorted(resources[t]):
            entries[t, rid] = offset
            offset += 16
    data_at = {}
    for t in types:
        for rid in sorted(resources[t]):
            offset += -offset % 8
            data_at[t, rid] = offset
            offset += len(resources[t][rid])

    sect = bytearray(offset + (-offset % 8))
    relocations = []

    def put_table(at: int, ids: list[int], targets: list[int], subdir: bool):
        struct.pack_into("<IIHHHH", sect, at, 0, 0, 0, 0, 0, len(ids))
        for i, (rid, target) in enumerate(zip(ids, targets)):
            struct.pack_into("<II", sect, at + 16 + 8 * i, rid, target | (0x80000000 if subdir else 0))

    put_table(0, types, [type_dirs[t] for t in types], True)
    for t in types:
        ids = sorted(resources[t])
        put_table(type_dirs[t], ids, [name_dirs[t, i] for i in ids], True)
        for rid in ids:
            put_table(name_dirs[t, rid], [LANG_EN_US], [entries[t, rid]], False)
            data = resources[t][rid]
            # OffsetToData must be an address in the final image: the linker adds the section's
            # address through an IMAGE_REL_AMD64_ADDR32NB relocation.
            struct.pack_into("<IIII", sect, entries[t, rid], data_at[t, rid], len(data), 0, 0)
            relocations.append(entries[t, rid])
            sect[data_at[t, rid]:data_at[t, rid] + len(data)] = data

    header_size, section_size = 20, 40
    raw_at = header_size + section_size
    reloc_at = raw_at + len(sect)
    symbols_at = reloc_at + 10 * len(relocations)
    out = bytearray()
    out += struct.pack("<HHIIIHH", 0x8664, 1, 0, symbols_at, 1, 0, 0)
    out += struct.pack("<8sIIIIIIHHI", b".rsrc", 0, 0, len(sect), raw_at, reloc_at, 0, len(relocations), 0,
                       0x40000040)  # initialised data, readable
    out += sect
    for r in relocations:
        out += struct.pack("<IIH", r, 0, 3)  # IMAGE_REL_AMD64_ADDR32NB against symbol 0 (the section)
    out += struct.pack("<8sIhHBB", b".rsrc", 0, 1, 0, 3, 0)  # static section symbol
    out += struct.pack("<I", 4)  # empty string table
    return bytes(out)


def main() -> int:
    if len(sys.argv) != 4:
        print(__doc__)
        return 2
    ico, version, output = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
    images, group = icon_resources(ico.read_bytes())
    resources = {
        RT_ICON: {i + 1: img for i, img in enumerate(images)},
        RT_GROUP_ICON: {1: group},
        RT_VERSION: {1: version_info(version, PRODUCT, f"{PRODUCT}.exe")},
        RT_MANIFEST: {1: manifest(version)},
    }
    output.write_bytes(coff(resources))
    print(f"wrote {output} ({output.stat().st_size} bytes, {len(images)} icon images)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
