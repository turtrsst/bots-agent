from __future__ import annotations

from studio.resilience import (
    CRITICAL_PREFIX,
    ERROR_PLAYBOOK,
    NO_RETRY_TOOLS,
    RETRY_SAFE_TOOLS,
    build_diagnostic,
    call_with_resilience,
    classify_error,
    critical_message,
    resilient_tool,
)


class Boom(RuntimeError):
    pass


def _no_sleep(_seconds: float) -> None:
    return None


# --------------------------------------------------------------- klasifikasi
def test_classify_error_categories() -> None:
    assert classify_error(Boom("CUDA out of memory")).category == "MEMORI_RENDER_PENUH"
    assert classify_error(Boom("No such file or directory: a_roll_raw.mp4")).category == "FILE_KORUP"
    assert classify_error(Boom("HttpError 429: quota exceeded")).category == "KUOTA_API"
    assert classify_error(Boom("invalid_grant: Token has been expired")).category == "TOKEN_KEDALUWARSA"
    assert classify_error(Boom("URLError: connection reset")).category == "JARINGAN"
    assert classify_error(Boom("ffmpeg exited with 1")).category == "FFMPEG"
    assert classify_error(Boom("remotion: chromium not found")).category == "REMOTION"
    assert classify_error(Boom("sesuatu aneh")).category == "TIDAK_DIKENAL"
    assert set(ERROR_PLAYBOOK) >= {"MEMORI_RENDER_PENUH", "TIDAK_DIKENAL"}
    for diagnosis in ERROR_PLAYBOOK.values():
        assert diagnosis.advice and diagnosis.label


# ------------------------------------------------------------------- retry
def test_retry_succeeds_on_third_attempt() -> None:
    calls: list[int] = []

    def flaky() -> str:
        calls.append(1)
        if len(calls) < 3:
            raise Boom("ffmpeg exited with 1")
        return "hasil"

    result = call_with_resilience(
        "assemble_and_render_video", flaky, max_retries=3, delay_seconds=0, sleep=_no_sleep
    )
    assert result == "hasil"
    assert len(calls) == 3


def test_retry_backoff_is_exponential_and_capped() -> None:
    delays: list[float] = []

    def broken() -> str:
        raise Boom("ffmpeg gagal")

    call_with_resilience(
        "assemble_and_render_video",
        broken,
        max_retries=4,
        delay_seconds=2,
        sleep=delays.append,
    )
    assert delays == [2, 4, 8]


def test_fatal_failure_returns_diagnostic_and_notifies() -> None:
    sent: list[str] = []

    class Notifier:
        def send(self, text: str, *, silent: bool = False) -> bool:
            sent.append(text)
            return True

    def broken() -> str:
        raise Boom("out of memory")

    result = call_with_resilience(
        "assemble_and_render_video",
        broken,
        max_retries=2,
        delay_seconds=0,
        sleep=_no_sleep,
        notifier=Notifier(),
        log=lambda *_: None,
    )
    assert result.startswith(CRITICAL_PREFIX)
    assert "KATEGORI=MEMORI_RENDER_PENUH" in result
    assert len(sent) == 1
    assert "KRA AUTO-HEALING GAGAL" in sent[0]
    assert "MEMORI_RENDER_PENUH" in sent[0]


def test_side_effect_tools_are_not_retried() -> None:
    calls: list[int] = []

    def upload() -> str:
        calls.append(1)
        raise Boom("quota exceeded")

    result = call_with_resilience(
        "upload_to_youtube",
        upload,
        max_retries=3,
        delay_seconds=0,
        sleep=_no_sleep,
        log=lambda *_: None,
    )
    assert len(calls) == 1  # upload tidak diulang: hindari video ganda
    assert result.startswith(CRITICAL_PREFIX)
    assert "KUOTA_API" in result


def test_tool_policies_do_not_overlap() -> None:
    assert RETRY_SAFE_TOOLS.isdisjoint(NO_RETRY_TOOLS)
    assert "assemble_and_render_video" in RETRY_SAFE_TOOLS
    assert "upload_to_youtube" in NO_RETRY_TOOLS


def test_success_without_exception_is_passthrough() -> None:
    assert (
        call_with_resilience(
            "upload_to_youtube", lambda: "UPLOAD YOUTUBE SUKSES", sleep=_no_sleep
        )
        == "UPLOAD YOUTUBE SUKSES"
    )


# ---------------------------------------------------------------- dekorator
def test_resilient_tool_decorator_retries_custom_function() -> None:
    state = {"n": 0}

    @resilient_tool(max_retries=3, delay_seconds=0, sleep=_no_sleep, name="render_custom")
    def render() -> str:
        state["n"] += 1
        if state["n"] == 1:
            raise Boom("remotion crashed")
        return "VIDEO READY"

    assert render() == "VIDEO READY"
    assert state["n"] == 2


def test_decorator_keeps_name_and_returns_string_on_total_failure() -> None:
    @resilient_tool(max_retries=2, delay_seconds=0, sleep=_no_sleep)
    def always() -> str:
        raise Boom("moov atom not found")

    result = always()
    assert always.__name__ == "always"
    assert result.startswith(CRITICAL_PREFIX)
    assert "FILE_KORUP" in result


# ------------------------------------------------------------------ laporan
def test_build_diagnostic_escapes_usage() -> None:
    report = build_diagnostic("assemble_and_render_video", Boom("out of memory"), attempts=3)
    assert "assemble_and_render_video" in report
    assert "3×" in report
    assert "Saran fallback" in report


def test_critical_message_mentions_damon() -> None:
    message = critical_message("upload_youtube_short", Boom("invalid_grant"), 1)
    assert "Damon" in message
    assert "TOKEN_KEDALUWARSA" in message


# ------------------------------------------------------- integrasi tool nyata
def test_tool_registry_survives_unexpected_exception(settings, monkeypatch) -> None:
    from studio.tools import StudioTools

    studio = StudioTools(settings)
    monkeypatch.setattr("studio.research.fetch_trends", _boom)

    functions = studio.build_functions()
    result = functions["research_current_trends"]("AI")
    assert result.startswith(CRITICAL_PREFIX)


def _boom(*_args, **_kwargs):
    raise Boom("URLError: network is unreachable")
