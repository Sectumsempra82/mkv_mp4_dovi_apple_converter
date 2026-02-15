#!/usr/bin/env python3
"""
Batch convert Dolby Vision MKV -> Apple-friendly MP4 (Apple TV app / QuickTime DV)
using:
  - ffprobe: detect Dolby Vision + read audio codec
  - ffmpeg: extract HEVC (copy), optionally copy E-AC3/AC-3, and always create AAC stereo fallback
  - MP4Box: mux (keeps DV signaling reliably)

Behavior:
  - Processes *.mkv in current folder
  - Skips if output .mp4 exists
  - Skips if input has no Dolby Vision metadata
  - Processes one file at a time
  - Deletes intermediates (temp dir)

Audio logic (first audio stream only):
  - Always create AAC stereo (.m4a) from the first audio stream
  - If first audio codec is eac3 -> also copy that track to .eac3
  - Else if first audio codec is ac3 -> also copy that track to .ac3
  - Else if first audio codec is any other codec -> do NOT copy it (Apple reliability)
  - MP4Box mux order is ALWAYS:
      1) video
      2) AAC stereo (first audio track in MP4)
      3) (optional) E-AC3/AC-3 surround copied from source (second audio track)
This guarantees Apple apps pick the stereo track by default, while still preserving surround
when it's a good, Apple-friendly format.
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


def iter_input_files(folder: Path) -> Iterable[Path]:
    for ext in (".mkv", ".MKV"):
        yield from folder.glob(f"*{ext}")


def output_path_for(src: Path) -> Path:
    return src.with_suffix(".mp4")


def convert_one(tool: Tooling, src: Path, dst: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="dovi_mp4box_") as tmp_dir_str:
        tmp_dir: Path = Path(tmp_dir_str)

        video_hevc: Path = tmp_dir / "video.hevc"
        audio_stereo_m4a: Path = tmp_dir / "audio_stereo.m4a"
        audio_surround: Optional[Path] = None
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

        # 2) Audio: use FIRST audio stream only
        a0 = first_audio_stream(tool, src)
        if a0 is None:
            print("WARN: no audio streams found; output will be video-only")
            have_stereo = False
        else:
            # 2a) Always create AAC stereo fallback (this will be the FIRST audio track in MP4)
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
            have_stereo = True

            # 2b) If E-AC3 or AC-3, also copy it (surround track as SECOND audio track)
            if a0.codec_name == "eac3":
                audio_surround = tmp_dir / "audio_surround.eac3"
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
                        "copy",
                        str(audio_surround),
                    ],
                    context=f"[ffmpeg] copying E-AC3 surround audio stream {a0.stream_index}",
                )
                print("INFO: preserved surround as E-AC3 (second audio track)")
            elif a0.codec_name == "ac3":
                audio_surround = tmp_dir / "audio_surround.ac3"
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
                        "copy",
                        str(audio_surround),
                    ],
                    context=f"[ffmpeg] copying AC-3 surround audio stream {a0.stream_index}",
                )
                print("INFO: preserved surround as AC-3 (second audio track)")
            else:
                # Do not copy other codecs (DTS/TrueHD/FLAC/Opus/AAC multichannel etc.)
                print(f"INFO: source audio codec '{a0.codec_name}' not copied (Apple reliability). Using AAC stereo only.")

        # 3) Mux with MP4Box.
        # Order matters: video first, then AAC stereo, then surround (if any).
        mp4box_args: list[str] = [tool.mp4box, "-add", str(video_hevc)]

        if have_stereo:
            mp4box_args += ["-add", str(audio_stereo_m4a)]

        if audio_surround is not None and audio_surround.exists() and audio_surround.stat().st_size > 0:
            mp4box_args += ["-add", str(audio_surround)]

        mp4box_args += ["-new", str(out_tmp)]

        run_cmd_or_raise(
            mp4box_args,
            context=f"[MP4Box] muxing to MP4 for {src.name}",
        )

        # 4) Move output into place
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
