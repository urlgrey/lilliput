# Technical Analysis: GIF realloc NULL Dereference

## Primary Bug Location

`giflib.cpp`, function `giflib_decoder_decode_frame()`:

```cpp
size_t image_size = desc.Width * desc.Height;

if (image_size > (SIZE_MAX / sizeof(GifPixelType))) {
    fprintf(stderr, "encountered error, gif frame is too large\n");
    return false;
}

if (image_size > d->pixel_len) {
    // BUG: d->pixel_len updated BEFORE confirming realloc success
    d->pixel_len = image_size;
    d->pixels = (GifByteType*)(realloc(d->pixels, d->pixel_len * sizeof(GifPixelType)));
    // If realloc() returns NULL:
    //   - d->pixels = NULL     (original pointer LOST)
    //   - d->pixel_len = image_size  (tracks the "failed" size)
}

if (d->pixels == NULL) {
    fprintf(stderr, "encountered error, gif pixel buffer failed to allocate\n");
    return false;
}
```

## Secondary Bug Location

`giflib.cpp`, function `giflib_encoder_render_frame()`:

```cpp
int image_size = im_out->Width * im_out->Height;  // Note: int, not size_t!

if (image_size > e->pixel_len) {
    // BUG: Same unsafe realloc pattern
    e->pixel_len = image_size;
    e->pixels = (GifByteType*)(realloc(e->pixels, e->pixel_len * sizeof(GifPixelType)));
}
```

Note the additional bug: `image_size` is `int` here (not `size_t`), so for large frames
`Width * Height` can overflow to a negative value, causing `realloc(e->pixels, <very large>)`
due to sign-extension of the negative int to size_t.

## State Corruption Chain

1. Frame N (large): `realloc` succeeds → `d->pixels` valid, `d->pixel_len = N_size`
2. Frame N+1 (small): `image_size < d->pixel_len`, skip realloc → use existing buffer (OK)
3. Frame N+2 (largest): `realloc` FAILS → `d->pixels = NULL`, `d->pixel_len = N2_size`
4. Decoder returns `false` (caught by NULL check)
5. Caller might try recovery or the GIF has more frames in a subsequent call:
   - If caller retries with a SMALLER frame that fits `d->pixel_len`: No realloc happens,
     `d->pixels` is NULL, `DGifGetLine(d->gif, NULL, ...)` → SIGSEGV

## Memory Leak Severity

Each leaked pixel buffer can be:
- Maximum GIF dimensions: 65535 × 65535 = ~4 billion pixels = ~4 GB per frame
- With the integer overflow guard: `image_size > SIZE_MAX / sizeof(GifPixelType)` check
  prevents allocation of > SIZE_MAX bytes, so max is SIZE_MAX (but this is still a large leak)
- Practical maximum: limited by libgiflib's parsing, but can easily be 16MB+ per leak

## Related Issues in giflib_decoder_release

```cpp
void giflib_decoder_release(giflib_decoder d)
{
    if (d->pixels) {
        free(d->pixels);
    }
    // ...
}
```

If `d->pixels` is NULL due to the bug, `free(NULL)` is a no-op (safe in C/C++),
but the old non-NULL pointer that was leaked by the realloc bug is never freed.

## Reproducibility

The bug requires memory pressure to trigger `realloc` failure. Techniques:
1. Use `mmap` to exhaust virtual address space before calling the decoder
2. Use `ulimit -v` to set a low virtual memory limit
3. Use `LD_PRELOAD` with a custom allocator that fails after N allocations

See `repro/craft_gif_realloc.py` for the crafted GIF input.
