# Vulnerability: WebP Per-Frame Decode Buffer Overflow

## Summary

In `webp.cpp`, `webp_decoder_create()` pre-allocates a decode buffer based on the **canvas**
dimensions. However, `webp_decoder_decode()` calls `WebPDecodeBGRAInto()` (or `WebPDecodeBGRInto`)
using per-frame dimensions from `WebPBitstreamFeatures`, which can exceed the canvas dimensions
(WebP animated format allows frames larger than the canvas, relying on clipping by the renderer).

This can result in `WebPDecodeBGRAInto` writing more bytes than the pre-allocated buffer can hold,
causing a **heap buffer overflow**.

## Affected File

- `webp.cpp`, functions `webp_decoder_create()` and `webp_decoder_decode()`

## Vulnerability Class

**CWE-122: Heap-based Buffer Overflow** (write beyond pre-allocated buffer)

## Root Cause

### Allocation in `webp_decoder_create()`:
```cpp
// Buffer sized to CANVAS dimensions (d->width, d->height)
d->decode_buffer_size = d->width * d->height * 4;  // canvas size
d->decode_buffer = new uint8_t[d->decode_buffer_size];
```

### Usage in `webp_decoder_decode()`:
```cpp
WebPMuxFrameInfo frame;
WebPMuxGetFrame(d->mux, d->current_frame_index, &frame);

WebPBitstreamFeatures features;
WebPGetFeatures(frame.bitstream.bytes, frame.bitstream.size, &features);

// cvMat resized to FRAME dimensions (may be != canvas)
cvMat->create(features.height, features.width, ...);

// row_size calculated from FRAME width
int row_size = cvMat->cols * cvMat->elemSize();

// Decode into d->decode_buffer sized for CANVAS, but using FRAME dimensions
res = WebPDecodeBGRAInto(
    frame.bitstream.bytes,
    frame.bitstream.size,
    d->decode_buffer,         // ← allocated for canvas_w * canvas_h * 4
    d->decode_buffer_size,    // ← canvas_w * canvas_h * 4
    row_size                  // ← frame_width * 4 (may be > canvas_width * 4)
);
```

If `features.width > d->width` OR `features.height > d->height`, then:
- `WebPDecodeBGRAInto` will write `features.width * features.height * 4` bytes
- But only `d->width * d->height * 4` bytes were allocated
- **Result**: heap buffer overflow

## Impact

- **Heap overflow**: Up to `(frame_w * frame_h - canvas_w * canvas_h) * 4` bytes overflowed
- **Memory corruption**: Corrupts heap metadata or adjacent allocations
- **Potential RCE**: On exploitable heap layouts, adjacent structures can be overwritten
- **Crash**: SIGSEGV or heap corruption detection via ASAN/valgrind

## Exploitation

1. Craft a WebP animation where frame dimensions exceed canvas dimensions
2. Feed to lilliput's WebP decoder
3. `WebPDecodeBGRAInto` writes past the end of `d->decode_buffer`

## Proof of Concept

See `repro/craft_webp_overflow.py` — creates a WebP animation with frames larger than canvas.

## Mitigation

The fix requires checking per-frame dimensions against the pre-allocated buffer, and either:

**Option A**: Re-allocate the decode buffer if the frame is larger than the canvas:
```cpp
size_t required_size = (size_t)features.width * features.height * cvMat->elemSize();
if (required_size > d->decode_buffer_size) {
    delete[] d->decode_buffer;
    d->decode_buffer_size = required_size;
    d->decode_buffer = new uint8_t[d->decode_buffer_size];
}
```

**Option B**: Reject frames whose dimensions exceed the pre-allocated buffer:
```cpp
if ((size_t)features.width > (size_t)d->width || 
    (size_t)features.height > (size_t)d->height) {
    WebPDataClear(&frame.bitstream);
    return false;
}
```

Additionally, the `decode_buffer_size` should be passed to `WebPDecodeBGRAInto` correctly:
```cpp
// Wrong: d->decode_buffer_size may be < frame_w * frame_h * 4
res = WebPDecodeBGRAInto(..., d->decode_buffer, d->decode_buffer_size, row_size);
```
The `WebPDecodeBGRAInto` function does check `output_size` internally, so the overflow
requires that `d->decode_buffer_size >= frame_w * frame_h * channels` but the buffer was
only allocated for `canvas_w * canvas_h * channels`. The internal check would catch this IF
the decode_buffer_size is passed correctly — but the check is only `output_size >= stride * height`,
where `stride = row_size` which uses frame dimensions, so the check passes when frame_h > canvas_h.
