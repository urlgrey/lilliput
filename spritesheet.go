package lilliput

// #include "avcodec.hpp"
// #include "opencv.hpp"
import "C"

import (
	"encoding/json"
	"fmt"
	"image"
	"strings"
	"time"
)

const (
	// SpriteSheetGridColumns is the number of columns in a sprite sheet grid.
	SpriteSheetGridColumns = 5

	// SpriteSheetTileMaxDim is the maximum dimension (width or height) of each tile in pixels.
	SpriteSheetTileMaxDim = 160

	// Tile count thresholds (from RFC).
	spriteSheetThresholdDense  = 50 * time.Second  // ≤50s → 1 tile/second
	spriteSheetThresholdMedium = 15 * time.Minute  // ≤15min → 50 tiles evenly spaced
	spriteSheetMaxTilesDense   = 50                // max tiles in the dense path
	spriteSheetTilesMedium     = 50                // fixed tile count for medium-length content
	spriteSheetTilesLong       = 100               // fixed tile count for long content
)

// SpriteSheetOptions controls how a sprite sheet is generated.
type SpriteSheetOptions struct {
	// FileType controls the output format of the sprite sheet image.
	// Supported values: ".jpeg", ".webp", ".png"
	// Defaults to ".jpeg" if empty.
	FileType string

	// EncodeOptions controls encode quality, e.g. map[int]int{lilliput.JpegQuality: 80}
	EncodeOptions map[int]int

	// GridColumns overrides the default number of grid columns (default: 5).
	GridColumns int

	// TileMaxDim overrides the maximum tile dimension in pixels (default: 160).
	TileMaxDim int
}

// SpriteSheetTile describes the position of one tile within the sprite sheet.
type SpriteSheetTile struct {
	// TimestampSec is the video timestamp this tile was captured at, in seconds.
	TimestampSec float64 `json:"timestamp_sec"`
	// X is the tile's left edge in the sprite sheet image, in pixels.
	X int `json:"x"`
	// Y is the tile's top edge in the sprite sheet image, in pixels.
	Y int `json:"y"`
	// Width is the tile width in pixels.
	Width int `json:"width"`
	// Height is the tile height in pixels.
	Height int `json:"height"`
}

// SpriteSheetManifest describes a generated sprite sheet: its dimensions and tile map.
type SpriteSheetManifest struct {
	// SheetWidth is the total width of the sprite sheet image in pixels.
	SheetWidth int `json:"sheet_width"`
	// SheetHeight is the total height of the sprite sheet image in pixels.
	SheetHeight int `json:"sheet_height"`
	// TileWidth is the width of each tile (constant across all tiles).
	TileWidth int `json:"tile_width"`
	// TileHeight is the height of each tile (constant across all tiles).
	TileHeight int `json:"tile_height"`
	// GridColumns is the number of columns in the grid.
	GridColumns int `json:"grid_columns"`
	// Tiles contains one entry per tile, in row-major order.
	Tiles []SpriteSheetTile `json:"tiles"`
}

// MarshalJSON serializes the manifest to JSON.
func (m *SpriteSheetManifest) MarshalJSON() ([]byte, error) {
	type Alias SpriteSheetManifest
	return json.Marshal((*Alias)(m))
}

// WebVTT generates a WebVTT thumbnail track string from the manifest.
// urlTemplate should be the URL of the sprite sheet image (e.g. "https://cdn.example.com/sheet.jpg").
// It uses the JW Player #xywh= convention.
func (m *SpriteSheetManifest) WebVTT(urlTemplate string) string {
	var sb strings.Builder
	sb.WriteString("WEBVTT\n\n")
	for i, tile := range m.Tiles {
		var endSec float64
		if i+1 < len(m.Tiles) {
			endSec = m.Tiles[i+1].TimestampSec
		} else {
			endSec = tile.TimestampSec + 1
		}
		sb.WriteString(fmt.Sprintf("%s --> %s\n", formatVTTTime(tile.TimestampSec), formatVTTTime(endSec)))
		sb.WriteString(fmt.Sprintf("%s#xywh=%d,%d,%d,%d\n\n",
			urlTemplate, tile.X, tile.Y, tile.Width, tile.Height))
	}
	return sb.String()
}

// formatVTTTime formats a float64 seconds value as HH:MM:SS.mmm.
func formatVTTTime(sec float64) string {
	d := time.Duration(sec * float64(time.Second))
	h := int(d.Hours())
	m := int(d.Minutes()) % 60
	s := int(d.Seconds()) % 60
	ms := int(d.Milliseconds()) % 1000
	return fmt.Sprintf("%02d:%02d:%02d.%03d", h, m, s, ms)
}

// selectTimestamps returns the set of timestamps (in seconds) at which to extract tiles,
// given the video duration. Follows the RFC tile count rules.
func selectTimestamps(duration time.Duration) []float64 {
	if duration <= 0 {
		return nil
	}
	durationSec := duration.Seconds()

	var count int
	switch {
	case duration <= spriteSheetThresholdDense:
		// 1 tile/second, capped at spriteSheetMaxTilesDense
		count = int(durationSec)
		if count > spriteSheetMaxTilesDense {
			count = spriteSheetMaxTilesDense
		}
		if count == 0 {
			count = 1
		}
	case duration <= spriteSheetThresholdMedium:
		count = spriteSheetTilesMedium
	default:
		count = spriteSheetTilesLong
	}

	timestamps := make([]float64, count)
	if count == 1 {
		timestamps[0] = 0
		return timestamps
	}
	// Evenly distribute across [0, duration). Start slightly into the video
	// to avoid black first frames.
	step := durationSec / float64(count)
	for i := 0; i < count; i++ {
		timestamps[i] = step * float64(i)
	}
	return timestamps
}

// tileDimensions calculates the tile width/height given the video's source dimensions
// and the max tile dimension constraint.
func tileDimensions(srcWidth, srcHeight, maxDim int) (tileW, tileH int) {
	if srcWidth <= 0 || srcHeight <= 0 || maxDim <= 0 {
		return maxDim, maxDim
	}
	if srcWidth >= srcHeight {
		// landscape or square
		tileW = maxDim
		tileH = int(float64(maxDim) * float64(srcHeight) / float64(srcWidth))
	} else {
		// portrait
		tileH = maxDim
		tileW = int(float64(maxDim) * float64(srcWidth) / float64(srcHeight))
	}
	if tileW < 1 {
		tileW = 1
	}
	if tileH < 1 {
		tileH = 1
	}
	return tileW, tileH
}

// GenerateSpriteSheet extracts frames from the video Decoder d and assembles them
// into a sprite sheet image encoded into dst. It returns the encoded sprite sheet bytes,
// the manifest describing tile positions, and any error.
//
// d must be an avCodecDecoder (i.e. a video source). The caller is responsible for
// closing d after this call.
//
// dst should be a pre-allocated byte slice large enough for the output image. A
// typical allocation is 8MB for JPEG output.
func GenerateSpriteSheet(d Decoder, opt *SpriteSheetOptions, dst []byte) ([]byte, *SpriteSheetManifest, error) {
	if opt == nil {
		opt = &SpriteSheetOptions{}
	}

	fileType := opt.FileType
	if fileType == "" {
		fileType = ".jpeg"
	}
	gridCols := opt.GridColumns
	if gridCols <= 0 {
		gridCols = SpriteSheetGridColumns
	}
	tileMaxDim := opt.TileMaxDim
	if tileMaxDim <= 0 {
		tileMaxDim = SpriteSheetTileMaxDim
	}

	// Resolve the underlying avCodecDecoder so we can seek.
	avd, ok := d.(*avCodecDecoder)
	if !ok {
		return nil, nil, fmt.Errorf("spritesheet: decoder must be an avCodecDecoder (video source)")
	}

	hdr, err := d.Header()
	if err != nil {
		return nil, nil, fmt.Errorf("spritesheet: failed to read header: %w", err)
	}

	duration := d.Duration()
	if duration <= 0 {
		return nil, nil, fmt.Errorf("spritesheet: video has no duration")
	}

	srcW := hdr.Width()
	srcH := hdr.Height()
	if srcW <= 0 || srcH <= 0 {
		return nil, nil, fmt.Errorf("spritesheet: invalid video dimensions %dx%d", srcW, srcH)
	}

	timestamps := selectTimestamps(duration)
	if len(timestamps) == 0 {
		return nil, nil, fmt.Errorf("spritesheet: no timestamps selected for duration %v", duration)
	}

	tileW, tileH := tileDimensions(srcW, srcH, tileMaxDim)

	rows := (len(timestamps) + gridCols - 1) / gridCols
	sheetW := tileW * gridCols
	sheetH := tileH * rows

	// Allocate the sprite sheet canvas as a 3-channel BGR mat.
	sheet := NewFramebuffer(sheetW, sheetH)
	defer sheet.Close()
	if err := sheet.Create3Channel(sheetW, sheetH); err != nil {
		return nil, nil, fmt.Errorf("spritesheet: failed to create canvas: %w", err)
	}
	// Zero-fill the canvas (black background).
	C.opencv_mat_reset(sheet.mat)

	// Allocate a reusable per-tile framebuffer.
	tile := NewFramebuffer(srcW, srcH)
	defer tile.Close()

	scaledTile := NewFramebuffer(tileW, tileH)
	defer scaledTile.Close()

	manifest := &SpriteSheetManifest{
		SheetWidth:  sheetW,
		SheetHeight: sheetH,
		TileWidth:   tileW,
		TileHeight:  tileH,
		GridColumns: gridCols,
		Tiles:       make([]SpriteSheetTile, 0, len(timestamps)),
	}

	for i, ts := range timestamps {
		col := i % gridCols
		row := i / gridCols
		destX := col * tileW
		destY := row * tileH

		// Seek and decode the frame at this timestamp.
		ok := bool(C.avcodec_decoder_seek_and_decode(avd.decoder, C.float(ts), tile.mat))
		if !ok {
			// On extraction failure, leave this tile black and continue.
			manifest.Tiles = append(manifest.Tiles, SpriteSheetTile{
				TimestampSec: ts,
				X: destX, Y: destY,
				Width: tileW, Height: tileH,
			})
			continue
		}

		// The mat may have been resized by the decode; update the Framebuffer width/height.
		tile.width = int(C.opencv_mat_get_width(tile.mat))
		tile.height = int(C.opencv_mat_get_height(tile.mat))

		// Scale the decoded frame to tile dimensions.
		if err := tile.ResizeTo(tileW, tileH, scaledTile); err != nil {
			// Non-fatal: leave the tile black.
			manifest.Tiles = append(manifest.Tiles, SpriteSheetTile{
				TimestampSec: ts,
				X: destX, Y: destY,
				Width: tileW, Height: tileH,
			})
			continue
		}

		// Copy the scaled tile into the correct position on the canvas.
		destRect := image.Rect(destX, destY, destX+tileW, destY+tileH)
		if err := sheet.CopyToOffsetNoBlend(scaledTile, destRect); err != nil {
			// Non-fatal.
			manifest.Tiles = append(manifest.Tiles, SpriteSheetTile{
				TimestampSec: ts,
				X: destX, Y: destY,
				Width: tileW, Height: tileH,
			})
			continue
		}

		manifest.Tiles = append(manifest.Tiles, SpriteSheetTile{
			TimestampSec: ts,
			X: destX, Y: destY,
			Width: tileW, Height: tileH,
		})
	}

	// Encode the assembled sprite sheet.
	enc, err := NewEncoder(fileType, nil, dst, nil)
	if err != nil {
		return nil, nil, fmt.Errorf("spritesheet: failed to create encoder: %w", err)
	}
	defer enc.Close()

	encOpts := opt.EncodeOptions
	if encOpts == nil {
		switch strings.ToLower(fileType) {
		case ".jpeg", ".jpg":
			encOpts = map[int]int{JpegQuality: 80}
		case ".webp":
			encOpts = map[int]int{WebpQuality: 85}
		case ".png":
			encOpts = map[int]int{PngCompression: 6}
		}
	}

	result, err := enc.Encode(sheet, encOpts)
	if err != nil {
		return nil, nil, fmt.Errorf("spritesheet: encode failed: %w", err)
	}

	return result, manifest, nil
}


