# Vulnerability: GIF Palette Index Out-of-Bounds Read

## Summary

In `giflib.cpp`, the function `giflib_decoder_render_frame()` reads palette entries from a
`ColorMapObject` using a `GifByteType` (uint8) index from the compressed pixel data without
validating that the index is within the bounds of the palette.

## Affected File

- `giflib.cpp`, function `giflib_decoder_render_frame()`, rendering loop

## Vulnerability Class

**CWE-125: Out-of-Bounds Read** (potential OOB read leading to info-leak or crash)

## Root Cause

```cpp
for (int x = frame_left; x < frame_left + frame_width; x++) {
    GifByteType palette_index = d->pixels[pixel_index++];
    if (palette_index == transparency_index) {
        dst += 4;
        continue;
    }
    *dst++ = colorMap->Colors[palette_index].Blue;   // <-- NO BOUNDS CHECK
    *dst++ = colorMap->Colors[palette_index].Green;
    *dst++ = colorMap->Colors[palette_index].Red;
    *dst++ = 255;
}
```

`palette_index` is a `GifByteType` (uint8), so it can be 0–255. However, `ColorMapObject` palettes
in GIF are allowed to have fewer than 256 entries (e.g., 2, 4, 8, 16, 32, 64, 128 entries).

The GIF specification allows palette sizes that are powers of 2 from 2 to 256.  A crafted GIF with:
- A palette of only 2 colors (ColorCount = 2)
- Pixel data containing palette index 200

...will cause `colorMap->Colors[200]` to be accessed, reading 6 bytes (`GifColorType` = 3 bytes
RGB × 2 sides) beyond the end of the allocated Colors array.

## Impact

- **Out-of-bounds heap read**: Reads up to 252 × sizeof(GifColorType) = 756 bytes past the palette
- **Information disclosure**: Values from adjacent heap allocations are rendered as pixel colors
- **Potential crash**: If the memory region after the palette is unmapped or guarded
- **CTF relevance**: Can be used to leak heap/process memory via the rendered output image

## Exploitation Steps

1. Craft a GIF with a 2-entry local or global color map (`ColorCount = 2`)
2. Fill the pixel raster data with index values of 128–255
3. Feed the crafted GIF to lilliput's resize/transcode pipeline
4. Inspect the output image pixel colors — they will contain data from adjacent heap memory

The leaked values appear as pixel color components (R, G, B) in the output image.

## Proof of Concept

See `repro/craft_gif_palette_oob.py` for a minimal GIF generator that triggers this condition.

Run:
```
python3 repro/craft_gif_palette_oob.py > poc.gif
# Process poc.gif through lilliput and observe output pixel colors
```

## Mitigation

Add a bounds check before accessing the palette:

```cpp
GifByteType palette_index = d->pixels[pixel_index++];
if (palette_index == transparency_index) {
    dst += 4;
    continue;
}
if (palette_index >= colorMap->ColorCount) {
    // clamp or skip; GIF spec violation
    dst += 4;
    continue;
}
*dst++ = colorMap->Colors[palette_index].Blue;
*dst++ = colorMap->Colors[palette_index].Green;
*dst++ = colorMap->Colors[palette_index].Red;
*dst++ = 255;
```

## References

- GIF89a Specification, Section 19 (Color Table)
- giflib source: `gif_lib.h`, `ColorMapObject.ColorCount`
