"""Image generation: the WebP check, failure classification, the engine client, the GPU lock and the jobs."""

import struct

import pytest
from aihub_web.images import MAX_IMAGE_BYTES, ImageError, classify, validate_webp
from helpers import FIXTURES, SHAPE_SIZES, engine_errors, webp


def chunk(fourcc: bytes, payload: bytes) -> bytes:
    """A RIFF chunk: tag, little-endian size, payload, and a pad byte when the size is odd."""
    pad = b"\0" * (len(payload) & 1)
    return fourcc + struct.pack("<I", len(payload)) + payload + pad


def riff(*chunks: bytes) -> bytes:
    body = b"WEBP" + b"".join(chunks)
    return b"RIFF" + struct.pack("<I", len(body)) + body


def with_riff_size(data: bytes, size: int) -> bytes:
    return data[:4] + struct.pack("<I", size) + data[8:]


def vp8_frame(shape: str = "square") -> bytes:
    """The frame inside a fixture's single VP8 chunk."""
    data = webp(shape)
    return data[20 : 20 + struct.unpack("<I", data[16:20])[0]]


def refused(data: bytes, size: tuple[int, int] = (1024, 1024)) -> ImageError:
    with pytest.raises(ImageError) as err:
        validate_webp(data, *size)
    assert err.value.code == "bad_output"
    assert err.value.message.startswith(
        "The image engine returned a file that isn't a plain WebP image ("
    )
    return err.value


class TestValidateWebp:
    @pytest.mark.parametrize("shape", SHAPE_SIZES)
    def test_a_plain_lossy_webp_of_the_requested_size_passes_unchanged(
        self, shape: str
    ) -> None:
        data = webp(shape)
        assert validate_webp(data, *SHAPE_SIZES[shape]) is data

    def test_what_the_encoder_writes_without_metadata_passes(self) -> None:
        """The lab's sd-server output carries a VP8X header and an XMP chunk holding the prompt, because
        that request left embed_image_metadata on. The encoder alone writes the VP8 chunk in a bare RIFF."""
        real = (FIXTURES / "images" / "lab-engine-output-with-xmp.webp").read_bytes()
        assert real[30:34] == b"VP8 "
        size = struct.unpack("<I", real[34:38])[0]
        bare = riff(chunk(b"VP8 ", real[38 : 38 + size]))
        assert validate_webp(bare, 512, 512) == bare

    def test_the_same_output_with_its_metadata_is_refused(self) -> None:
        real = (FIXTURES / "images" / "lab-engine-output-with-xmp.webp").read_bytes()
        assert real[12:16] == b"VP8X"
        assert "VP8X" in refused(real, (512, 512)).message

    @pytest.mark.parametrize("tag", [b"XMP ", b"EXIF", b"ICCP"])
    def test_metadata_chunks_after_the_image_are_refused(self, tag: bytes) -> None:
        data = riff(chunk(b"VP8 ", vp8_frame()), chunk(tag, b"<prompt text>"))
        assert "more than one chunk" in refused(data).message

    def test_a_vp8x_header_is_refused_even_with_no_metadata_behind_it(self) -> None:
        header = chunk(b"VP8X", b"\0\0\0\0" + b"\xff\x03\x00" + b"\xff\x03\x00")
        data = riff(header, chunk(b"VP8 ", vp8_frame()))
        assert "VP8X" in refused(data).message

    @pytest.mark.parametrize("tag", [b"VP8L", b"ALPH", b"ANIM"])
    def test_lossless_alpha_and_animated_files_are_refused(self, tag: bytes) -> None:
        assert tag.decode() in refused(riff(chunk(tag, vp8_frame()))).message

    def test_trailing_bytes_are_refused(self) -> None:
        assert "trailing bytes" in refused(webp() + b"\0").message
        assert (
            "trailing bytes" in refused(webp() + riff(chunk(b"VP8 ", b"x" * 8))).message
        )

    @pytest.mark.parametrize("delta", [-100, -2, -1])
    def test_a_riff_size_smaller_than_the_file_leaves_trailing_bytes(
        self, delta: int
    ) -> None:
        data = webp()
        size = struct.unpack("<I", data[4:8])[0]
        assert "trailing bytes" in refused(with_riff_size(data, size + delta)).message

    @pytest.mark.parametrize("delta", [1, 2, 100])
    def test_a_riff_size_larger_than_the_file_means_it_was_cut_short(
        self, delta: int
    ) -> None:
        data = webp()
        size = struct.unpack("<I", data[4:8])[0]
        assert (
            "shorter than its header"
            in refused(with_riff_size(data, size + delta)).message
        )

    def test_a_truncated_file_is_refused(self) -> None:
        data = webp()
        assert "shorter than its header" in refused(data[:-1]).message
        assert "shorter than its header" in refused(data[: len(data) // 2]).message

    def test_a_chunk_size_that_does_not_fill_the_file_is_refused(self) -> None:
        data = webp()
        size = struct.unpack("<I", data[16:20])[0]
        shrunk = data[:16] + struct.pack("<I", size - 2) + data[20:]
        assert "more than one chunk" in refused(shrunk).message

    @pytest.mark.parametrize("shape", ["wide", "tall"])
    def test_other_dimensions_than_requested_are_refused(self, shape: str) -> None:
        width, height = SHAPE_SIZES[shape]
        message = refused(webp(shape)).message
        assert f"dimensions are {width}x{height}, not 1024x1024" in message

    def test_a_square_image_is_not_a_wide_one(self) -> None:
        message = refused(webp("square"), SHAPE_SIZES["wide"]).message
        assert "dimensions are 1024x1024, not 1024x576" in message

    def test_the_scale_bits_of_the_size_fields_must_be_zero(self) -> None:
        frame = bytearray(vp8_frame())
        frame[7] |= 0x40  # the two high bits of the width field are an upscaling hint
        assert "dimensions" in refused(riff(chunk(b"VP8 ", bytes(frame)))).message

    def test_the_frame_must_be_a_visible_key_frame_with_the_start_code(self) -> None:
        good = vp8_frame()
        broken = {
            "inter frame": bytes([good[0] | 1]) + good[1:],
            "hidden frame": bytes([good[0] & ~0x10]) + good[1:],
            "start code": good[:3] + b"\x9d\x01\x2b" + good[6:],
            "profile 4": bytes([(good[0] & ~0x0E) | (4 << 1)]) + good[1:],
            "too short": good[:9],
        }
        for what, frame in broken.items():
            assert "VP8 frame" in refused(riff(chunk(b"VP8 ", frame))).message, what

    def test_a_first_partition_longer_than_the_chunk_is_refused(self) -> None:
        good = vp8_frame()
        flags = (good[0] | good[1] << 8 | good[2] << 16) & 0x1F
        frame = (flags | len(good) << 5).to_bytes(3, "little") + good[3:]
        assert "VP8 frame" in refused(riff(chunk(b"VP8 ", frame))).message

    @pytest.mark.parametrize(
        "data", [b"", b"RIFF", b"RIFF\0\0\0\0WEBP", b"\x89PNG\r\n\x1a\n" * 8]
    )
    def test_things_that_are_not_webp_are_refused(self, data: bytes) -> None:
        assert "not a RIFF/WebP file" in refused(data).message

    @pytest.mark.parametrize("at, tag", [(0, b"RIFX"), (0, b"RF64"), (8, b"WAVE")])
    def test_another_container_or_form_type_is_refused(
        self, at: int, tag: bytes
    ) -> None:
        data = webp()
        assert (
            "not a RIFF/WebP file" in refused(data[:at] + tag + data[at + 4 :]).message
        )

    def test_an_odd_sized_chunk_needs_its_pad_byte_counted_in_the_riff_size(
        self,
    ) -> None:
        odd = vp8_frame() + b"\0"
        assert len(odd) % 2 == 1
        padded = riff(chunk(b"VP8 ", odd))
        assert validate_webp(padded, 1024, 1024) == padded
        unpadded = with_riff_size(padded[:-1], struct.unpack("<I", padded[4:8])[0] - 1)
        assert refused(unpadded).code == "bad_output"

    def test_a_file_over_the_cap_is_refused_before_it_is_read(self) -> None:
        big = webp() + b"\0" * (MAX_IMAGE_BYTES - len(webp()) + 1)
        assert len(big) == MAX_IMAGE_BYTES + 1
        assert "larger than 8 MiB" in refused(big).message

    def test_the_message_never_repeats_the_bytes(self) -> None:
        secret = b"prompt-text-that-must-not-be-echoed"
        data = riff(chunk(b"VP8 ", vp8_frame()), chunk(b"XMP ", secret))
        assert secret.decode() not in refused(data).message


class TestClassify:
    def test_a_gpu_short_of_memory_reports_how_much_it_had(self) -> None:
        assert classify(engine_errors("vram_short")) == (
            "vram_busy",
            (
                "The GPU is short of memory (950 MB free). Close GPU-heavy apps or choose "
                "Wide or Tall, then retry."
            ),
        )

    def test_the_figure_is_the_one_the_engine_logged_for_that_run(self) -> None:
        code, message = classify(engine_errors("vram_short_other_model"))
        assert code == "vram_busy" and "(499 MB free)" in message

    def test_the_figure_is_rounded_not_cut_and_a_budget_does_not_confuse_it(
        self,
    ) -> None:
        """available 942.75 MB device / 452.31 MB budget"""
        code, message = classify(engine_errors("vram_short_with_budget"))
        assert code == "vram_busy" and "(943 MB free)" in message

    def test_a_gpu_that_refuses_cuda_is_unavailable_not_short_of_memory(self) -> None:
        assert classify(engine_errors("gpu_busy_or_unavailable")) == (
            "cuda_unavailable",
            "The GPU didn't respond. If this keeps happening, restart the PC.",
        )

    def test_cuda_running_out_of_memory_is_vram_without_a_figure(self) -> None:
        code, message = classify(engine_errors("vae_out_of_memory_abort"))
        assert code == "vram_busy"
        assert message.startswith("The GPU is short of memory. Close GPU-heavy apps")

    def test_a_start_failure_names_the_first_error_line(self) -> None:
        """The warnings in this log say the GPU was unreachable too, but the error is the model file."""
        assert classify(
            engine_errors("model_files_wrong"), default="engine_failed_to_start"
        ) == (
            "engine_failed_to_start",
            (
                "The image engine couldn't start: Conditioner model tensor "
                "'text_encoders.llm.model.embed_tokens.weight' not in model metadata."
            ),
        )

    def test_the_default_applies_to_anything_unrecognised(self) -> None:
        lines = engine_errors("wrong_vae")
        code, message = classify(lines)
        assert code == "engine_failed"
        assert message.startswith("The image engine reported an error: VAE tensor")
        assert classify(lines, default="engine_failed_to_start")[0] == (
            "engine_failed_to_start"
        )

    def test_a_warning_alone_is_not_an_error_to_quote(self) -> None:
        assert classify(engine_errors("vae_retry_warning")) == (
            "engine_failed",
            "The image engine reported an error.",
        )

    def test_no_lines_at_all(self) -> None:
        assert classify([]) == ("engine_failed", "The image engine reported an error.")
        assert classify([], default="engine_failed_to_start") == (
            "engine_failed_to_start",
            "The image engine couldn't start.",
        )

    def test_lines_are_quoted_without_their_log_decoration(self) -> None:
        _, message = classify(["[E] generate failed --- main.cpp:940"])
        assert message == "The image engine reported an error: generate failed."

    def test_a_long_line_is_cut(self) -> None:
        _, message = classify(["[E] " + "x" * 5000])
        assert len(message) < 400 and message.endswith("…")

    def test_only_strings_are_read(self) -> None:
        assert classify([None, 7, {"a": 1}])[0] == "engine_failed"
