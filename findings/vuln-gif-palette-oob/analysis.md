# Technical Analysis: GIF Palette OOB Read

## Code Location

`giflib.cpp:giflib_decoder_render_frame()` — the inner pixel rendering loop

## Detailed Analysis

### The Bug

```cpp
// giflib.cpp lines ~310-330 (render loop)
for (int y = frame_top; y < frame_top + frame_height; y++) {
    pixel_index += skip_left;
    uint8_t* dst = cvMat->data + y * cvMat->step + (frame_left * 4);
    for (int x = frame_left; x < frame_left + frame_width; x++) {
        GifByteType palette_index = d->pixels[pixel_index++];
        if (palette_index == transparency_index) {
            dst += 4;
            continue;
        }
        // *** BUG: no check that palette_index < colorMap->ColorCount ***
        *dst++ = colorMap->Colors[palette_index].Blue;
        *dst++ = colorMap->Colors[palette_index].Green;
        *dst++ = colorMap->Colors[palette_index].Red;
        *dst++ = 255;
    }
    pixel_index += skip_right;
}
```

### Type Analysis

- `GifByteType` is `uint8_t` → range 0–255
- `colorMap->ColorCount` is `int` → can be 2, 4, 8, 16, 32, 64, 128, or 256
- `colorMap->Colors` is `GifColorType*` where `GifColorType = {uint8_t Red, Green, Blue}` (3 bytes)

### Maximum Overread

For a minimal palette (ColorCount=2), index 255 reads:
- Offset: `255 * sizeof(GifColorType)` = `255 * 3` = **765 bytes into the palette array**
- But only 2 entries were allocated: `2 * 3` = 6 bytes
- Maximum overread: `765 - 6 = 759 bytes` beyond end of allocation

### Giflib Allocation

`GifColorType* Colors` is allocated by giflib's `GifMakeMapObject()`:
```c
ColorMap = (ColorMapObject *)malloc(sizeof(ColorMapObject));
ColorMap->Colors = (GifColorType *)malloc(ColorCount * 3);
```

So for a 2-entry palette, `Colors` points to a **6-byte** malloc allocation.
Reading index 200 accesses byte offset 600 into this 6-byte block.

### Heap Layout Considerations

On modern allocators (jemalloc, tcmalloc, glibc malloc):
- The 6-byte palette allocation will be in a small-object bin (likely 8 or 16 bytes)
- Adjacent allocations in the same bin contain other giflib parsing structures
- The overread will expose metadata from adjacent allocations

### Information Disclosure Potential

The OOB-read values are written directly into the output image pixel buffer as
R, G, B values. When lilliput returns the processed image (e.g., PNG or JPEG output),
the leaked memory is present as pixel color values in the output.

An attacker who can:
1. Submit a crafted GIF to lilliput
2. Receive the output image
...can decode pixel colors to reconstruct up to 759 bytes of heap memory per frame.

### Secondary Effect: Encoder OOB in giflib_encoder_render_frame

The same class of bug exists in the encoder's palette distance calculation:
```cpp
int dist = rgb_distance(R_compare, G_compare, B_compare,
                        color_map->Colors[i].Red,    // i goes from 0 to ColorCount-1
                        color_map->Colors[i].Green,
                        color_map->Colors[i].Blue);
```
This is safe, but the `palette_lookup` then stores indices 0–ColorCount-1 which
are then written as GIF pixel indices. If a frame color map has 2 colors, the
output pixel indices will be 0 or 1 — correct. The encoder side is not directly
vulnerable here.

## Severity

- **CVSS v3.1 Base Score**: ~7.5 (High) — AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:N/A:L
- Remotely exploitable (just upload a crafted GIF)
- No authentication required in typical Discord CDN / image proxy use
