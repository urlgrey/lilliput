# Vulnerability: AVIF HDR Tonemap Wrong Stride OOB Read

## Summary

In `avif.cpp`, the function `avif_tonemap_rgb()` receives a `uint16_t*` pixel buffer
from `avifRGBImage` and iterates over pixels using `idx = (y * width + x) * 3`.

However, `avifRGBImage.pixels` is laid out with a row stride of `avifRGBImage.rowBytes`
which **may not equal** `width * channels * sizeof(uint16_t)`. libavif allocates rows
with alignment padding. Using the non-padded index formula causes:

1. **Out-of-bounds read** in `src[idx]` when rowBytes > width * channels * 2
   (reading into padding/next-row alignment bytes, or past the buffer entirely)
2. **Incorrect pixel processing** even when no crash occurs — the extracted R,G,B
   values are wrong because they come from the wrong byte offsets

## Affected File

- `avif.cpp`, function `avif_tonemap_rgb()`, pixel reading loop

## Vulnerability Class

- **CWE-125: Out-of-Bounds Read** (reads past actual pixel row data due to wrong stride)
- **CWE-682: Incorrect Calculation** (wrong index formula ignores rowBytes)

## Root Cause

```cpp
static void avif_tonemap_rgb(uint16_t* src,
                             uint8_t* dst,
                             int width,
                             int height,
                             int src_depth,
                             avifTransferCharacteristics transfer,
                             avifColorPrimaries primaries)
{
    // ...
    for (int y = 0; y < height; y++) {
        for (int x = 0; x < width; x++) {
            int idx = (y * width + x) * 3;  // BUG: ignores rowBytes alignment!
            float r = src[idx] * scale;     // may read OOB
            float g = src[idx + 1] * scale; // may read OOB
            float b = src[idx + 2] * scale; // may read OOB
            // ...
        }
    }
}
```

The correct formula would use the actual row stride:
```cpp
// avifRGBImage stores rowBytes (total bytes per row, including padding)
// rowBytes / sizeof(uint16_t) gives the stride in uint16_t units
int row_stride = rowBytes / sizeof(uint16_t);  // need to pass rowBytes to the function
int idx = y * row_stride + x * 3;
```

## Impact

- **OOB heap read**: In the worst case, reads past the end of the `temp.pixels` buffer
- **Information disclosure**: Values from adjacent heap allocations are processed as HDR
  pixel values and appear in the tone-mapped output image
- **Incorrect rendering**: Even without a crash, pixels are blended with wrong data
- **DoS**: Potential SIGSEGV if the OOB read extends into unmapped memory

## Trigger Conditions

The bug is triggered when:
1. An AVIF image is HDR (BT.2020 primaries or PQ/HLG transfer characteristics)
2. `tone_mapping_enabled` is `true` (passed from the Go caller)
3. `avifRGBImage.rowBytes > avifRGBImage.width * channels * sizeof(uint16_t)`

The third condition holds whenever libavif adds row alignment padding (common behavior).

## Proof of Concept

See `repro/craft_avif_hdr.py` for a test case.

## Mitigation

Pass `rowBytes` to `avif_tonemap_rgb()` and use it in the loop:

```cpp
static void avif_tonemap_rgb(uint16_t* src,
                             uint8_t* dst,
                             int width,
                             int height,
                             int src_depth,
                             uint32_t rowBytes,        // ADD THIS PARAMETER
                             avifTransferCharacteristics transfer,
                             avifColorPrimaries primaries)
{
    // ...
    for (int y = 0; y < height; y++) {
        uint16_t* row = (uint16_t*)((uint8_t*)src + y * rowBytes);  // Use rowBytes
        for (int x = 0; x < width; x++) {
            float r = row[x * 3 + 0] * scale;
            float g = row[x * 3 + 1] * scale;
            float b = row[x * 3 + 2] * scale;
        }
    }
}
```

And update the call site in `avif_convert_yuv_to_rgb_with_tone_mapping()` to pass `temp.rowBytes`.
