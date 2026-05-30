"""
Convert a vertical (9:16) video to 4:3 blurred-fill (1440x1080) using ffmpeg.

Layout:
  - Background: source scaled to COVER 1440x1080 (zoomed in, cropped), Gaussian-blurred
  - Foreground: source scaled to FIT inside 1440x1080 (letter-boxed, no crop)
  - Output: fg centred on blurred bg

Only called for vertical (portrait) source videos.
Horizontal videos are uploaded as-is — no conversion needed.

ffmpeg must be on PATH (installed in GitHub Actions ubuntu-latest by default).
"""

import subprocess
import logging
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

# Output frame dimensions (4:3 landscape)
OUT_W = 1440
OUT_H = 1080

# Default Gaussian blur strength (approved by operator 2026-05-29, increased to 40 on 2026-05-29)
DEFAULT_SIGMA = 40


def is_ffmpeg_available() -> bool:
    """Return True if ffmpeg is on PATH."""
    return shutil.which("ffmpeg") is not None


def trim_video(input_path: Path, output_path: Path, duration_seconds: int = 179) -> Path:
    """
    Trim video to duration_seconds using stream copy (no re-encode, very fast).
    Used by trim_dual mode: slot 2 trims slot 1's video to <3 min so YouTube treats it as a Short.
    Returns output_path on success, raises on failure.
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found on PATH")
    cmd = [ffmpeg, "-y", "-i", str(input_path), "-t", str(duration_seconds),
           "-c", "copy", str(output_path)]
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg trim failed: {result.stderr.decode()[:300]}")
    logger.info("[converter] Trimmed %s → %s (%ds)", input_path.name, output_path.name, duration_seconds)
    return output_path


def get_video_dimensions(path: Path) -> tuple:
    """
    Return (width, height) of a video file.
    Returns (0, 0) on error.

    Tries ffprobe first (fastest). Falls back to parsing ffmpeg -i stderr
    (ffprobe and ffmpeg ship together but PATH may differ on some systems).

    Used to reliably determine orientation AFTER download, since yt-dlp's
    extract_flat mode often omits width/height from profile metadata.
    """
    # ── Method 1: ffprobe ────────────────────────────────────────────────────
    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        try:
            result = subprocess.run(
                [
                    ffprobe, "-v", "error",
                    "-select_streams", "v:0",
                    "-show_entries", "stream=width,height",
                    "-of", "csv=p=0",
                    str(path),
                ],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode == 0 and result.stdout.strip():
                parts = result.stdout.strip().split(",")
                if len(parts) >= 2:
                    return (int(parts[0]), int(parts[1]))
        except Exception as exc:
            logger.debug("[converter] ffprobe error: %s", exc)

    # ── Method 2: ffmpeg -i (parses stderr for "NxM" in video stream line) ──
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        try:
            import re
            result = subprocess.run(
                [ffmpeg, "-i", str(path)],
                capture_output=True, text=True, timeout=30,
            )
            # ffmpeg always prints codec info to stderr even on error
            for line in (result.stdout + result.stderr).splitlines():
                if "Video:" in line:
                    m = re.search(r"(\d{2,5})x(\d{2,5})", line)
                    if m:
                        return (int(m.group(1)), int(m.group(2)))
        except Exception as exc:
            logger.debug("[converter] ffmpeg -i dimension parse error: %s", exc)

    logger.warning(
        "[converter] Could not probe dimensions of %s — assuming horizontal",
        path.name,
    )
    return (0, 0)


def is_vertical(path: Path) -> bool:
    """
    Return True if the video is portrait/vertical (height > width).
    Falls back to False on probe failure (safe default — skips conversion rather than crashing).
    """
    w, h = get_video_dimensions(path)
    result = h > w
    logger.debug("[converter] %s dimensions: %dx%d  vertical=%s", path.name, w, h, result)
    return result


def convert_to_4_3_blurred(
    input_path: Path,
    output_path: Path,
    sigma: int = DEFAULT_SIGMA,
) -> Path:
    """
    Convert a vertical video to 4:3 blurred-fill format.

    Args:
        input_path:  Path to source .mp4 (should be vertical/portrait)
        output_path: Desired output path for the converted .mp4
        sigma:       Gaussian blur strength (default 25)

    Returns:
        output_path on success.

    Raises:
        FileNotFoundError: if ffmpeg is not on PATH.
        RuntimeError:      if ffmpeg exits with a non-zero code or times out.
    """
    if not is_ffmpeg_available():
        raise FileNotFoundError(
            "ffmpeg not found on PATH. "
            "Install ffmpeg to use dual/longform_only upload modes."
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Filter graph:
    #  [bg] = source → scale to COVER 1440x1080 → crop → blur
    #  [fg] = source → scale to FIT inside 1440x1080 (no crop)
    #  composite: fg centred on blurred bg
    filter_complex = (
        f"[0:v]"
        f"scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=increase,"
        f"crop={OUT_W}:{OUT_H},"
        f"gblur=sigma={sigma}"
        f"[bg];"

        f"[0:v]"
        f"scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=decrease"
        f"[fg];"

        f"[bg][fg]overlay=(W-w)/2:(H-h)/2[out]"
    )

    cmd = [
        "ffmpeg", "-y",            # overwrite output without prompting
        "-i", str(input_path),
        "-filter_complex", filter_complex,
        "-map", "[out]",
        "-map", "0:a?",            # copy audio if present (optional stream)
        "-c:v", "libx264",
        "-crf", "18",              # high quality (visually lossless)
        "-preset", "medium",
        "-c:a", "copy",
        str(output_path),
    ]

    logger.info(
        "[converter] %s → %s  (sigma=%d, crf=18)",
        input_path.name, output_path.name, sigma,
    )

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=600,           # 10 min cap — large videos on slow runners
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"ffmpeg timed out (>10 min) converting {input_path.name}"
        )

    if result.returncode != 0:
        stderr_tail = (result.stderr or "")[-2000:]
        logger.error(
            "[converter] ffmpeg failed (code %d):\n%s",
            result.returncode, stderr_tail,
        )
        raise RuntimeError(
            f"ffmpeg conversion failed for {input_path.name} "
            f"(exit code {result.returncode})"
        )

    size_mb = output_path.stat().st_size / 1_048_576
    logger.info(
        "[converter] Done: %s  (%.1f MB)",
        output_path.name, size_mb,
    )
    return output_path
