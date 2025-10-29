# imghdr.py
# Small shim to provide imghdr.what() detection in case environment
# doesn't expose the stdlib imghdr. This is minimal but enough for
# python-telegram-bot's needs (recognize common image types).

def _read_magic(fp, n):
    try:
        return fp.read(n)
    except:
        return b''

def what(filename, h=None):
    """
    Minimal implementation: if h (header bytes) provided, use that,
    otherwise open the file path and read initial bytes.
    Returns: 'jpeg','png','gif','webp','bmp' or None
    """
    data = h
    if data is None:
        try:
            with open(filename, 'rb') as f:
                data = _read_magic(f, 32)
        except Exception:
            return None

    if not data:
        return None

    # JPEG
    if data.startswith(b'\xff\xd8'):
        return 'jpeg'
    # PNG
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'png'
    # GIF
    if data[:6] in (b'GIF87a', b'GIF89a'):
        return 'gif'
    # WebP (RIFF....WEBP)
    if data.startswith(b'RIFF') and b'WEBP' in data[:16]:
        return 'webp'
    # BMP
    if data.startswith(b'BM'):
        return 'bmp'
    # TIFF
    if data.startswith(b'II') or data.startswith(b'MM'):
        return 'tiff'
    return None

# Expose same API as stdlib
def test(filepath):
    return what(filepath)
