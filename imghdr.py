# Temporary shim for Python 3.13+ (since stdlib imghdr was removed)
import struct

def what(file, h=None):
    if h is None:
        if isinstance(file, (bytes, bytearray)):
            h = file
        else:
            with open(file, 'rb') as f:
                h = f.read(32)

    for kind, test in _tests:
        res = test(h)
        if res:
            return kind
    return None


def test_jpeg(h):
    return h[:3] == b'\xff\xd8\xff' and h[6:10] in (b'JFIF', b'Exif')


def test_png(h):
    return h[:8] == b'\x89PNG\r\n\x1a\n'


def test_gif(h):
    return h[:6] in (b'GIF87a', b'GIF89a')


def test_tiff(h):
    return h[:2] in (b'MM', b'II')


def test_bmp(h):
    return h[:2] == b'BM'


_tests = [
    ('jpeg', test_jpeg),
    ('png', test_png),
    ('gif', test_gif),
    ('tiff', test_tiff),
    ('bmp', test_bmp),
]
