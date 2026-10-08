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
