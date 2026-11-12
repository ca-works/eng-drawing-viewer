"""Encoding and delimiter rules for local text references and table handoff."""
import codecs

MAX_TEXT_BYTES = 24 * 1024 * 1024
TABLE_EXTS = {".csv": ",", ".tsv": "\t"}


def decode_text(data):
    for markers, codec in (((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE), "utf-32"),
                           ((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE), "utf-16")):
        if data.startswith(markers):
            return data.decode(codec)
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("cp1252", errors="replace")
    if "\x00" in text:
        raise ValueError("This appears to be binary data or text without a recognized encoding.")
    return text
