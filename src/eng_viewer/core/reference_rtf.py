"""Bounded plain-text RTF preview. Never loads objects, fields or linked resources."""
from __future__ import annotations

import re

_TOKEN = re.compile(r"\\([a-zA-Z]+)(-?\d+)? ?|\\'([0-9a-fA-F]{2})|\\([^a-zA-Z])|([{}])|([^\\{}]+)")
_SKIP = frozenset({'fonttbl', 'colortbl', 'stylesheet', 'info', 'pict', 'object',
                   'fldinst', 'filetbl', 'datastore', 'themedata', 'xmlnstbl',
                   'header', 'footer', 'annotation'})
_CHAR = {'par': '\n', 'line': '\n', 'tab': '\t', 'cell': '\t', 'row': '\n',
         'emdash': '—', 'endash': '–', 'bullet': '•', 'lquote': '‘', 'rquote': '’',
         'ldblquote': '“', 'rdblquote': '”'}


def rtf_text(data: bytes) -> str:
    if not data.lstrip().startswith(b'{\\rtf'):
        raise ValueError('This file does not contain valid RTF.')
    text = data.decode('latin1')
    stack = []
    skip, uc, fallback, codec = False, 1, 0, 'cp1252'
    output = []
    position = 0
    while position < len(text):
        match = _TOKEN.match(text, position)
        if match is None:
            raise ValueError('Malformed RTF control sequence.')
        position = match.end()
        word, number, byte, symbol, brace, plain = match.groups()
        if brace == '{':
            if len(stack) >= 256:
                raise ValueError('RTF nesting exceeds the preview limit.')
            stack.append((skip, uc, codec))
        elif brace == '}':
            if not stack:
                raise ValueError('Unbalanced RTF groups.')
            skip, uc, codec = stack.pop()
        elif word == 'bin':
            length = int(number or 0)
            if length < 0 or position + length > len(text):
                raise ValueError('Invalid RTF binary length.')
            position += length
        elif word in _SKIP or symbol == '*':
            skip = True
        elif word == 'ansicpg':
            candidate = 'cp' + (number or '1252')
            try:
                b''.decode(candidate)
                codec = candidate
            except LookupError:
                codec = 'cp1252'
        elif word == 'uc':
            uc = max(0, min(16, int(number or 1)))
        elif not skip:
            if word == 'u':
                output.append(chr(int(number or 0) & 0xffff))
                fallback = uc
                continue
            value = (_CHAR.get(word, '') if word else
                     bytes.fromhex(byte).decode(codec, errors='replace') if byte else
                     {'~': '\u00a0', '_': '‑', '-': '', '\\': '\\', '{': '{', '}': '}'}.get(symbol, '') if symbol else
                     plain.replace('\r', '').replace('\n', ''))
            if fallback:
                consumed = min(fallback, len(value))
                fallback -= consumed
                value = value[consumed:]
            output.append(value)
    if stack:
        raise ValueError('Unbalanced RTF groups.')
    # Combine RTF's UTF-16 surrogate pairs, replacing malformed isolated values.
    return ''.join(output).encode('utf-16-le', errors='surrogatepass').decode('utf-16-le', errors='replace')
