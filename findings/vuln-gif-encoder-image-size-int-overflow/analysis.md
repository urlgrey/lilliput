# Technical Analysis: GIF Encoder image_size Integer Overflow

## Code Comparison: Decoder vs Encoder

### Decoder (SAFE - giflib_decoder_decode_frame):
```cpp
size_t image_size = desc.Width * desc.Height;  // size_t ✓

if (image_size > (SIZE_MAX / sizeof(GifPixelType))) {  // overflow check ✓
    fprintf(stderr, "encountered error, gif frame is too large\n");
    return false;
}

if (image_size > d->pixel_len) {
    d->pixel_len = image_size;
    d->pixels = (GifByteType*)(realloc(d->pixels, d->pixel_len * sizeof(GifPixelType)));
    // Note: still has unsafe realloc pattern (see vuln-gif-realloc-null-deref)
}
```

### Encoder (VULNERABLE - giflib_encoder_render_frame):
```cpp
int image_size = im_out->Width * im_out->Height;  // int ✗, NO overflow check ✗

if (image_size > e->pixel_len) {  // signed comparison, can be wrong ✗
    e->pixel_len = image_size;
    e->pixels = (GifByteType*)(realloc(e->pixels, e->pixel_len * sizeof(GifPixelType)));
    // unsafe realloc ✗
}
```

The decoder was hardened but the encoder was not.

## Type Analysis

`im_out->Width` and `im_out->Height` are `int` (from `GifImageDesc`).
`int * int` in C++ produces `int` (the "usual arithmetic conversions" don't widen to 64-bit).

For the maximum GIF dimensions:
- GIF allows 16-bit canvas dimensions: 65535 × 65535
- But `GifImageDesc.Width/Height` are `int` — giflib doesn't enforce the 16-bit limit internally
- Frames can legally be 65535 × 65535

Overflow calculation:
```
65535 * 65535 = 4,294,836,225
INT_MAX       = 2,147,483,647
4,294,836,225 - 2*2,147,483,648 = -131,071  (two's complement wrap)
```

So `int image_size = 65535 * 65535` evaluates to `-131,071` in C (signed overflow = UB in C++,
but in practice wraps on all common architectures with two's complement).

## The Compound Bug: Overflow + Stale Buffer

Assuming `e->pixel_len` = 0 initially:
1. First frame: `image_size = 65535 * 65535 = -131071` (negative after overflow)
2. `if (-131071 > 0)` → FALSE (since -131071 < 0)
3. No realloc: `e->pixels` is NULL (initial state)
4. `GifByteType* raster_out = e->pixels;` → raster_out = NULL
5. `*raster_out++ = transparency_index;` → **NULL dereference**

Alternatively, if an earlier small frame was processed:
1. First frame (small): `image_size = 100`, realloc to 100 bytes, `e->pixel_len = 100`
2. Second frame (large): `image_size = -131071`, `if (-131071 > 100)` → FALSE
3. No realloc: `e->pixels` still points to 100-byte buffer
4. The loop writes `65535 * 65535 ≈ 4 billion` pixels into the 100-byte buffer
5. **Catastrophic heap overflow**

## The Compound Bug: Overflow + realloc with huge size_t

For `image_size = -131071`:
`(GifByteType*)realloc(e->pixels, e->pixel_len * sizeof(GifPixelType))`

When stored as `e->pixel_len = (size_t)-131071`:
- On 64-bit: `e->pixel_len = 18446744073709420545` (≈ 16 exabytes)
- `realloc(ptr, 18446744073709420545)` will fail (no such memory)
- Returns NULL → `e->pixels = NULL`, old pointer leaked
- Then the NULL check would catch it... but `e->pixel_len` is now a huge poisoned value

## EGifPutLine Interaction

Even if the overflow doesn't crash in the render loop, the subsequent `EGifPutLine` calls:
```cpp
for (int i = 0; i < frame_height; i++) {
    res = EGifPutLine(e->gif, e->pixels + i * frame_width, frame_width);
}
```
For frame_width=65535 and frame_height=65535, this iterates 65535 times, each call reading
65535 bytes from `e->pixels`. If `e->pixels` points to a smaller buffer, all calls past
the first few hundred rows are reading OOB from the pixel buffer.

## PoC Trigger

See `repro/trigger_encoder_overflow.py` for the GIF input that exercises this path via
the encoder (round-trip: decode a large GIF, encode back).
