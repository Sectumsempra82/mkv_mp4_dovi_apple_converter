# mkv_mp4_dovi_apple_converter

Batch-convert **Dolby Vision MKV** files into **Apple-friendly MP4** files so they play in the **Apple TV app / QuickTime** with Dolby Vision processing.

This does **not** re-encode the video stream. It:
1) extracts the HEVC bitstream from the MKV (copy),
2) converts the first audio track to AAC (for Apple compatibility),
3) muxes video+audio into MP4 using **GPAC MP4Box** (keeps Dolby Vision signaling),
4) deletes intermediate files,
5) processes one file at a time,
6) skips files when output already exists,
7) skips MKVs that are not Dolby Vision.

## Why this exists

`ffmpeg -c copy` MKV→MP4 often drops Dolby Vision signaling, so Apple apps treat the result as HDR10 only. MP4Box is typically much more reliable for preserving DV signaling.

## Requirements

### Python
- Python 3.9+ (3.10+ recommended)

### External tools (required)
- `ffmpeg` (includes `ffprobe`)
- `MP4Box` (from GPAC)

> `pip install` alone is not enough because `ffmpeg` and `MP4Box` are native binaries.

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

##### Chocolatey:
```
choco install ffmpeg gpac
```

##### Scoop:
```
scoop install ffmpeg gpac
```

After installing on Windows, open a new terminal so PATH updates apply.

## Usage

Put the script in the folder containing your MKV files:
```
batch_dovi_mp4box.py
```

### Run it:
```
python3 batch_dovi_mp4box.py
```

### Behavior

Input: *.mkv in the current folder

Output: same name, .mp4 extension (e.g., Movie.mkv → Movie.mp4)

If Movie.mp4 already exists, it is skipped

If the MKV does not contain Dolby Vision metadata, it is skipped

Intermediate files are written to a temporary directory and discarded

## Notes / Gotchas

Audio: the script converts the first audio track to AAC (384k) to improve Apple compatibility.
If the MKV has no audio track, it produces a video-only MP4.

Subtitles: subtitles are not copied (by design). Apple players are picky; keep it simple.

Dolby Vision: this is aimed primarily at DV Profile 8 (and similar single-layer DV). Some DV Profile 7 FEL titles may still fall back depending on devices/players.

Large files: no problem — the script processes one file at a time and does not load video into memory.

## How to verify Dolby Vision is preserved

After conversion:
```
ffprobe -hide_banner -loglevel error -show_streams -print_format json "Movie.mp4"
```
Look for, in the video stream:
```
side_data_type: "DOVI configuration record"
```

# License

MIT
