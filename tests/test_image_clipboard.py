import io
import struct
import sys

import pytest
from PIL import Image

from eng_viewer.core.image_clipboard import prepare_clipboard, _dibv5, write_clipboard


def test_exact_formats_alpha_and_source_unchanged():
    source = Image.new('RGBA', (20, 10), (50, 100, 150, 110))
    original = source.tobytes()
    for fmt in ('PNG', 'JPEG', 'TIFF', 'BMP'):
        result = prepare_clipboard(source, {'format': fmt, 'background': '#ff0000'})
        with Image.open(io.BytesIO(result.data)) as decoded:
            assert decoded.format == fmt
            assert decoded.size == source.size
            if fmt in ('PNG', 'TIFF'):
                assert decoded.convert('RGBA').getpixel((0, 0))[3] == 110
            else:
                assert decoded.convert('RGBA').getpixel((0, 0))[3] == 255
        assert source.tobytes() == original


def test_jpeg_user_background_and_selected_region():
    source = Image.new('RGBA', (20, 10), (0, 0, 0, 0))
    mask = Image.new('L', source.size)
    mask.paste(255, (3, 2, 13, 8))
    payload = prepare_clipboard(source, {'format': 'JPEG', 'quality': 100, 'background': '#00ff00',
                                        'region': 'selection', 'scale': 200}, mask)
    with Image.open(io.BytesIO(payload.data)) as output:
        assert output.size == (20, 12)
        assert output.getpixel((5, 5))[1] >= 250
    with pytest.raises(ValueError, match='selection'):
        prepare_clipboard(source, {'region': 'selection'})


def test_native_windows_alpha_bitmap_header():
    image = Image.new('RGBA', (2, 3), (1, 2, 3, 128))
    data = _dibv5(image)
    assert struct.unpack_from('<Iii', data) == (124, 2, -3)
    assert struct.unpack_from('<IIII', data, 40) == (0x00FF0000, 0x0000FF00, 0x000000FF, 0xFF000000)
    assert data[124:128] == bytes([3, 2, 1, 128])


@pytest.mark.skipif(sys.platform != 'darwin', reason='macOS native pasteboard')
def test_macos_actual_selected_flavor(qapp):
    from AppKit import NSPasteboard, NSImage
    pb = NSPasteboard.generalPasteboard()
    if pb is None:
        pytest.skip('native pasteboard unavailable in restricted session')
    for fmt, uti in [('PNG', 'public.png'), ('JPEG', 'public.jpeg'), ('TIFF', 'public.tiff'), ('BMP', 'com.microsoft.bmp')]:
        payload = prepare_clipboard(Image.new('RGBA', (12, 9), (45, 90, 180, 100)), {'format': fmt})
        write_clipboard(payload)
        assert bytes(pb.dataForType_(uti)) == payload.data
        assert pb.dataForType_('public.png') is not None
        assert pb.dataForType_('public.tiff') is not None
        native_image = NSImage.alloc().initWithPasteboard_(pb)
        assert native_image is not None
        assert native_image.representations()[0].pixelsWide() == 12
        # Qt's offscreen platform deliberately has a private, in-memory clipboard.
        if qapp.platformName() == 'cocoa':
            qapp.processEvents()
            assert not qapp.clipboard().image().isNull()
