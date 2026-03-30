# Technical Analysis: avcodec SAR Integer Overflow

## Affected Functions

### avcodec_decoder_get_width (avcodec.cpp)
```cpp
int avcodec_decoder_get_width(const avcodec_decoder d)
{
    if (d->codec) {
        AVStream* st = d->container->streams[d->video_stream_index];
        if (st->sample_aspect_ratio.num > 0 && st->sample_aspect_ratio.den > 0 &&
            st->sample_aspect_ratio.num > st->sample_aspect_ratio.den) {
            return (int64_t)d->codec->width * st->sample_aspect_ratio.num /
              st->sample_aspect_ratio.den;   // ← implicit narrowing to int
        }
        return d->codec->width;
    }
    return 0;
}
```

### avcodec_decoder_get_height (avcodec.cpp)
```cpp
int avcodec_decoder_get_height(const avcodec_decoder d)
{
    if (d->codec) {
        AVStream* st = d->container->streams[d->video_stream_index];
        if (st->sample_aspect_ratio.num > 0 && st->sample_aspect_ratio.den > 0 &&
            st->sample_aspect_ratio.den > st->sample_aspect_ratio.num) {
            return (int64_t)d->codec->height * st->sample_aspect_ratio.den /
              st->sample_aspect_ratio.num;   // ← implicit narrowing to int
        }
        return d->codec->height;
    }
    return 0;
}
```

## Overflow Analysis

### Width Overflow Example:
- `d->codec->width` = 16,384 (common H.264 max)
- `st->sample_aspect_ratio.num` = 196,608 (3:1 up-scaling of 65536 base)
- `st->sample_aspect_ratio.den` = 1
- Intermediate (int64_t): `16384 * 196608 / 1` = 3,221,225,472
- Cast to int: `3,221,225,472 & 0xFFFFFFFF` = -1,073,741,824 (overflow → negative!)

### Height Overflow Example:
- `d->codec->height` = 16,384
- `st->sample_aspect_ratio.den` = 131,072 (>num)
- `st->sample_aspect_ratio.num` = 1
- Intermediate: `16384 * 131072 / 1` = 2,147,483,648
- Cast to int: exactly INT_MAX+1 = -2,147,483,648 (overflow → most-negative int!)

## SAR Value Sources

Sample Aspect Ratio values come from:
1. `AVStream.sample_aspect_ratio` — set from container metadata
2. H.264 VUI parameters (attacker-controlled in the video bitstream)
3. MPEG-4 pixel aspect ratio box (avcC, pasp atoms)

In an H.264 stream, VUI parameters include `sar_width` and `sar_height` as 16-bit values
(0-65535 each). An attacker can set these to arbitrary values in a crafted bitstream.

## Downstream Impact

The returned width/height values are consumed by Go code (`avcodec.go`):
```go
width := int(C.avcodec_decoder_get_width(d.decoder))
height := int(C.avcodec_decoder_get_height(d.decoder))
```

If width or height is negative:
- `opencv_mat_create(width, height, type)` receives a negative dimension
- OpenCV typically throws `cv::Exception` for negative dimensions
- This can result in a panic/crash in the Go caller

If width/height overflows to a small positive number:
- A small output buffer is allocated
- The actual pixel data may be larger → OOB write into the allocation

## Interaction with avcodec_decoder_copy_frame

```cpp
static int avcodec_decoder_copy_frame(const avcodec_decoder d, opencv_mat mat, AVFrame* frame)
{
    // ...
    int stepSize = 4 * cvMat->cols;  // Based on cvMat dimensions, which were set from
                                      // the get_width/get_height returns
    // ...
    sws_scale(sws, frame->data, frame->linesize, 0, frame->height, dstData, dstLinesizes);
    // frame->height is the ACTUAL video height
    // cvMat->rows was set from avcodec_decoder_get_height() (which may have overflowed)
    // If cvMat->rows < frame->height, sws_scale writes past the end of cvMat->data
}
```

This creates a potential write OOB: if the overflowed height is smaller than the actual
frame height, `sws_scale` writes more rows than the output matrix can hold.

## CVSS

- Base Score: 8.1 (High)
- AV:N/AC:H/PR:N/UI:N/S:U/C:H/I:H/A:H
- Network-accessible, no privileges required, but exploitability depends on SAR overflow
  producing a specific small value that triggers the downstream OOB
