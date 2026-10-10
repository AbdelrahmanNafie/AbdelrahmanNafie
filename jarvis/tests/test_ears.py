from types import SimpleNamespace

import pytest

from jarvis.ears import Heard, understand


def test_english_text_skips_gemini():
    h = understand(text="open notepad", client=object())  # client would explode if used
    assert h == Heard(original="open notepad", language="english", english="open notepad")


def test_arabic_text_goes_to_gemini_with_schema():
    calls = {}

    class Models:
        def generate_content(self, *, model, contents, config):
            calls.update(model=model, contents=contents, config=config)
            return SimpleNamespace(parsed=Heard(original=contents[-1], language="egyptian_arabic",
                                                english="Make a note called shopping"), text="")

    h = understand(text="اعمل نوت اسمها شوبينج", model="m", client=SimpleNamespace(models=Models()))
    assert h.language == "egyptian_arabic" and h.english.startswith("Make a note")
    assert calls["model"] == "m"
    assert calls["config"].response_schema is Heard and calls["config"].temperature == 0


def test_requires_exactly_one_input():
    with pytest.raises(ValueError):
        understand()


def test_gemini_billing_error_becomes_a_clear_message(monkeypatch):
    from google.genai import errors

    from jarvis.ears import EarsError

    monkeypatch.setenv("GEMINI_API_KEY", "abcd1234")

    class Models:
        def generate_content(self, **_):
            raise errors.ClientError(402, {"error": {"code": 402, "status": "RESOURCE_EXHAUSTED",
                                                     "message": "Your prepayment credits are depleted."}})

    with pytest.raises(EarsError) as exc:
        understand(text="افتح", client=SimpleNamespace(models=Models()))
    msg = str(exc.value)
    assert "402" in msg and "...1234" in msg and "aistudio.google.com/projects" in msg


def test_busy_model_falls_back_to_next(monkeypatch):
    from google.genai import errors

    tried = []

    class Models:
        def generate_content(self, *, model, contents, config):
            tried.append(model)
            if model == "main":
                raise errors.ServerError(503, {"error": {"code": 503, "status": "UNAVAILABLE",
                                                         "message": "high demand"}})
            return SimpleNamespace(parsed=Heard(original="x", language="egyptian_arabic", english="Open WhatsApp"))

    h = understand(text="افتح الواتساب", model="main", fallbacks=("backup", "third"),
                   client=SimpleNamespace(models=Models()))
    assert tried == ["main", "backup"] and h.english == "Open WhatsApp"


def test_all_models_busy_gives_clear_error():
    from google.genai import errors

    from jarvis.ears import EarsError

    class Models:
        def generate_content(self, **_):
            raise errors.ServerError(503, {"error": {"code": 503, "status": "UNAVAILABLE", "message": "busy"}})

    with pytest.raises(EarsError, match="busy right now"):
        understand(text="افتح", model="a", fallbacks=("b",), client=SimpleNamespace(models=Models()))


def test_timeout_falls_back_then_reports_clearly():
    import httpx

    from jarvis.ears import EarsError

    tried = []

    class Models:
        def generate_content(self, *, model, **_):
            tried.append(model)
            raise httpx.ReadTimeout("The read operation timed out")

    with pytest.raises(EarsError, match="did not answer"):
        understand(text="افتح", model="a", fallbacks=("b", "c"), client=SimpleNamespace(models=Models()))
    assert tried == ["a", "b", "c"]


def _models(behaviour):
    """behaviour: model name -> callable(model) returning Heard or raising."""
    tried = []

    class Models:
        def generate_content(self, *, model, **_):
            tried.append(model)
            return SimpleNamespace(parsed=behaviour[model](model), text="")

    return SimpleNamespace(models=Models()), tried


def test_slow_model_is_raced_and_fast_answer_wins():
    import time

    def slow(_):
        time.sleep(2)
        return Heard(original="x", language="english", english="slow")

    def fast(_):
        return Heard(original="x", language="english", english="fast")

    client, tried = _models({"slow": slow, "fast": fast})
    started = time.monotonic()
    h = understand(text="افتح", model="slow", fallbacks=("fast",), client=client, hedge_after_s=0.1)
    assert h.english == "fast" and time.monotonic() - started < 1.0
    assert tried == ["slow", "fast"]


def test_model_that_answered_goes_first_next_time():
    ok = lambda m: Heard(original="x", language="english", english=m)  # noqa: E731

    def busy(_):
        from google.genai import errors
        raise errors.ServerError(503, {"error": {"code": 503, "status": "UNAVAILABLE", "message": "busy"}})

    client, tried = _models({"a": busy, "b": ok})
    understand(text="افتح", model="a", fallbacks=("b",), client=client)
    tried.clear()
    assert understand(text="افتح", model="a", fallbacks=("b",), client=client).english == "b"
    assert tried == ["b"]


def test_thinking_is_turned_off_for_speed():
    seen = {}

    class Models:
        def generate_content(self, *, model, contents, config):
            seen["thinking"] = config.thinking_config
            return SimpleNamespace(parsed=Heard(original="x", language="english", english="ok"), text="")

    understand(text="افتح", model="m", client=SimpleNamespace(models=Models()))
    assert seen["thinking"] is not None
