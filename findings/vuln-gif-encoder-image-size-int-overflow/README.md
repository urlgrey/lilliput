# Vulnerability: GIF Encoder image_size Integer Overflow + Unsafe realloc

## Summary

In `giflib.cpp`, `giflib_encoder_render_frame()` computes `image_size` as `int` (not `size_t`),
causing a signed integer overflow for large frames. Additionally, the same unsafe `realloc`
pattern (store result back directly) means that on realloc failure, `e->pixels` is set to NULL
and the old pointer is leaked. The `int image_size` can also produce a **negative value** for
frames where `Width * Height > INT_MAX`, causing `realloc(e->pixels, negative_size)` — which
is undefined behavior on most platforms (treated as a very large size_t due to sign extension).

## Affected File

- `giflib.cpp`, function `giflib_encoder_render_frame()`

## Vulnerability Class

- **CWE-190: Integer Overflow** (`int image_size` with large frame dimensions)
- **CWE-401: Memory Leak** (unsafe realloc pattern overwrites old pointer on failure)
- **CWE-131: Incorrect Calculation of Buffer Size** (negative size passed to realloc)

## Root Cause

```cpp
static bool giflib_encoder_render_frame(giflib_encoder e,
                                        const giflib_decoder d,
                                        const opencv_mat opaque_frame)
{
    // ...
    GifImageDesc* im_out = &gif_out->Image;
    im_out->Width = frame->cols;
    im_out->Height = frame->rows;

    int image_size = im_out->Width * im_out->Height;  // BUG 1: int, not size_t
                                                        // If Width*Height > INT_MAX, overflow!

    if (image_size > e->pixel_len) {                  // BUG 2: if image_size overflowed to
                                                        // negative, this is FALSE (negative < len)
                                                        // so NO realloc, but next line writes past
        e->pixel_len = image_size;
        e->pixels = (GifByteType*)(realloc(e->pixels, e->pixel_len * sizeof(GifPixelType)));
        // BUG 3: unsafe realloc -- if realloc fails, e->pixels = NULL, old ptr leaked
    }
```

Note: `GifImageDesc.Width` is of type `int` in giflib, and `GifImageDesc.Height` is also `int`.
The multiplication `int * int` can overflow when Width × Height > 2,147,483,647 (~2 billion).

GIF maximum dimensions are theoretically 65535 × 65535 = 4,294,836,225 — larger than INT_MAX.
So this overflow IS reachable for near-maximum GIF dimensions.

## Overflow Scenarios

| Width  | Height | int overflow? | image_size (int) | realloc size |
|--------|--------|--------------|------------------|--------------|
| 46341  | 46341  | YES          | -2,147,418,113   | massive (via UB sign-ext) |
| 65535  | 65535  | YES          | -131071          | huge        |
| 46340  | 46340  | NO           | 2,147,395,600    | 2 GB alloc  |

For `image_size = -131071` (negative):
- `if (image_size > e->pixel_len)`: if `e->pixel_len > 0`, this is FALSE
  (negative int < positive int)
- No realloc: `e->pixels` still points to old (smaller) buffer
- But `e->pixel_len = image_size = -131071` (stored as size_t → wraps to huge)
- The loop writes `frame->cols * frame->rows` pixels into the old undersized buffer
  → **heap buffer overflow**

## Impact

- **Heap buffer overflow**: For frames with Width×Height > INT_MAX, the encoder writes
  more pixels than the buffer can hold
- **Memory leak**: Unsafe realloc pattern
- **Crash or RCE**: Heap overflow can corrupt allocator metadata or adjacent objects
- **DoS**: Even without RCE, triggers a crash in the processing pipeline

## Mitigation

```cpp
// Use size_t, not int:
size_t image_size = (size_t)im_out->Width * (size_t)im_out->Height;

// Bounds check before allocation:
if (image_size > SIZE_MAX / sizeof(GifPixelType)) {
    fprintf(stderr, "encoder frame too large\n");
    return false;
}

// Safe realloc:
if (image_size > e->pixel_len) {
    GifByteType* new_buf = (GifByteType*)realloc(e->pixels, image_size * sizeof(GifPixelType));
    if (!new_buf) {
        fprintf(stderr, "out of memory allocating encoder pixel buffer\n");
        return false;
    }
    e->pixels = new_buf;
    e->pixel_len = image_size;
}
```

Note: `e->pixel_len` must also be changed from `size_t` (it currently is `size_t` per the struct)
to ensure it stores the full size without truncation.

## Related Issue

Compare with `giflib_decoder_decode_frame()` which correctly uses `size_t image_size` and
checks `image_size > (SIZE_MAX / sizeof(GifPixelType))`. The encoder function was apparently
missed during the same hardening pass.
