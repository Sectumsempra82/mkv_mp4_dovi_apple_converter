# mkv_mp4_dovi_apple_converter

Batch-convert **Dolby Vision MKV** files into **Apple-friendly MP4** files so they play in the **Apple TV app / QuickTime** with proper Dolby Vision processing.

This does **not** re-encode the video stream. It:

1) extracts the HEVC bitstream from the MKV (copy),
2) preserves Dolby Vision metadata,
3) intelligently handles audio (surround + fallback),
4) converts supported subtitle tracks into Apple-compatible format,
5) muxes everything into MP4 using **GPAC MP4Box** (keeps Dolby Vision signaling),
6) deletes intermediate files,
7) processes one file at a time,
8) skips files when output already exists,
9) skips MKVs that are not Dolby Vision.

## Why this exists

`ffmpeg -c copy` MKV→MP4 often drops Dolby Vision signaling, so Apple apps treat the result as HDR10 only. MP4Box is typically much more reliable for preserving DV signaling.

This tool is specifically designed to preserve:

- Dolby Vision metadata
- Apple playback compatibility
- Proper surround audio handling
- Proper subtitle behavior inside MP4

---

## Audio Handling

The script uses the **first audio track only** and applies the following logic:

### Always:
- Generate an **AAC stereo fallback** (for guaranteed Apple compatibility)

### Also:
- Copy the original first audio track (no re-encode), when possible

### Track Ordering Logic

| Source Codec | Audio Track #1 | Audio Track #2 |
|--------------|----------------|----------------|
| E-AC3        | Original E-AC3 | AAC Stereo     |
| AC-3         | Original AC-3  | AAC Stereo     |
| Anything else| AAC Stereo     | Original Copy  |

Why?

- Apple prefers E-AC3 / AC-3 when available.
- AAC stereo guarantees playback on any output device.
- Other codecs (DTS, TrueHD, FLAC, etc.) may not be supported by Apple apps — but are still preserved as secondary tracks when possible.

If the MKV has no audio track, it produces a video-only MP4.

---

## Subtitle Handling

Each subtitle track is evaluated individually:

### Supported (converted to mov_text / tx3g inside MP4):

- SRT / SubRip
- WebVTT
- ASS / SSA (formatting may simplify)
- mov_text
- TTML (when supported by ffmpeg)

Each supported subtitle stream is converted to a small MP4 containing a `mov_text` subtitle track and then added to the final MP4.

Result:
- All supported subtitle tracks are selectable in Apple TV / QuickTime.
- Language metadata is preserved when available.

### Not Supported (dropped):

- PGS (Blu-ray image subtitles)
- VobSub / DVD subtitles

Image-based subtitles require OCR or burn-in and are intentionally not handled.

---

## Requirements

### Python
- Python 3.9+ (3.10+ recommended)

### External tools (required)
- `ffmpeg` (includes `ffprobe`)
- `MP4Box` (from GPAC)

> `pip install` alone is not enough because `ffmpeg` and `MP4Box` are native binaries.

---

## Install

### macOS
```bash
brew install ffmpeg gpac
```

### Linux (examples)

#### Debian/Ubuntu:
```
sudo apt update && sudo apt install -y ffmpeg gpac
```

#### Fedora:
```
sudo dnf install -y ffmpeg gpac
```

#### Arch:
```
sudo pacman -S ffmpeg gpac
```

### Windows

Pick one:

#### Winget:
```
winget install Gyan.FFmpeg
winget install GPAC.GPAC
```

#### Chocolatey:
```
choco install ffmpeg gpac
```

#### Scoop:
```
scoop install ffmpeg gpac
```

After installing on Windows, open a new terminal so PATH updates apply.

---

## Usage

Put the script in the folder containing your MKV files:

```
batch_dovi_mp4box.py
```

### Run it:

```
python3 batch_dovi_mp4box.py
```

---

## Behavior

Input: `*.mkv` in the current folder

Output: same name, `.mp4` extension (e.g., `Movie.mkv` → `Movie.mp4`)

If `Movie.mp4` already exists, it is skipped.

If the MKV does not contain Dolby Vision metadata, it is skipped.

Intermediate files are written to a temporary directory and discarded.

The script processes one file at a time (low memory footprint).

---

## Dolby Vision Support

Primarily tested with:
- DV Profile 8 (single-layer DV)

Profile 7 FEL titles may fall back depending on the playback device and display chain.

---

## How to verify Dolby Vision is preserved

After conversion:

```
ffprobe -hide_banner -loglevel error -show_streams -print_format json "Movie.mp4"
```

Look for, in the video stream:

```
"side_data_type": "DOVI configuration record"
```

---

# License
MIT

MIT
