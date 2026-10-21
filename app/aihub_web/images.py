"""Making images: checks on what the engine returns, one lock for the GPU, and jobs that outlive their request."""

import re
import struct
from collections.abc import Sequence

# Shapes are all 1024-class: the same pixel count as the 768 px square for Wide and Tall.
SHAPES = {"square": (1024, 1024), "wide": (1024, 576), "tall": (576, 1024)}
MAX_PROMPT_CHARS = 2000
MAX_IMAGE_BYTES = 8 * 1024 * 1024
IMAGE_STORE_CAP = 2 * 1024**3

# What the user is told, per error code. The wording is the one the UI is written against.
MESSAGES = {
    "vram_busy": "The GPU is short of memory ({free} MB free). Close GPU-heavy apps or choose Wide or Tall, then retry.",
    "vram_busy_unknown": "The GPU is short of memory. Close GPU-heavy apps or choose Wide or Tall, then retry.",
    "cuda_unavailable": "The GPU didn't respond. If this keeps happening, restart the PC.",
}


class ImageError(Exception):
    """An image could not be made. `code` picks what the UI offers next; `message` is shown as written."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _plain_webp_error(reason: str) -> ImageError:
    return ImageError(
        "bad_output",
        f"The image engine returned a file that isn't a plain WebP image ({reason}).",
    )


def validate_webp(data: bytes, width: int, height: int) -> bytes:
    """Return `data` if it is exactly what a lossy, metadata-free WebP of this size looks like.

    The engine's output is untrusted. The only shape accepted is RIFF/WEBP holding a single `VP8 ` chunk
    whose frame is a visible key frame of the requested size, with nothing after it: no VP8X header, no
    XMP (which would carry the prompt), EXIF or ICC chunk, no alpha, no animation.
    """
    if len(data) > MAX_IMAGE_BYTES:
        raise _plain_webp_error("it is larger than 8 MiB")
    if len(data) < 30 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        raise _plain_webp_error("it is not a RIFF/WebP file")
    claimed = struct.unpack_from("<I", data, 4)[0] + 8
    if claimed < len(data):
        raise _plain_webp_error("it has trailing bytes after the image")
    if claimed > len(data):
        raise _plain_webp_error("it is shorter than its header says")
    tag = data[12:16]
    if tag != b"VP8 ":
        what = (
            "extended header, which means metadata or an alpha channel"
            if tag == b"VP8X"
            else "not a lossy VP8 image"
        )
        raise _plain_webp_error(
            f"it starts with a {tag.decode('ascii', 'replace')!r} chunk, {what}"
        )
    size = struct.unpack_from("<I", data, 16)[0]
    if 20 + size + (size & 1) != len(data):
        raise _plain_webp_error("it has more than one chunk, such as embedded metadata")
    # The 10-byte frame header, as libwebp's VP8GetInfo reads it.
    frame = data[20 : 20 + size]
    if len(frame) < 10 or frame[3:6] != b"\x9d\x01\x2a":
        raise _plain_webp_error("the VP8 frame header is damaged")
    flags = frame[0] | frame[1] << 8 | frame[2] << 16
    key_frame, profile, visible, first_partition = (
        not flags & 1,
        flags >> 1 & 7,
        flags >> 4 & 1,
        flags >> 5,
    )
    if not (key_frame and profile <= 3 and visible and first_partition < size):
        raise _plain_webp_error("the VP8 frame is not a visible key frame")
    # Whole 16-bit fields are compared: their two high bits are an upscaling hint that is never set.
    found = struct.unpack_from("<HH", frame, 6)
    if found != (width, height):
        raise _plain_webp_error(
            f"its dimensions are {found[0] & 0x3FFF}x{found[1] & 0x3FFF}, not {width}x{height}"
        )
    return data


_FREE_MB = re.compile(
    r"cannot make enough memory available on \S+: need [\d.]+ MB device"
    r"(?: / [\d.]+ MB budget)?, available ([\d.]+) MB device"
)
_NO_GPU = re.compile(
    r"busy or unavailable|no CUDA-capable device|failed to initialize CUDA"
    r"|CUDA driver version is insufficient",
    re.IGNORECASE,
)
_OUT_OF_MEMORY = re.compile(r"out of memory", re.IGNORECASE)
_LOG_TAIL = re.compile(r"\s+---\s+\S+:\d+\s*$")
_PREFIX = {
    "engine_failed": "The image engine reported an error",
    "engine_failed_to_start": "The image engine couldn't start",
}


def _quote(line: str) -> str:
    """An engine log line as a sentence fragment: no `[E]` tag, no `--- file.cpp:123` tail, cut short."""
    text = " ".join(_LOG_TAIL.sub("", line.removeprefix("[E]")).split())
    return text if len(text) <= 200 else text[:199].rstrip() + "…"


def classify(
    last_error: Sequence[object], default: str = "engine_failed"
) -> tuple[str, str]:
    """The error code and message for the engine's last [E]/[W] log lines.

    A GPU that refuses CUDA is told apart from one that is merely short of memory (the warning that
    names how much was free is the only place that figure appears). Warnings never count as the error
    to quote, and the "GPU is busy" signature is only believed in error lines: a start that failed on
    its model files logs it as a warning too. Anything else gets `default` and the first error line.
    """
    lines = [line for line in last_error if isinstance(line, str)]
    errors = [line for line in lines if not line.startswith("[W]")]
    if any(_NO_GPU.search(line) for line in errors):
        return "cuda_unavailable", MESSAGES["cuda_unavailable"]
    for line in lines:
        if found := _FREE_MB.search(line):
            return "vram_busy", MESSAGES["vram_busy"].format(
                free=round(float(found[1]))
            )
    if any(_OUT_OF_MEMORY.search(line) for line in errors):
        return "vram_busy", MESSAGES["vram_busy_unknown"]
    quote = _quote(errors[0]) if errors else ""
    detail = f": {quote}{'' if quote[-1:] in '.…!?' else '.'}" if quote else "."
    return default, _PREFIX[default] + detail
