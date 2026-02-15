#!/usr/bin/env python3
"""
Batch convert MKV -> Apple-friendly MP4 preserving Dolby Vision (DV Profile 8 etc.)
via:
  1) Extract HEVC elementary stream with ffmpeg (no video re-encode)
  2) Convert first audio track to AAC M4A (Apple-friendly)
  3) Mux with GPAC MP4Box (more reliable DV signaling than ffmpeg remux)
  4) Discard intermediates (temp dir), process one file at a time
Skips if output file already exists.
Skips MKVs that do NOT contain Dolby Vision (checks via ffprobe for DOVI config record).

Cross-platform notes:
- This script is pure Python, but REQUIRES external binaries:
    - ffmpeg
    - ffprobe (ships with ffmpeg)
    - MP4Box (from GPAC)
  pip alone cannot provide them reliably.
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
        print(
            "ERROR: Missing required tool(s) in PATH: " + ", ".join(missing),
            file=sys.stderr,
        )
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


def iter_input_files(folder: Path) -> Iterable[Path]:
    for ext in (".mkv", ".MKV"):
        yield from folder.glob(f"*{ext}")


def output_path_for(src: Path) -> Path:
    return src.with_suffix(".mp4")


def has_dovi(tool: Tooling, src: Path) -> bool:
    # True if any video stream exposes "DOVI configuration record"
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
        raise RuntimeError(
            f"[ffprobe] failed on {src.name}\n\nSTDERR:\n{res.stderr}\n"
        )

    data = json.loads(res.stdout or "{}")
    streams = data.get("streams", [])
    for s in streams:
        if s.get("codec_type") != "video":
            continue
        for sd in (s.get("side_data_list") or []):
            if sd.get("side_data_type") == "DOVI configuration record":
                return True
    return False


def convert_one(tool: Tooling, src: Path, dst: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="dovi_mp4box_") as tmp_dir_str:
        tmp_dir: Path = Path(tmp_dir_str)

        video_hevc: Path = tmp_dir / "video.hevc"
        audio_m4a: Path = tmp_dir / "audio.m4a"
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

        # 2) Convert first audio stream to AAC/M4A for Apple compatibility.
        # If there's no audio stream, continue video-only.
        have_audio: bool = True
        audio_res: RunResult = run_cmd(
            [
                tool.ffmpeg,
                "-hide_banner",
                "-y",
                "-i",
                str(src),
                "-map",
                "0:a:0",
                "-c:a",
                "aac",
                "-b:a",
                "384k",
                str(audio_m4a),
            ]
        )
        if audio_res.returncode != 0:
            have_audio = False

        # 3) Mux with MP4Box
        mp4box_args: list[str] = [tool.mp4box]
        mp4box_args += ["-add", str(video_hevc)]
        if have_audio:
            mp4box_args += ["-add", str(audio_m4a)]
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
            print(f"SKIP: {src.name} -> {dst.name} (already exists)")
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