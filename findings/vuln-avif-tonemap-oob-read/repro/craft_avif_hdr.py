#!/usr/bin/env python3
"""
Reproducer: AVIF HDR tonemap wrong stride / channel mismatch in avif.cpp

Demonstrates the avif_tonemap_rgb() bug where:
  - For BGRA images (alpha channel present), the function uses a 3-channel
    index formula on 4-channel pixel data
  - idx = (y * width + x) * 3  but should be * 4 for BGRA

The output tone-mapped image will have incorrect pixel colors because
each pixel reads from the wrong source channel.

Requirements:
    pip install pillow-avif-plugin  (or use system avifenc)

Manual reproduction without Python:
    # Create a 10-bit HDR AVIF with alpha using avifenc:
    avifenc --depth 10 --cicp 9/16/0 --alpha-premultiplied hdr_rgba.png output.avif
    # Process with lilliput with tone_mapping=true
    # Compare output colors with expected tone-mapped values

Expected vs Actual:
    For a bright red pixel at 10-bit value (1000, 50, 50, 800) [R,G,B,A]:
    Correct read at idx*4:  r=1000, g=50, b=50
    Buggy read at idx*3:    r=1000, g=50, b=50  [row 0, ok]
                            r=???, g=???, b=???   [later rows - reads wrong position]
"""

import sys


def describe_bug():
    print("""
=== AVIF HDR Tonemap Wrong Stride Analysis ===

Bug Location: avif.cpp, avif_tonemap_rgb()

Vulnerable code:
    for (int y = 0; y < height; y++) {
        for (int x = 0; x < width; x++) {
            int idx = (y * width + x) * 3;  // BUG: 3 channels assumed
            float r = src[idx] * scale;
            float g = src[idx + 1] * scale;
            float b = src[idx + 2] * scale;

For BGRA images (4 channels * 2 bytes = 8 bytes/pixel):
    width=4, height=4, format=BGRA (4ch, uint16)
    Pixel buffer layout (indices of uint16_t values):
        Row 0: [B0,G0,R0,A0, B1,G1,R1,A1, B2,G2,R2,A2, B3,G3,R3,A3]
                0   1  2  3   4   5  6  7   8   9 10 11  12  13 14 15
        Row 1: [B0,G0,R0,A0, B1,G1,R1,A1, ...]
                16 17 18 19  20  21 22 23  ...

    For pixel (x=3, y=0) -- last pixel, first row:
        Correct idx = (0 * 4 + 3) * 4 = 12  -> reads B=src[12], G=src[13], R=src[14]  CORRECT
        Bug idx     = (0 * 4 + 3) * 3 = 9   -> reads B=src[9],  G=src[10], R=src[11]
                                               (reads from middle of pixel 2's data)  WRONG

    For pixel (x=0, y=1) -- first pixel, second row:
        Correct idx = (1 * 4 + 0) * 4 = 16  -> reads B=src[16] (actual row 1)  CORRECT
        Bug idx     = (1 * 4 + 0) * 3 = 12  -> reads B=src[12] (still row 0!)  WRONG

This means:
  1. The bug reads from wrong positions in every row except (partially) row 0
  2. For a 100x100 BGRA 16-bit image:
     - Buffer size = 100 * 100 * 4 * 2 = 80,000 bytes (40,000 uint16_t values)
     - Maximum valid index = 39,997 (for last 3 channels of last pixel)
     - Bug maximum index = (99*100+99)*3 = 29,697 -> WITHIN bounds
     - But reads wrong channels -> incorrect tone-mapping -> data from wrong pixels
""")


def generate_test_scenario():
    """Show what the correct vs buggy pixel reads would be for a sample image."""
    width, height = 4, 4
    channels = 4  # BGRA
    
    # Simulate a 4x4 BGRA 16-bit image (values 0-1023 for 10-bit)
    # Format: [B, G, R, A] per pixel
    pixels = []
    for y in range(height):
        for x in range(width):
            B = (x * 100) & 1023
            G = (y * 100) & 1023
            R = ((x + y) * 50) & 1023
            A = 800
            pixels.extend([B, G, R, A])
    
    print("=== Pixel Read Comparison (4x4 BGRA, 10-bit) ===")
    print(f"{'Pixel':10s} {'Correct B,G,R':25s} {'Bug B,G,R':25s} {'Match':6s}")
    print("-" * 70)
    
    mismatch_count = 0
    for y in range(height):
        for x in range(width):
            correct_idx = (y * width + x) * channels  # *4 for BGRA
            bug_idx = (y * width + x) * 3              # *3 (wrong)
            
            correct_b = pixels[correct_idx]
            correct_g = pixels[correct_idx + 1]
            correct_r = pixels[correct_idx + 2]
            
            if bug_idx + 2 < len(pixels):
                bug_b = pixels[bug_idx]
                bug_g = pixels[bug_idx + 1]
                bug_r = pixels[bug_idx + 2]
            else:
                bug_b = bug_g = bug_r = -1  # OOB
            
            match = (correct_b == bug_b and correct_g == bug_g and correct_r == bug_r)
            if not match:
                mismatch_count += 1
            
            print(f"({x:2d},{y:2d})    "
                  f"B={correct_b:4d} G={correct_g:4d} R={correct_r:4d}   "
                  f"B={bug_b:4d} G={bug_g:4d} R={bug_r:4d}   "
                  f"{'OK' if match else 'WRONG'}")
    
    print(f"\nMismatches: {mismatch_count}/{width*height} pixels")
    print(f"Only first pixel is correct (both indices start at 0).")


if __name__ == '__main__':
    describe_bug()
    generate_test_scenario()
    print("\n[+] To reproduce with actual AVIF:")
    print("[+] 1. Create an HDR AVIF with alpha: depth>=10, BT.2020 primaries")
    print("[+] 2. Process with lilliput with tone_mapping enabled")
    print("[+] 3. Compare output to expected tone-mapped colors")
    print("[+] 4. Colors will be shifted/wrong due to 3-channel vs 4-channel stride")
