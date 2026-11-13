"""Create private, passive OOXML copies for local reference conversion."""
from pathlib import Path
import zipfile
from lxml import etree as ET

MAX_UNPACKED = 256 * 1024 * 1024


def passive_office_copy(source: Path, directory: Path) -> Path:
    """Drop external relationships, embedded objects and dynamic Word fields.
    Spreadsheet formulas are replaced by their stored cached results so preview
    cannot evaluate WEBSERVICE, DDE or other external-content functions.

    No extraction is performed. The original document is never modified. This is
    defense in depth alongside LibreOffice's disabled macros/active content/links;
    it is not a sandbox for vulnerabilities in the installed converter.
    """
    target = directory / ('Reference' + source.suffix.lower())
    try:
        with zipfile.ZipFile(source) as src, zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as dst:
            entries = src.infolist()
            if len(entries) > 20000 or sum(e.file_size for e in entries) > MAX_UNPACKED:
                raise ValueError('Office document exceeds the reference preview limit.')
            seen = set()
            for entry in entries:
                name = entry.filename
                lower = name.casefold()
                if name in seen or name.startswith('/') or '..' in name.split('/') or '\\' in name:
                    raise ValueError('Invalid Office document entry name.')
                seen.add(name)
                if any(part in lower for part in ('vbaproject', '/embeddings/', '/activex/', '/externallinks/')):
                    continue
                data = src.read(entry)
                if lower.endswith(('.xml', '.rels')):
                    if b'<!DOCTYPE' in data.upper() or b'<!ENTITY' in data.upper():
                        raise ValueError('Office preview does not support XML document declarations.')
                    tree = ET.fromstring(data, ET.XMLParser(resolve_entities=False, no_network=True, remove_comments=True, remove_pis=True))
                    if tree.getroottree().docinfo.doctype:
                        raise ValueError("Office preview does not support XML document declarations.")
                    changed = False
                    for parent in tree.iter():
                        for child in list(parent):
                            local = child.tag.rsplit('}', 1)[-1]
                            if (local == 'Relationship' and (child.get('TargetMode', '').lower() == 'external'
                                or any(x in child.get('Type', '').lower() for x in ('oleobject', 'vbaproject', 'activex', 'externallink')))
                                or local in ('instrText', 'fldChar', 'externalReferences', 'oleObject')
                                or local in ('f', 'definedNames', 'altChunk', 'connection', 'queryTable')):
                                parent.remove(child)
                                changed = True
                        if parent.tag.rsplit('}', 1)[-1] == 'fldSimple':
                            for key in list(parent.attrib):
                                if key.rsplit('}', 1)[-1] == 'instr':
                                    del parent.attrib[key]
                                    changed = True
                    if changed:
                        data = ET.tostring(tree, encoding='utf-8', xml_declaration=True)
                dst.writestr(name, data)
    except (zipfile.BadZipFile, ET.XMLSyntaxError, RuntimeError) as exc:
        raise ValueError('This Office document cannot be safely previewed.') from exc
    return target
