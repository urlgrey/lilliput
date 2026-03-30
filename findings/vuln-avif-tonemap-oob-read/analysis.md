# Technical Analysis: AVIF HDR Tonemap Wrong Stride OOB

## Code Path

### avif_convert_yuv_to_rgb_with_tone_mapping (avif.cpp)

```cpp
static avifResult avif_convert_yuv_to_rgb_with_tone_mapping(avifImage* image,
                                                            avifRGBImage* rgb,
                                                            bool enable_tone_mapping)
{
    if (!enable_tone_mapping || !avif_is_hdr_source(image)) {
        return avifImageYUVToRGB(image, rgb);
    }

    // Allocate temp buffer for high-bit-depth RGB
    avifRGBImage temp;
    avifRGBImageSetDefaults(&temp, image);
    temp.depth = image->depth;        // e.g., 10 or 12 bits
    temp.format = rgb->format;        // e.g., AVIF_RGB_FORMAT_BGR

    avifRGBImageAllocatePixels(&temp);  // libavif allocates with alignment!
    avifImageYUVToRGB(image, &temp);   // fills temp.pixels with aligned rows

    // ...

    avif_tonemap_rgb(
        (uint16_t*)temp.pixels,  // <-- passed as uint16_t*
        rgb->pixels,
        image->width,
        image->height,
        temp.depth,
        // NOTE: temp.rowBytes is NOT passed!
        transferCharacteristics,
        colorPrimaries
    );
```

### avif_tonemap_rgb (avif.cpp)

```cpp
static void avif_tonemap_rgb(uint16_t* src,
                             uint8_t* dst,
                             int width,
                             int height,
                             int src_depth,
                             // rowBytes MISSING from signature
                             avifTransferCharacteristics transfer,
                             avifColorPrimaries primaries)
{
    float scale = 1.0f / ((1 << src_depth) - 1);
    // ...
    for (int y = 0; y < height; y++) {
        for (int x = 0; x < width; x++) {
            int idx = (y * width + x) * 3;  // WRONG: assumes no padding
            float r = src[idx] * scale;
            float g = src[idx + 1] * scale;
            float b = src[idx + 2] * scale;
```

## libavif Row Alignment

`avifRGBImageAllocatePixels` calls `avifAlloc`:
```c
// libavif/src/avif.c
uint32_t rowBytes = image->width * avifRGBImagePixelSize(rgb);
// Note: libavif does NOT currently add alignment padding to rowBytes,
// but rowBytes depends on the pixel format:
// - AVIF_RGB_FORMAT_BGR with depth>8: 3 channels * 2 bytes = 6 bytes/pixel
// - AVIF_RGB_FORMAT_BGRA with depth>8: 4 channels * 2 bytes = 8 bytes/pixel
```

For `AVIF_RGB_FORMAT_BGR` (3 channels) at 10-bit depth (stored as uint16):
- `pixelSize = 3 * 2 = 6` bytes per pixel
- `rowBytes = width * 6`
- `idx = (y * width + x) * 3` would index `uint16_t`, so actual byte offset = `idx * 2`
- Correct byte offset = `y * rowBytes + x * 6 = y * width * 6 + x * 6`
- Bug offset: `(y * width + x) * 3 * 2 = y * width * 6 + x * 6` ← SAME for 3-channel!

Wait, let me recalculate. `idx = (y * width + x) * 3` used to index `uint16_t* src`:
- Memory byte offset = `idx * sizeof(uint16_t)` = `(y * width + x) * 3 * 2`
- = `y * width * 6 + x * 6`
- Correct row offset = `y * rowBytes + x * channels * 2` = `y * (width * 6) + x * 6`
- These ARE equal when `rowBytes = width * 6`...

## BUT: The AVIF Format Issue

The actual format stored in `temp` depends on `rgb->format` which is set to:
```cpp
d->rgb.format = AVIF_RGB_FORMAT_BGR;  // or AVIF_RGB_FORMAT_BGRA for alpha
```

But `avif_tonemap_rgb` assumes **3 channels** (`* 3` in the index) regardless of the actual format!

If the image has alpha (`d->has_alpha == true`), `rgb->format = AVIF_RGB_FORMAT_BGRA` (4 channels),
and `temp.format = rgb->format = AVIF_RGB_FORMAT_BGRA`.

Then `avifRGBImageAllocatePixels` allocates `width * height * 4 * 2` bytes (4 channels, 16-bit).
But `avif_tonemap_rgb` uses `idx = (y * width + x) * 3` — treating it as 3-channel data!

For a 4-channel BGRA image:
- Correct byte offset for pixel (x,y) channel 0: `(y * width + x) * 4 * 2`
- Bug byte offset: `(y * width + x) * 3 * 2`

This shifts ALL accesses, and at the end of the image:
- Correct final byte: `(height * width - 1) * 4 * 2 + 6` = `height * width * 8 - 2`
- Bug final byte: `(height * width - 1) * 3 * 2 + 4` = `height * width * 6 - 2`

For a 100×100 image: correct size = 80,000 bytes, bug reads up to 60,000 bytes.
The LAST row is fine, but every row except the last reads the WRONG pixels (from wrong channel offset).

For an alpha image where the bug misreads: the final pixels appear at (offset * 3/4) of the actual
buffer, which is WITHIN bounds... but wait:

Actually for index `(y * width + x) * 3` with y=height-1, x=width-1:
= `(height * width - 1) * 3`
= `height * width * 3 - 3`

Array size = `width * height * 4` (for BGRA)
= `width * height * 4` uint16_t values

Maximum safe index = `width * height * 4 - 3` (for last 3 values)
Bug maximum index = `width * height * 3 - 3`
These are within bounds!

But the DATA INTERPRETATION is wrong — pixel (x=10, y=5) in the bug will read from the
byte position of approximately pixel (x=7, y=5) in a BGRA layout, skipping the alpha channel.
This doesn't crash but causes incorrect tone-mapping for BGRA AVIF images.

## Summary of Impact

| Image Type | Effect |
|------------|--------|
| HDR AVIF, BGR (no alpha) | Correct stride, no bug (3ch * 2bytes = 6 bytes/px) |
| HDR AVIF, BGRA (alpha) | Wrong pixel data read (uses 3-channel stride on 4-ch data), results in corrupted tone-mapping output |

The severity is primarily **correctness** (wrong output) rather than memory safety for typical cases,
but non-standard libavif builds with alignment padding could create an OOB read.
