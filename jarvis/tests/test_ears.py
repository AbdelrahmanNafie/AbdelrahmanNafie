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
