# Technical Analysis: WebP Decode Buffer Overflow

## Code Flow

### Step 1: Buffer Allocation (webp_decoder_create)

```cpp
// webp.cpp ~line 75
if (WebPMuxGetCanvasSize(mux, &d->width, &d->height) != WEBP_MUX_OK) {
    // ...
}

// ...

// Pre-allocate decode buffer using CANVAS dimensions
d->decode_buffer_size = d->width * d->height * 4;  // 4 bytes per pixel (RGBA)
d->decode_buffer = new uint8_t[d->decode_buffer_size];
```

### Step 2: Per-Frame Decode (webp_decoder_decode)

```cpp
// webp.cpp ~line 250+
WebPMuxFrameInfo frame;
WebPMuxGetFrame(d->mux, d->current_frame_index, &frame);

// Get FRAME-SPECIFIC dimensions (NOT canvas dimensions)
WebPBitstreamFeatures features;
WebPGetFeatures(frame.bitstream.bytes, frame.bitstream.size, &features);
// features.width and features.height are the FRAME dimensions

// Create cv::Mat with FRAME dimensions
auto cvMat = static_cast<cv::Mat*>(mat);
cvMat->create(features.height, features.width, webp_decoder_get_pixel_type(d));

// row_size based on FRAME width
int row_size = cvMat->cols * cvMat->elemSize();
// = features.width * (3 or 4)

// THE VULNERABLE CALL:
res = WebPDecodeBGRAInto(
    frame.bitstream.bytes,
    frame.bitstream.size,
    d->decode_buffer,       // allocated for canvas_w * canvas_h * 4
    d->decode_buffer_size,  // = canvas_w * canvas_h * 4
    row_size                // = features.width * 4
);
```

## WebPDecodeBGRAInto Internal Check

From libwebp source, `WebPDecodeBGRAInto` validates:
```c
if (output_size < (uint64_t)stride * config.output.height) {
    return NULL;
}
```
Where `stride = row_size = features.width * 4` and `config.output.height = features.height`.

So the check is: `d->decode_buffer_size >= features.width * 4 * features.height`

If `canvas_w * canvas_h * 4 >= features.width * 4 * features.height`, then no crash...
UNLESS `features.width > canvas_w`, in which case even the check may pass (since total size
could still be comparable), but the *row stride* is wider than expected.

## The Actual Overflow Scenario

Consider:
- Canvas: 100 × 100 (d->decode_buffer_size = 40,000 bytes)
- Frame 2: 200 × 50 (features: width=200, height=50)
  - row_size = 200 * 4 = 800
  - Required: 800 * 50 = 40,000 → check passes! But now the *layout* is wrong.
  - The buffer was intended for a 100×100 layout (stride 400), but we're writing stride 800.
  - memcpy target: `cvMat->data = d->decode_buffer` (assumed)... wait, let me re-check.

Actually re-reading the code:
```cpp
res = WebPDecodeBGRAInto(..., d->decode_buffer, d->decode_buffer_size, row_size);

if (res) {
    memcpy(cvMat->data, d->decode_buffer, cvMat->total() * cvMat->elemSize());
}
```

So `d->decode_buffer` is the target, and then `cvMat->data` gets the result via memcpy.
The overflow is into `d->decode_buffer` during the WebPDecodeBGRAInto call.

## Overflow Conditions

| Canvas (CW×CH) | Frame (FW×FH) | Buffer Size | Decode Size | Overflow? |
|----------------|---------------|-------------|-------------|-----------|
| 100×100        | 150×150       | 40,000      | 90,000      | YES (+50KB)|
| 100×100        | 100×200       | 40,000      | 80,000      | YES (+40KB)|
| 100×100        | 200×50        | 40,000      | 40,000      | NO (but wrong layout)|
| 100×100        | 50×200        | 40,000      | 40,000      | NO (but row confusion)|

The clear overflow case: frame height OR width larger than canvas.

## WebP Animation Spec

RFC 6386 / WebP Container Spec allows frame canvases to extend beyond the animation canvas.
The renderer is supposed to clip. However, the per-frame bitstream contains actual image
data at the frame's dimensions — a 200×200 frame in a 100×100 canvas contains 200×200 pixels
of actual decoded data.

## Stack Trace (expected with ASAN)

```
==ASAN: heap-buffer-overflow on address 0x...
WRITE of size 4 at 0x... thread T0
    #0 in VP8LDecodeAlphaImageStream  (libwebp)
    #1 in WebPDecodeBGRAInto          (libwebp)
    #2 in webp_decoder_decode         (webp.cpp:~275)
    #3 in ...
```
