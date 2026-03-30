# Vulnerability: GIF Pixel Buffer realloc Memory Leak + NULL Dereference

## Summary

In `giflib.cpp`, `giflib_decoder_decode_frame()` uses `realloc()` to grow the pixel buffer
when a new frame is larger than the previous. The standard unsafe `realloc` pattern is used:
the return value is stored directly back to `d->pixels`, which means on allocation failure
(realloc returns NULL), the original pointer is lost and a memory leak occurs. Additionally,
the subsequent NULL check happens *after* the old pointer has been overwritten, meaning:

1. The old `d->pixels` pointer is leaked (memory leak)
2. A subsequent call to `giflib_decoder_decode_frame()` with a larger frame will pass the
   NULL check but immediately dereference `d->pixels` (NULL dereference / crash)

## Affected File

- `giflib.cpp`, function `giflib_decoder_decode_frame()`, pixel buffer reallocation

## Vulnerability Class

- **CWE-401: Missing Release of Memory after Effective Lifetime** (memory leak)
- **CWE-476: NULL Pointer Dereference** (crash on subsequent frame)

## Root Cause

```cpp
// giflib.cpp - giflib_decoder_decode_frame()
if (image_size > d->pixel_len) {
    d->pixel_len = image_size;
    d->pixels = (GifByteType*)(realloc(d->pixels, d->pixel_len * sizeof(GifPixelType)));
    // ^^ If realloc fails, d->pixels = NULL but old pointer is lost (LEAK)
}

if (d->pixels == NULL) {  // This check is AFTER the assignment, too late to recover
    fprintf(stderr, "encountered error, gif pixel buffer failed to allocate\n");
    return false;
}
```

The correct pattern would be:
```cpp
GifByteType* new_pixels = (GifByteType*)realloc(d->pixels, image_size * sizeof(GifPixelType));
if (new_pixels == NULL) {
    // old d->pixels still valid, can free it or retry
    return false;
}
d->pixels = new_pixels;
d->pixel_len = image_size;
```

## Impact

- **Memory leak**: On realloc failure, the original pixel buffer (potentially megabytes for large
  frames) is leaked. In an animated GIF processing scenario, this can cause unbounded memory growth.
- **NULL dereference**: `DGifGetLine(d->gif, d->pixels, image_size)` with `d->pixels == NULL`
  causes a write to address 0 → SIGSEGV / process crash.
- **DoS**: A crafted multi-frame GIF that triggers realloc failures can crash the process.

## Exploitation Scenario

To trigger realloc failure in a memory-constrained environment:
1. Craft a GIF with many frames of progressively increasing size (1×1, 2×2, 4×4, ... 4096×4096)
2. Each frame triggers a `realloc()` call for a larger buffer
3. Under memory pressure (e.g., combined with other memory exhaustion), `realloc()` returns NULL
4. `d->pixels` is set to NULL; `d->pixel_len` is set to the (new) image_size
5. The NULL check catches this and returns false — but the original buffer is leaked
6. On the *next* call: if a frame is smaller than `d->pixel_len` (the corrupted value), the
   `realloc` is skipped, and `d->pixels == NULL` is used directly → crash

## Proof of Concept

See `repro/craft_gif_realloc.py` — creates a multi-frame GIF with escalating frame sizes.

## Mitigation

```cpp
if (image_size > d->pixel_len) {
    GifByteType* new_buf = (GifByteType*)realloc(d->pixels, image_size * sizeof(GifPixelType));
    if (new_buf == NULL) {
        fprintf(stderr, "encountered error, gif pixel buffer failed to allocate\n");
        return false;  // d->pixels still points to old (valid) buffer
    }
    d->pixels = new_buf;
    d->pixel_len = image_size;
}
```

Note: The same unsafe realloc pattern appears in `giflib_encoder_render_frame()` for `e->pixels`:
```cpp
if (image_size > e->pixel_len) {
    e->pixel_len = image_size;
    e->pixels = (GifByteType*)(realloc(e->pixels, e->pixel_len * sizeof(GifPixelType)));
    // Same bug: if realloc fails, e->pixels = NULL and old pointer is leaked
}
```
