# PDF content editor implementation

The Edit PDF workspace edits native PDF content through the existing render
worker pool. Qt never invokes PyMuPDF. Its viewport rerenders the PDF at the
current zoom and device-pixel ratio, with a 20-million-pixel render ceiling.

Opening a PDF creates a private temporary source copy. Every operation replays a
validated command journal against that copy. Only a successful worker result
becomes the current revision. Undo/redo replays prior journals. Save As writes a
new PDF atomically, rejects source-file aliases, and asks before replacing an
existing output. Closing discards private temporary revisions.

## Implemented behavior

- Inspect individual text spans, image instances, PDF annotations and form
  fields, with explicit entity distinctions.
- Replace or move an isolated horizontal original text span using a chosen
  standard PDF font; delete an isolated span using actual text redaction.
- Preserve images and vector graphics under a text replacement. There is no
  white rectangle hiding the original text.
- Add searchable vector text, rectangles, ellipses, lines, images and separate
  PDF note annotations. Added objects can be changed or removed through the
  session journal, including numeric position and size changes.
- Remove a selected PDF annotation without deleting form widgets.
- Redact a region's text, image pixels and touching vector drawings, with a
  clear confirmation and session undo. The saved PDF is fully rewritten with
  garbage collection; this is not a general metadata/attachment sanitization.
- Rotate and crop pages without flattening their vector contents.
- Undo/redo edits and save a new searchable PDF. Existing PDF Tools remains the
  broader page-organizing workspace.

## Reliability boundaries

Original PDF content does not have the editable object structure of a design
document. This implementation rejects overlapping and transformed original text
spans instead of guessing which operators to rewrite. It explicitly displays
the original font and selected fallback. Built-in fonts currently accept Latin-1
replacement text only. Embedded-font reconstruction, arbitrary original vector
path editing, original-image instance replacement and interactive form editing
are not implemented. Images and forms are labeled inspect-only. New images can
be moved/resized through their session command.

PDFs containing signature fields are not edited. Pages with pending redaction
annotations reject further redaction-based text edits, so existing pending
redactions cannot be silently applied. Scanned text is not represented as
editable original text; use the existing OCR tools deliberately.

The command journal lives for the editing session. Saved native text/shapes can
be reopened as PDF content, but the exact session command history is not saved
inside the PDF. The implementation limits each editing session to 500 commands
and input PDFs to 512 MiB. Long replay histories can take longer than incremental
editing. Rendering remains process-isolated. A Windows GUI run has not been
performed on this Mac.

## Validation

`tests/test_pdf_edit.py` uses synthetic PDFs and images. Tests cover text
replacement and removal, searchability, retained vectors and images, actual
stream removal, source immutability, region redaction, added native objects,
annotation/form separation, rotation/cropping, invalid-operation rejection,
source aliases, overwrite protection, worker dispatch, and native-widget
undo/redo. This is focused regression coverage, not an assertion that every PDF
producer's object structures are editable.

Primary API reference: [PyMuPDF Page API](https://pymupdf.readthedocs.io/en/latest/page.html)
documents unrotated editing coordinates, text insertion and redaction behavior.
