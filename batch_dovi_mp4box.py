#!/usr/bin/env python3
"""
Batch convert Dolby Vision MKV -> Apple-friendly MP4 (Apple TV app / QuickTime DV)
using:
  - ffprobe: detect Dolby Vision + enumerate first audio + subtitle streams
  - ffmpeg: extract HEVC (copy), copy original first audio stream (no re-encode) when possible,
            always create AAC stereo fallback, convert each supported subtitle stream to mov_text (tx3g) inside MP4
  - MP4Box: mux (keeps DV signaling reliably)

Behavior:
  - Processes *.mkv in current folder
  - Skips if output .mp4 exists
  - Skips if input has no Dolby Vision metadata
  - Processes one file at a time
  - Deletes intermediates (temp dir)

Audio behavior (first audio stream only):
  - Always produce AAC stereo fallback from the first audio stream.
  - Also try to copy original first audio stream to an elementary file (no re-encode).
  - Track ordering:
      * If source codec is eac3 or ac3 and copy succeeded: original is audio #1, AAC stereo is audio #2
      * Otherwise: AAC stereo is audio #1, original copied track is audio #2 (if copy succeeded)

Subtitles:
  - For each subtitle stream:
      * If text-based and convertible (SubRip/SRT, WebVTT, ASS/SSA, mov_text, TTML):
          convert to a tiny MP4 containing a single mov_text (tx3g) subtitle track:
            ffmpeg -map 0:s:N -c:s mov_text subN.mp4
          then MP4Box adds subN.mp4 (Apple TV / QuickTime compatible, selectable per track)
      * If image-based (PGS/VobSub/DVD):
          skipped (not supported yet)
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Sequence


@dataclass(frozen=True)
class Tooling:
    ffmpeg: str
    ffprobe: str
    mp4box: str


@dataclass(frozen=True)
class RunResult:
    returncode: int
    stdout: str
    stderr: str


def _is_windows() -> bool:
    return os.name == "nt" or platform.system().lower().startswith("win")


def _exe_name(base: str) -> str:
    return f"{base}.exe" if _is_windows() else base


def _install_hints() -> str:
    sysname: str = platform.system().lower()
    if sysname == "darwin":
        return "macOS:\n  brew install ffmpeg gpac\n"
    if sysname == "linux":
        return (
            "Linux (examples):\n"
            "  Debian/Ubuntu: sudo apt update && sudo apt install -y ffmpeg gpac\n"
            "  Fedora:        sudo dnf install -y ffmpeg gpac\n"
            "  Arch:          sudo pacman -S ffmpeg gpac\n"
        )
    if sysname.startswith("win"):
        return (
            "Windows (pick one):\n"
            "  winget install Gyan.FFmpeg\n"
            "  winget install GPAC.GPAC\n"
            "  (or) choco install ffmpeg gpac\n"
            "  (or) scoop install ffmpeg gpac\n"
            "\n"
            "After installing, open a NEW terminal so PATH updates are applied.\n"
        )
    return (
        "Install ffmpeg and GPAC (MP4Box) using your OS package manager.\n"
        "Then ensure both are in your PATH.\n"
    )


def which_or_none(name: str) -> Optional[str]:
    return shutil.which(name)


def ensure_tools_or_exit() -> Tooling:
    ffmpeg_name: str = _exe_name("ffmpeg")
    ffprobe_name: str = _exe_name("ffprobe")
    mp4box_name: str = _exe_name("MP4Box")

    ffmpeg_path: Optional[str] = which_or_none(ffmpeg_name)
    ffprobe_path: Optional[str] = which_or_none(ffprobe_name)
    mp4box_path: Optional[str] = which_or_none(mp4box_name)

    missing: list[str] = []
    if not ffmpeg_path:
        missing.append(ffmpeg_name)
    if not ffprobe_path:
        missing.append(ffprobe_name)
    if not mp4box_path:
        missing.append(mp4box_name)

    if missing:
        print("ERROR: Missing required tool(s) in PATH: " + ", ".join(missing), file=sys.stderr)
        print("\nInstall instructions:\n" + _install_hints(), file=sys.stderr)
        sys.exit(2)

    return Tooling(ffmpeg=ffmpeg_path, ffprobe=ffprobe_path, mp4box=mp4box_path)


def run_cmd(args: Sequence[str]) -> RunResult:
    proc = subprocess.run(
        list(args),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return RunResult(proc.returncode, proc.stdout, proc.stderr)


def run_cmd_or_raise(args: Sequence[str], context: str) -> None:
    res: RunResult = run_cmd(args)
    if res.returncode != 0:
        joined: str = " ".join(repr(a) for a in args)
        raise RuntimeError(
            f"{context}\n"
            f"Command failed (exit {res.returncode}):\n{joined}\n\n"
            f"STDOUT:\n{res.stdout}\n\nSTDERR:\n{res.stderr}\n"
        )


def ffprobe_json(tool: Tooling, src: Path) -> dict:
    res: RunResult = run_cmd(
        [
            tool.ffprobe,
            "-hide_banner",
            "-loglevel",
            "error",
            "-show_streams",
            "-print_format",
            "json",
            str(src),
        ]
    )
    if res.returncode != 0:
        raise RuntimeError(f"[ffprobe] failed on {src.name}\n\nSTDERR:\n{res.stderr}\n")
    return json.loads(res.stdout or "{}")


def has_dovi(tool: Tooling, src: Path) -> bool:
    data = ffprobe_json(tool, src)
    for s in data.get("streams", []):
        if s.get("codec_type") != "video":
            continue
        for sd in (s.get("side_data_list") or []):
            if sd.get("side_data_type") == "DOVI configuration record":
                return True
    return False


@dataclass(frozen=True)
class AudioStreamInfo:
    stream_index: int  # ffprobe absolute stream index (used as 0:<index>)
    codec_name: str


@dataclass(frozen=True)
class SubtitleStreamInfo:
    stream_index: int  # ffprobe absolute stream index
    codec_name: str
    language: str
    title: str


def first_audio_stream(tool: Tooling, src: Path) -> Optional[AudioStreamInfo]:
    data = ffprobe_json(tool, src)
    audio_streams = [s for s in data.get("streams", []) if s.get("codec_type") == "audio"]
    if not audio_streams:
        return None
    s0 = audio_streams[0]
    idx = s0.get("index")
    codec = s0.get("codec_name") or ""
    if not isinstance(idx, int) or not codec:
        return None
    return AudioStreamInfo(stream_index=int(idx), codec_name=str(codec).lower())


def subtitle_streams(tool: Tooling, src: Path) -> list[SubtitleStreamInfo]:
    data = ffprobe_json(tool, src)
    subs: list[SubtitleStreamInfo] = []
    for s in data.get("streams", []):
        if s.get("codec_type") != "subtitle":
            continue
        idx = s.get("index")
        codec = (s.get("codec_name") or "").lower()
        tags = s.get("tags") or {}
        lang = (tags.get("language") or "und").lower()
        title = str(tags.get("title") or "").strip()
        if isinstance(idx, int) and codec:
            subs.append(SubtitleStreamInfo(stream_index=int(idx), codec_name=codec, language=lang, title=title))
    return subs


def iter_input_files(folder: Path) -> Iterable[Path]:
    for ext in (".mkv", ".MKV"):
        yield from folder.glob(f"*{ext}")


def output_path_for(src: Path) -> Path:
    return src.with_suffix(".mp4")


def _copied_audio_extension(codec_name: str) -> str:
    if codec_name == "eac3":
        return "eac3"
    if codec_name == "ac3":
        return "ac3"
    if codec_name == "aac":
        return "aac"
    if codec_name == "mp3":
        return "mp3"
    if codec_name == "opus":
        return "opus"
    if codec_name == "flac":
        return "flac"
    if codec_name in ("truehd", "mlp"):
        return "truehd"
    if codec_name == "dts":
        return "dts"
    return codec_name or "audio"


_TEXT_SUB_CODECS = {
    # common text-based
    "subrip",   # srt
    "srt",
    "webvtt",
    "ass",
    "ssa",
    # mp4 text
    "mov_text",
    # some text-y variants
    "text",
    "ttml",
}

_IMAGE_SUB_CODECS = {
    "hdmv_pgs_subtitle",
    "pgs",
    "dvd_subtitle",
    "vobsub",
}


def _sanitize_lang(lang: str) -> str:
    l = (lang or "und").strip().lower()
    return l if l else "und"


def extract_subtitles_to_tx3g_mp4(
    tool: Tooling,
    src: Path,
    subs: list[SubtitleStreamInfo],
    tmp_dir: Path,
) -> list[tuple[Path, str]]:
    """
    For each supported text subtitle stream, create a tiny MP4 containing a single mov_text subtitle track.
    Returns list of (mp4_path, lang) to be added to MP4Box.
    """
    extracted: list[tuple[Path, str]] = []

    for i, s in enumerate(subs):
        lang = _sanitize_lang(s.language)

        if s.codec_name in _IMAGE_SUB_CODECS:
            print(f"INFO: skipping image-based subtitle stream {s.stream_index} ({s.codec_name}, {lang})")
            continue

        if s.codec_name not in _TEXT_SUB_CODECS:
            print(f"INFO: skipping unsupported subtitle codec stream {s.stream_index} ({s.codec_name}, {lang})")
            continue

        out_mp4 = tmp_dir / f"sub_{i}_{lang}.mp4"

        res = run_cmd(
            [
                tool.ffmpeg,
                "-hide_banner",
                "-y",
                "-i",
                str(src),
                "-map",
                f"0:{s.stream_index}",
                "-c:s",
                "mov_text",
                str(out_mp4),
            ]
        )

        if res.returncode == 0 and out_mp4.exists() and out_mp4.stat().st_size > 0:
            extracted.append((out_mp4, lang))
            title_hint = f", title='{s.title}'" if s.title else ""
            print(f"INFO: subtitle {s.stream_index} ({s.codec_name}, {lang}{title_hint}) -> {out_mp4.name}")
        else:
            print(
                f"WARN: failed to convert subtitle stream {s.stream_index} ({s.codec_name}, {lang}); skipping.\n"
                f"ffmpeg error:\n{res.stderr.strip()}\n"
            )

    return extracted


def convert_one(tool: Tooling, src: Path, dst: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="dovi_mp4box_") as tmp_dir_str:
        tmp_dir: Path = Path(tmp_dir_str)

        video_hevc: Path = tmp_dir / "video.hevc"
        audio_stereo_m4a: Optional[Path] = None
        audio_original: Optional[Path] = None
        a0: Optional[AudioStreamInfo] = None
        out_tmp: Path = tmp_dir / "out.mp4"

        # 1) Extract HEVC elementary stream (no re-encode)
        run_cmd_or_raise(
            [
                tool.ffmpeg,
                "-hide_banner",
                "-y",
                "-i",
                str(src),
                "-map",
                "0:v:0",
                "-c:v",
                "copy",
                "-bsf:v",
                "hevc_mp4toannexb",
                "-f",
                "hevc",
                str(video_hevc),
            ],
            context=f"[ffmpeg] extracting video from {src.name}",
        )

        # 2) Audio: first audio stream only
        a0 = first_audio_stream(tool, src)
        if a0 is None:
            print("WARN: no audio streams found; output will be video-only")
        else:
            # 2a) Always create AAC stereo fallback
            audio_stereo_m4a = tmp_dir / "audio_stereo.m4a"
            run_cmd_or_raise(
                [
                    tool.ffmpeg,
                    "-hide_banner",
                    "-y",
                    "-i",
                    str(src),
                    "-map",
                    f"0:{a0.stream_index}",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "256k",
                    "-ac",
                    "2",
                    str(audio_stereo_m4a),
                ],
                context=f"[ffmpeg] creating AAC stereo fallback from audio stream {a0.stream_index} ({a0.codec_name})",
            )

            # 2b) Copy original first audio track (no re-encode) if possible
            ext = _copied_audio_extension(a0.codec_name)
            audio_original = tmp_dir / f"audio_original.{ext}"
            res = run_cmd(
                [
                    tool.ffmpeg,
                    "-hide_banner",
                    "-y",
                    "-i",
                    str(src),
                    "-map",
                    f"0:{a0.stream_index}",
                    "-c:a",
                    "copy",
                    str(audio_original),
                ]
            )
            if res.returncode != 0 or not audio_original.exists() or audio_original.stat().st_size == 0:
                print(
                    "WARN: could not copy original audio track; keeping only AAC stereo.\n"
                    f"ffmpeg error:\n{res.stderr.strip()}\n"
                )
                audio_original = None

        # 3) Subtitles: convert all supported text subs to tx3g-in-mp4 and add each
        subs = subtitle_streams(tool, src)
        extracted_subs = extract_subtitles_to_tx3g_mp4(tool, src, subs, tmp_dir)

        # 4) Mux with MP4Box.
        # Audio track order rule:
        #   - if original codec is eac3/ac3 and copy succeeded -> original first, AAC stereo second
        #   - otherwise -> AAC stereo first, original second (if available)
        mp4box_args: list[str] = [tool.mp4box, "-add", str(video_hevc)]

        prefer_original_first = (
            a0 is not None
            and audio_original is not None
            and a0.codec_name in ("eac3", "ac3")
        )

        if prefer_original_first:
            mp4box_args += ["-add", str(audio_original)]
            if audio_stereo_m4a is not None:
                mp4box_args += ["-add", str(audio_stereo_m4a)]
            print(f"INFO: audio order = original {a0.codec_name} first, AAC stereo second")
        else:
            if audio_stereo_m4a is not None:
                mp4box_args += ["-add", str(audio_stereo_m4a)]
            if audio_original is not None:
                mp4box_args += ["-add", str(audio_original)]
                if a0 is not None:
                    print(f"INFO: audio order = AAC stereo first, original {a0.codec_name} second")
            elif audio_stereo_m4a is not None:
                print("INFO: audio = AAC stereo only")

        # Add subtitles after audio; set language so Apple UI labels them nicely
        for (sub_mp4, lang) in extracted_subs:
            mp4box_args += ["-add", f"{sub_mp4}:lang={lang}"]

        mp4box_args += ["-new", str(out_tmp)]

        run_cmd_or_raise(
            mp4box_args,
            context=f"[MP4Box] muxing to MP4 for {src.name}",
        )

        # 5) Move output into place
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            raise RuntimeError(f"Refusing to overwrite existing output: {dst}")
        out_tmp.replace(dst)


def main() -> int:
    folder: Path = Path(".").resolve()
    tooling: Tooling = ensure_tools_or_exit()

    inputs: list[Path] = sorted(iter_input_files(folder))
    if not inputs:
        print("No .mkv files found in this folder.")
        return 0

    converted: int = 0
    skipped_exists: int = 0
    skipped_no_dovi: int = 0
    failed: int = 0

    for src in inputs:
        dst: Path = output_path_for(src)
        if dst.exists():
            print(f"SKIP:  {src.name} -> {dst.name} (already exists)")
            skipped_exists += 1
            continue

        print(f"CHECK: {src.name} (Dolby Vision?)")
        try:
            if not has_dovi(tooling, src):
                print(f"SKIP:  {src.name} (no Dolby Vision detected)")
                skipped_no_dovi += 1
                continue
        except KeyboardInterrupt:
            print("\nInterrupted by user. Exiting.", file=sys.stderr)
            return 130
        except Exception as e:
            failed += 1
            print(f"FAIL: {src.name}\n{e}", file=sys.stderr)
            continue

        print(f"DO:    {src.name} -> {dst.name}")
        try:
            convert_one(tooling, src, dst)
            converted += 1
            print(f"OK:    {dst.name}")
        except KeyboardInterrupt:
            print("\nInterrupted by user. Exiting.", file=sys.stderr)
            return 130
        except Exception as e:
            failed += 1
            print(f"FAIL: {src.name}\n{e}", file=sys.stderr)

    print("\nSummary:")
    print(f"  Converted:        {converted}")
    print(f"  Skipped (exists): {skipped_exists}")
    print(f"  Skipped (no DV):  {skipped_no_dovi}")
    print(f"  Failed:           {failed}")

    if failed != 0 and _is_windows():
        print(
            "\nWindows note: if you just installed ffmpeg/GPAC, open a NEW terminal so PATH refreshes.",
            file=sys.stderr,
        )

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
