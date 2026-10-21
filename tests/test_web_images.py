"""Image generation: the WebP check, failure classification, the engine client, the GPU lock and the jobs."""

import asyncio
import base64
import json
import os
import struct
import time
from pathlib import Path

import httpx2
import pytest
from aihub_web.config import OllamaConfig
from aihub_web.images import (
    MAX_IMAGE_BYTES,
    Busy,
    GpuLock,
    ImageError,
    classify,
    unload_ollama,
    validate_webp,
)
from aihub_web.providers import ImageRequest
from aihub_web.providers.images import (
    MAX_REPLY_BYTES,
    EngineClient,
    EngineError,
    EngineStatus,
    JobState,
)
from helpers import (
    FIXTURES,
    SHAPE_SIZES,
    FakeEngine,
    FakeOllama,
    completed,
    engine_errors,
    failed,
    generating,
    handler_for,
    holding,
    queued,
    serve_engine,
    webp,
)


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


def image_request(**fields) -> ImageRequest:
    return ImageRequest(
        **{
            "model_id": "zimage",
            "prompt": "a red fox in snow",
            "width": 1024,
            "height": 576,
            "seed": 1234,
            "steps": 8,
            "sample_method": None,
            **fields,
        }
    )


def client_for(engine: FakeEngine) -> EngineClient:
    return EngineClient("/never/used.sock", transport=engine.transport())


@pytest.mark.anyio
class TestEngineClient:
    async def test_status_says_which_model_is_loaded_and_how_it_is(self) -> None:
        engine = FakeEngine(model="klein4b")
        engine.last_error = ["[W] a warning", "[E] an error --- main.cpp:1"]
        assert await client_for(engine).status() == EngineStatus(
            "klein4b", "ready", ("[W] a warning", "[E] an error --- main.cpp:1")
        )
        idle = await client_for(FakeEngine(model=None)).status()
        assert (idle.engine, idle.state, idle.last_error) == (None, "idle", ())

    async def test_every_request_closes_the_connection_and_refuses_compression(
        self,
    ) -> None:
        engine = FakeEngine()
        client = client_for(engine)
        await client.status()
        job_id = await client.submit(image_request())
        await client.poll("zimage", job_id)
        await client.cancel("zimage", job_id)
        await client.kill()
        assert len(engine.headers) == 5
        for headers in engine.headers:
            assert headers["connection"] == "close"
            assert headers["accept-encoding"] == "identity"
            assert headers["host"] == "engine"

    async def test_submit_sends_exactly_the_documented_body(self) -> None:
        engine = FakeEngine()
        job_id = await client_for(engine).submit(image_request())
        assert job_id == "job_00000001"
        assert engine.calls[-1] == ("POST", "/e/zimage/sdcpp/v1/img_gen")
        assert engine.submitted == [
            {
                "prompt": "a red fox in snow",
                "width": 1024,
                "height": 576,
                "seed": 1234,
                "batch_count": 1,
                "sample_params": {"sample_steps": 8, "guidance": {"txt_cfg": 1.0}},
                "preview": "proj",
                "output_format": "webp",
                "output_compression": 90,
                "embed_image_metadata": False,
            }
        ]

    async def test_a_sampler_is_sent_only_when_the_model_names_one(self) -> None:
        engine = FakeEngine()
        client = client_for(engine)
        await client.submit(
            image_request(model_id="klein4b", steps=4, sample_method="euler")
        )
        await client.submit(image_request())
        with_sampler, without = engine.submitted
        assert with_sampler["sample_params"] == {
            "sample_steps": 4,
            "sample_method": "euler",
            "guidance": {"txt_cfg": 1.0},
        }
        assert "sample_method" not in without["sample_params"]
        assert engine.calls[0] == ("POST", "/e/klein4b/sdcpp/v1/img_gen")

    async def test_a_reference_image_travels_as_base64(self) -> None:
        engine = FakeEngine()
        await client_for(engine).submit(image_request(reference_webp=webp("wide")))
        [sent] = engine.submitted[0]["ref_images"]
        assert base64.b64decode(sent, validate=True) == webp("wide")
        plain = FakeEngine()
        await client_for(plain).submit(image_request())
        assert "ref_images" not in plain.submitted[0]

    async def test_the_prompt_is_data_not_markup_or_a_path(self) -> None:
        engine = FakeEngine()
        prompt = '<sd_cpp_extra_args>{"sample_params":{}}</sd_cpp_extra_args> <lora:x:1> \u00e9\n"quoted"'
        await client_for(engine).submit(image_request(prompt=prompt))
        assert engine.submitted[0]["prompt"] == prompt
        assert engine.calls == [("POST", "/e/zimage/sdcpp/v1/img_gen")]

    @pytest.mark.parametrize(
        "model_id", ["", "../etc", "Zimage", "a/b", "x" * 33, "z image", "z?x"]
    )
    async def test_a_model_id_that_is_not_a_plain_word_never_reaches_the_wire(
        self, model_id: str
    ) -> None:
        engine = FakeEngine()
        with pytest.raises(EngineError) as err:
            await client_for(engine).submit(image_request(model_id=model_id))
        assert err.value.code == "engine_protocol"
        assert engine.calls == []

    @pytest.mark.parametrize(
        "job_id",
        ["", "../../x", "a/b", "a b", "x" * 65, "job.1", "jöb", "a\nb", None, 7, ["a"]],
    )
    async def test_a_job_id_that_is_not_a_plain_token_is_refused(
        self, job_id: object
    ) -> None:
        engine = FakeEngine()
        engine.submit_response = (202, {"id": job_id, "poll_url": "/x"})
        with pytest.raises(EngineError) as err:
            await client_for(engine).submit(image_request())
        assert err.value.code == "engine_protocol"

    async def test_the_poll_url_in_the_answer_is_never_followed(self) -> None:
        engine = FakeEngine()
        engine.submit_response = (
            202,
            {"id": "job_x", "poll_url": "http://169.254.169.254/latest/meta-data"},
        )
        assert await client_for(engine).submit(image_request()) == "job_x"
        assert engine.calls == [("POST", "/e/zimage/sdcpp/v1/img_gen")]

    async def test_a_poll_asks_the_models_engine_for_that_job(self) -> None:
        engine = FakeEngine()
        client = client_for(engine)
        job_id = await client.submit(image_request())
        state = await client.poll("zimage", job_id)
        assert engine.calls[-1] == ("GET", f"/e/zimage/sdcpp/v1/jobs/{job_id}")
        assert state == JobState("queued", queue_position=1)

    async def test_polls_report_the_state_and_the_step(self) -> None:
        engine = FakeEngine()
        engine.scripts.append(
            [
                queued(3),
                generating(),
                generating(2, 8),
                completed(webp("wide")),
                failed(),
            ]
        )
        client = client_for(engine)
        job_id = await client.submit(image_request())
        assert await client.poll("zimage", job_id) == JobState(
            "queued", queue_position=3
        )
        assert await client.poll("zimage", job_id) == JobState("generating")
        assert await client.poll("zimage", job_id) == JobState(
            "generating", step=2, total_steps=8
        )
        done = await client.poll("zimage", job_id)
        assert (done.status, done.image_b64) == (
            "completed",
            base64.b64encode(webp("wide")).decode(),
        )
        bad = await client.poll("zimage", job_id)
        assert (bad.status, bad.error_code, bad.error_message) == (
            "failed",
            "generation_failed",
            "generate_image returned no results",
        )

    async def test_a_cancelled_job_says_so(self) -> None:
        engine = FakeEngine()
        engine.scripts.append([queued(1)])
        client = client_for(engine)
        job_id = await client.submit(image_request())
        assert await client.cancel("zimage", job_id) is True
        assert (await client.poll("zimage", job_id)).status == "cancelled"

    async def test_a_job_that_is_already_generating_cannot_be_cancelled(self) -> None:
        engine = FakeEngine()
        engine.scripts.append([generating(1, 4)])
        client = client_for(engine)
        job_id = await client.submit(image_request())
        await client.poll("zimage", job_id)
        assert await client.cancel("zimage", job_id) is False

    async def test_cancelling_a_job_the_engine_no_longer_has_changes_nothing(
        self,
    ) -> None:
        gone = FakeEngine(model="klein4b")  # the zimage engine is not the one running
        assert await client_for(gone).cancel("zimage", "job_00000001") is False
        unknown = FakeEngine()
        assert await client_for(unknown).cancel("zimage", "job_00000001") is False

    async def test_a_cancel_that_fails_for_another_reason_is_an_error(self) -> None:
        engine = FakeEngine()
        engine.handle = lambda *args: (500, {"error": "boom"})
        with pytest.raises(EngineError) as err:
            await client_for(engine).cancel("zimage", "job_00000001")
        assert err.value.code == "engine_protocol"

    async def test_the_preview_frame_is_not_kept(self) -> None:
        engine = FakeEngine()
        engine.scripts.append([generating(1, 4)])
        client = client_for(engine)
        state = await client.poll("zimage", await client.submit(image_request()))
        assert state.image_b64 is None and "iVBOR" not in repr(state)

    @pytest.mark.parametrize(
        "reply",
        [
            {"status": "exploded"},
            {"status": 7},
            {},
            {"status": "generating", "preview": {"step": "3", "total_steps": 8}},
            {"status": "generating", "preview": {"step": True, "total_steps": 8}},
            {"status": "generating", "preview": {"step": -1, "total_steps": 8}},
            {"status": "completed", "result": {"images": []}},
            {"status": "completed", "result": {"images": [{"b64_json": 5}]}},
            {"status": "queued", "queue_position": "soon"},
            [1, 2],
            "text",
        ],
    )
    async def test_an_answer_that_is_not_a_job_is_a_protocol_error(
        self, reply: object
    ) -> None:
        engine = FakeEngine()
        engine.scripts.append([reply])
        client = client_for(engine)
        job_id = await client.submit(image_request())
        with pytest.raises(EngineError) as err:
            await client.poll("zimage", job_id)
        assert err.value.code == "engine_protocol"

    @pytest.mark.parametrize(
        ("status", "reply", "lines"),
        [
            (
                410,
                {"code": "engine_gone", "last_error": ["[E] crashed"]},
                ("[E] crashed",),
            ),
            (404, {"error": "job not found"}, ()),
        ],
    )
    async def test_a_job_the_engine_no_longer_has_is_gone(
        self, status: int, reply: object, lines: tuple[str, ...]
    ) -> None:
        engine = FakeEngine()
        engine.scripts.append([(status, reply)])
        client = client_for(engine)
        job_id = await client.submit(image_request())
        with pytest.raises(EngineError) as err:
            await client.poll("zimage", job_id)
        assert (err.value.code, err.value.lines) == ("engine_gone", lines)

    @pytest.mark.parametrize(
        "model_id, job_id", [("../x", "job_1"), ("zimage", "../x"), ("zimage", "")]
    )
    async def test_polling_and_cancelling_check_their_ids_too(
        self, model_id: str, job_id: str
    ) -> None:
        engine = FakeEngine()
        client = client_for(engine)
        for call in (client.poll, client.cancel):
            with pytest.raises(EngineError) as err:
                await call(model_id, job_id)
            assert err.value.code == "engine_protocol"
        assert engine.calls == []

    async def test_polling_a_model_whose_engine_is_not_running_is_gone(self) -> None:
        engine = FakeEngine(model="klein4b")
        with pytest.raises(EngineError) as err:
            await client_for(engine).poll("zimage", "job_00000001")
        assert err.value.code == "engine_gone"

    async def test_a_failed_start_carries_the_engines_last_lines(self) -> None:
        engine = FakeEngine(model=None)
        lines = engine_errors("model_files_wrong")
        engine.submit_response = (
            503,
            {"code": "engine_failed_to_start", "last_error": lines},
        )
        with pytest.raises(EngineError) as err:
            await client_for(engine).submit(image_request())
        assert (err.value.code, err.value.lines) == (
            "engine_failed_to_start",
            tuple(lines),
        )

    async def test_a_503_that_is_not_the_launchers_failed_start_is_a_protocol_error(
        self,
    ) -> None:
        engine = FakeEngine()
        engine.submit_response = (503, {"error": "service unavailable"})
        with pytest.raises(EngineError) as err:
            await client_for(engine).submit(image_request())
        assert err.value.code == "engine_protocol"

    async def test_an_unknown_model_is_not_a_failed_start(self) -> None:
        engine = FakeEngine()
        engine.models = {"klein4b"}
        with pytest.raises(EngineError) as err:
            await client_for(engine).submit(image_request())
        assert err.value.code == "unknown_model"

    async def test_the_engines_own_words_come_with_a_bad_request(self) -> None:
        engine = FakeEngine()
        engine.submit_response = (400, {"error": "invalid generation parameters"})
        with pytest.raises(EngineError) as err:
            await client_for(engine).submit(image_request())
        assert (err.value.code, err.value.message) == (
            "bad_request",
            "invalid generation parameters",
        )

    @pytest.mark.parametrize(
        ("status", "code"),
        [
            (429, "engine_busy"),
            (500, "engine_protocol"),
            (502, "engine_io"),
            (504, "engine_io"),
            (302, "engine_protocol"),
        ],
    )
    async def test_other_statuses_are_errors_too(self, status: int, code: str) -> None:
        engine = FakeEngine()
        engine.submit_response = (status, {"error": "no"})
        with pytest.raises(EngineError) as err:
            await client_for(engine).submit(image_request())
        assert err.value.code == code

    async def test_kill_reports_whether_an_engine_was_running(self) -> None:
        engine = FakeEngine()
        client = client_for(engine)
        assert await client.kill() is True
        assert await client.kill() is False
        assert engine.kills == 2

    async def test_the_engines_log_lines_are_bounded(self) -> None:
        engine = FakeEngine()
        engine.last_error = [f"[E] line {n} " + "x" * 2000 for n in range(40)] + [
            None,
            5,
        ]
        status = await client_for(engine).status()
        assert len(status.last_error) == 20
        assert all(
            len(line) <= 500 and isinstance(line, str) for line in status.last_error
        )
        assert status.last_error[0].startswith("[E] line 0 ")

    @pytest.mark.parametrize(
        "reply",
        [
            {"engine": "../x", "state": "ready", "last_error": []},
            {"engine": None, "state": "melting", "last_error": []},
            {"engine": None, "state": "idle", "last_error": "oops"},
            {"engine": None, "state": "idle"},
            [],
        ],
    )
    async def test_a_status_that_does_not_fit_is_a_protocol_error(
        self, reply: object
    ) -> None:
        engine = FakeEngine()
        engine.handle = lambda *args: (200, reply)
        with pytest.raises(EngineError) as err:
            await client_for(engine).status()
        assert err.value.code == "engine_protocol"


@pytest.mark.anyio
class TestEngineTransport:
    """Failures of the wire itself, with handlers that misbehave on purpose."""

    @staticmethod
    def client(handler) -> EngineClient:
        return EngineClient("/never/used.sock", transport=httpx2.MockTransport(handler))

    async def test_a_refused_connection_is_an_engine_that_is_not_running(self) -> None:
        def refuse(request: httpx2.Request) -> httpx2.Response:
            raise httpx2.ConnectError("refused", request=request)

        for call in (
            lambda c: c.status(),
            lambda c: c.kill(),
            lambda c: c.submit(image_request()),
        ):
            with pytest.raises(EngineError) as err:
                await call(self.client(refuse))
            assert err.value.code == "engine_unreachable"

    async def test_a_timeout_is_not_a_refusal(self) -> None:
        def slow(request: httpx2.Request) -> httpx2.Response:
            raise httpx2.ReadTimeout("slow", request=request)

        with pytest.raises(EngineError) as err:
            await self.client(slow).status()
        assert err.value.code == "engine_timeout"

    async def test_a_dropped_connection_is_an_io_error(self) -> None:
        def drop(request: httpx2.Request) -> httpx2.Response:
            raise httpx2.RemoteProtocolError("closed", request=request)

        with pytest.raises(EngineError) as err:
            await self.client(drop).poll("zimage", "job_1")
        assert err.value.code == "engine_io"

    async def test_an_answer_that_is_not_json_is_a_protocol_error(self) -> None:
        def html(request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(200, content=b"<html>hello</html>")

        with pytest.raises(EngineError) as err:
            await self.client(html).status()
        assert err.value.code == "engine_protocol"

    async def test_reading_stops_at_the_reply_cap(self) -> None:
        pulled = 0
        chunk = b"x" * (1024 * 1024)

        async def endless():
            nonlocal pulled
            for _ in range(100):
                pulled += 1
                yield chunk

        def huge(request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(200, content=endless())

        with pytest.raises(EngineError) as err:
            await self.client(huge).poll("zimage", "job_1")
        assert err.value.code == "engine_protocol"
        assert "16 MiB" in err.value.message
        assert MAX_REPLY_BYTES == 16 * 1024 * 1024
        assert pulled <= 17

    async def test_a_reply_just_under_the_cap_is_read_whole(self) -> None:
        body = json.dumps(
            {"status": "queued", "pad": "x" * (MAX_REPLY_BYTES - 100)}
        ).encode()
        assert len(body) < MAX_REPLY_BYTES

        def big(request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(200, content=body)

        assert (await self.client(big).poll("zimage", "job_1")).status == "queued"


@pytest.mark.anyio
class TestEngineOverARealSocket:
    async def test_a_full_exchange_over_a_unix_socket(self, socket_dir: Path) -> None:
        path = str(socket_dir / "engine.sock")
        engine = FakeEngine()
        with serve_engine(engine, path):
            client = EngineClient(path)
            assert (await client.status()).engine == "zimage"
            job_id = await client.submit(image_request(steps=3))
            seen = []
            while (state := await client.poll("zimage", job_id)).status != "completed":
                seen.append((state.status, state.step))
            assert seen == [
                ("queued", None),
                ("generating", 1),
                ("generating", 2),
                ("generating", 3),
            ]
            assert base64.b64decode(state.image_b64) == webp("wide")
            assert await client.kill() is True
        assert all(h["host"] == "engine" for h in engine.headers)
        assert all(h["connection"] == "close" for h in engine.headers)

    async def test_a_missing_socket_is_unreachable(self, socket_dir: Path) -> None:
        with pytest.raises(EngineError) as err:
            await EngineClient(str(socket_dir / "absent.sock")).status()
        assert err.value.code == "engine_unreachable"

    async def test_a_socket_nobody_listens_on_is_unreachable(
        self, bound_socket: str
    ) -> None:
        with pytest.raises(EngineError) as err:
            await EngineClient(bound_socket).status()
        assert err.value.code == "engine_unreachable"

    async def test_a_slow_start_is_waited_for(self, socket_dir: Path) -> None:
        path = str(socket_dir / "engine.sock")
        engine = FakeEngine(model=None)
        engine.submit_delay = 0.3
        with serve_engine(engine, path):
            assert (await EngineClient(path).status()).state == "idle"
            assert await EngineClient(path).submit(image_request()) == "job_00000001"
            assert (await EngineClient(path).status()).state == "ready"


def open_fds() -> int:
    return len(os.listdir("/proc/self/fd"))


class TestGpuLock:
    def test_a_job_and_a_text_turn_cannot_hold_the_gpu_together(
        self, socket_dir: Path
    ) -> None:
        lock = GpuLock(socket_dir)
        job = lock.try_exclusive()
        assert job is not None
        with pytest.raises(Busy, match="local models are paused"):
            lock.share()
        job.release()
        turn = lock.share()
        assert turn is not None
        assert lock.try_exclusive() is None
        turn.release()
        assert lock.try_exclusive() is not None

    def test_text_turns_share(self, socket_dir: Path) -> None:
        lock = GpuLock(socket_dir)
        first, second = lock.share(), lock.share()
        assert first is not None and second is not None
        first.release()
        assert lock.try_exclusive() is None  # one is still replying
        second.release()
        assert lock.try_exclusive() is not None

    def test_there_is_no_lock_to_take_where_there_is_no_directory(
        self, socket_dir: Path
    ) -> None:
        """Without the engine's socket directory there is no engine either: a text turn just goes ahead."""
        assert GpuLock(socket_dir / "absent").share() is None

    def test_releasing_twice_is_harmless_and_frees_the_descriptor(
        self, socket_dir: Path
    ) -> None:
        lock = GpuLock(socket_dir)
        before = open_fds()
        lease = lock.try_exclusive()
        assert open_fds() == before + 1
        lease.release()
        lease.release()
        assert open_fds() == before

    def test_a_lease_that_is_dropped_still_lets_go(
        self, socket_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A text turn whose stream never started would otherwise hold the lock until restart."""
        lock = GpuLock(socket_dir)
        lock.share()  # the lease is dropped on the spot
        assert lock.try_exclusive() is not None
        assert "garbage collection" in caplog.text

    @pytest.mark.anyio
    async def test_an_image_waits_its_turn(self, socket_dir: Path) -> None:
        lock = GpuLock(socket_dir)
        text_turn = lock.share()
        task = asyncio.create_task(lock.exclusive(wait=5, tick=0.01))
        await asyncio.sleep(0.1)
        assert not task.done()
        text_turn.release()
        lease = await asyncio.wait_for(task, 2)
        lease.release()

    @pytest.mark.anyio
    async def test_a_free_gpu_is_taken_at_once(self, socket_dir: Path) -> None:
        lease = await GpuLock(socket_dir).exclusive(wait=0, tick=0.01)
        lease.release()

    @pytest.mark.anyio
    async def test_waiting_gives_up_after_the_limit_and_leaves_no_descriptor(
        self, socket_dir: Path
    ) -> None:
        lock = GpuLock(socket_dir)
        holder = lock.try_exclusive()
        before = open_fds()
        started = time.monotonic()
        with pytest.raises(ImageError) as err:
            await lock.exclusive(wait=0.2, tick=0.02)
        assert err.value.code == "gpu_busy"
        assert 0.15 < time.monotonic() - started < 1.5
        assert open_fds() == before
        holder.release()

    @pytest.mark.anyio
    async def test_a_cancelled_wait_leaves_no_descriptor(
        self, socket_dir: Path
    ) -> None:
        lock = GpuLock(socket_dir)
        holder = lock.try_exclusive()
        before = open_fds()
        task = asyncio.create_task(lock.exclusive(wait=30, tick=0.01))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert open_fds() == before
        holder.release()

    @pytest.mark.anyio
    async def test_a_second_process_holds_the_same_lock(self, socket_dir: Path) -> None:
        lock = GpuLock(socket_dir)
        with holding(socket_dir, "exclusive"):
            assert lock.try_exclusive() is None
            with pytest.raises(Busy):
                lock.share()
        # the holder was killed: the kernel dropped its lock with it
        lease = lock.try_exclusive()
        assert lease is not None
        lease.release()

    @pytest.mark.anyio
    async def test_a_second_process_replying_blocks_images_but_not_other_replies(
        self, socket_dir: Path
    ) -> None:
        lock = GpuLock(socket_dir)
        with holding(socket_dir, "shared"):
            other_turn = lock.share()
            assert other_turn is not None
            other_turn.release()
            assert lock.try_exclusive() is None
            with pytest.raises(ImageError):
                await lock.exclusive(wait=0.1, tick=0.02)

    @pytest.mark.anyio
    async def test_an_image_gets_the_gpu_the_moment_the_other_process_dies(
        self, socket_dir: Path
    ) -> None:
        lock = GpuLock(socket_dir)
        with holding(socket_dir, "exclusive") as child:
            task = asyncio.create_task(lock.exclusive(wait=10, tick=0.02))
            await asyncio.sleep(0.1)
            assert not task.done()
            child.kill()
            lease = await asyncio.wait_for(task, 5)
        lease.release()


@pytest.mark.anyio
class TestUnloadOllama:
    @staticmethod
    def client(ollama: FakeOllama | None, **kwargs) -> httpx2.AsyncClient:
        handler = handler_for(ollama=ollama, **kwargs)
        return httpx2.AsyncClient(transport=httpx2.MockTransport(handler))

    async def test_a_loaded_model_is_asked_to_leave_and_gone_before_the_job_goes_on(
        self,
    ) -> None:
        ollama = FakeOllama("qwen3:8b")
        await unload_ollama(self.client(ollama), OllamaConfig(), settle=1, tick=0.01)
        assert ollama.posts() == [{"model": "qwen3:8b", "keep_alive": 0}]
        assert ollama.resident == []
        # asked, told to unload, then asked again to see that it went
        assert [(m, p) for m, p, _ in ollama.calls] == [
            ("GET", "/api/ps"),
            ("POST", "/api/generate"),
            ("GET", "/api/ps"),
        ]

    async def test_every_model_that_holds_vram_is_unloaded(self) -> None:
        ollama = FakeOllama("qwen3:8b", "gemma3:4b")
        ollama.resident.append(
            {"name": "bge-m3", "model": "bge-m3", "size": 700_000_000, "size_vram": 0}
        )
        await unload_ollama(self.client(ollama), OllamaConfig(), settle=1, tick=0.01)
        assert sorted(b["model"] for b in ollama.posts()) == ["gemma3:4b", "qwen3:8b"]

    async def test_nothing_loaded_means_nothing_to_do(self) -> None:
        ollama = FakeOllama()
        await unload_ollama(self.client(ollama), OllamaConfig(), settle=1, tick=0.01)
        assert [(m, p) for m, p, _ in ollama.calls] == [("GET", "/api/ps")]

    async def test_a_model_that_will_not_leave_refuses_the_job(self) -> None:
        ollama = FakeOllama("qwen3:8b", stubborn=("qwen3:8b",))
        started = time.monotonic()
        with pytest.raises(ImageError) as err:
            await unload_ollama(
                self.client(ollama), OllamaConfig(), settle=0.2, tick=0.02
            )
        assert err.value.code == "ollama_busy"
        assert "qwen3:8b" in err.value.message
        assert 0.15 < time.monotonic() - started < 2

    async def test_a_model_that_leaves_late_is_waited_for(self) -> None:
        ollama = FakeOllama("qwen3:8b", stubborn=("qwen3:8b",))
        client = self.client(ollama)

        async def finish_reply_soon() -> None:
            await asyncio.sleep(0.1)
            ollama.stubborn.clear()
            ollama.resident.clear()

        task = asyncio.create_task(finish_reply_soon())
        await unload_ollama(client, OllamaConfig(), settle=2, tick=0.02)
        await task

    async def test_ollama_being_down_is_not_a_reason_to_refuse(self) -> None:
        await unload_ollama(
            self.client(None, ollama_up=False), OllamaConfig(), settle=0.1, tick=0.01
        )

    async def test_an_ollama_that_answers_badly_is_skipped(self) -> None:
        for reply in (
            httpx2.Response(500),
            httpx2.Response(200, text="not json"),
            httpx2.Response(200, json=[1]),
        ):
            client = httpx2.AsyncClient(
                transport=httpx2.MockTransport(lambda request, r=reply: r)
            )
            await unload_ollama(client, OllamaConfig(), settle=0.1, tick=0.01)

    async def test_the_configured_address_is_the_one_asked(self) -> None:
        seen = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            seen.append(str(request.url))
            return httpx2.Response(200, json={"models": []})

        client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
        await unload_ollama(
            client, OllamaConfig(url="http://127.0.0.1:9999"), settle=0.1, tick=0.01
        )
        assert seen == ["http://127.0.0.1:9999/api/ps"]
