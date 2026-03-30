# Vulnerability: avcodec SAR-Adjusted Width/Height Integer Overflow

## Summary

In `avcodec.cpp`, `avcodec_decoder_get_width()` and `avcodec_decoder_get_height()` compute
the display dimensions by multiplying codec dimensions by the sample aspect ratio (SAR).
The multiplication uses `int64_t` for the intermediate result but **truncates back to `int`**
without checking for overflow.

Additionally, `avcodec_decoder_get_width` uses `d->codec->width * SAR.num / SAR.den` —
if SAR values are attacker-controlled (from the video container metadata), this can produce
an integer overflow resulting in a **negative or zero width/height** being returned.

## Affected File

- `avcodec.cpp`, functions `avcodec_decoder_get_width()` and `avcodec_decoder_get_height()`

## Vulnerability Class

- **CWE-190: Integer Overflow or Wraparound**
- Leads to incorrect dimension reporting, potential downstream OOB when callers use the
  dimension to allocate buffers

## Root Cause

```cpp
int avcodec_decoder_get_width(const avcodec_decoder d)
{
    if (d->codec) {
        AVStream* st = d->container->streams[d->video_stream_index];
        if (st->sample_aspect_ratio.num > 0 && st->sample_aspect_ratio.den > 0 &&
            st->sample_aspect_ratio.num > st->sample_aspect_ratio.den) {
            return (int64_t)d->codec->width * st->sample_aspect_ratio.num /
              st->sample_aspect_ratio.den;
            // ^^^ The int64_t intermediate is CAST TO int implicitly
            // If width=65535 and SAR=65535:1, result = 65535*65535 = 4,294,836,225
            // This overflows int (max 2,147,483,647) -> negative or wrong value
        }
        return d->codec->width;
    }
    return 0;
}
```

The condition `SAR.num > SAR.den` ensures the SAR is > 1:1, meaning width is multiplied up.
For extreme SAR values combined with maximum codec width:
- `codec->width` = 32,767 (max for some codecs) or 65,535
- `SAR.num` = 65,535
- Result before divide: up to 4,294,967,295 → overflows `int`

## Impact

- **Negative/zero width or height** reported by `avcodec_decoder_get_width/height`
- Go callers use these dimensions to allocate output buffers: a negative or zero dimension
  causes downstream errors or zero-size allocations
- A zero-size allocation followed by data writing = **heap buffer overflow**
- An extremely large (but valid) width could also trigger allocation of enormous buffers,
  leading to OOM/DoS

## Attack Vector

An attacker can craft a video file with:
- Normal video dimensions (e.g., 1920×1080)  
- Sample Aspect Ratio metadata set to e.g., 65535:1 (within allowed range)
- When processed, the returned "width" will be (1920 * 65535) / 1 = 125,827,200 — far larger
  than the input, potentially causing downstream buffer allocation issues

Or:
- SAR.num close to INT_MAX / codec->width to cause exact overflow to negative

## Proof of Concept

The script `repro/craft_mp4_sar_overflow.py` creates a real MP4 file:

```bash
# Requires: ffmpeg, python3
python3 repro/craft_mp4_sar_overflow.py poc.mp4

# Verify the SAR was injected:
ffprobe -show_streams poc.mp4 2>/dev/null | grep sample_aspect
# → sample_aspect_ratio=33554433:1

# The MP4 is a valid 64x64 H.264 stream with a pasp atom
# encoding SAR 33554433:1. When lilliput processes it:
#   avcodec_decoder_get_width() computes (int64_t)64 * 33554433 / 1
#   = 2,147,483,712 → truncated to int32 → -2,147,483,584
#   Downstream buffer allocation uses negative width → heap overflow
```

## Mitigation

```cpp
int avcodec_decoder_get_width(const avcodec_decoder d)
{
    if (d->codec) {
        AVStream* st = d->container->streams[d->video_stream_index];
        if (st->sample_aspect_ratio.num > 0 && st->sample_aspect_ratio.den > 0 &&
            st->sample_aspect_ratio.num > st->sample_aspect_ratio.den) {
            int64_t adjusted = (int64_t)d->codec->width * st->sample_aspect_ratio.num /
                               st->sample_aspect_ratio.den;
            // Clamp to valid range
            if (adjusted <= 0 || adjusted > 65535) {
                return d->codec->width;  // Fall back to raw dimension
            }
            return (int)adjusted;
        }
        return d->codec->width;
    }
    return 0;
}
```
