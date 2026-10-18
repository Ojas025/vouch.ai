"""Unit tests for vouch.preprocessing.preprocess.

preprocess() is the deterministic canonicalization stage: NFKC, invisible
character stripping, confusable skeleton folding and script profiling. Tests
drive it through the public entry point with a minimal IncomingMessage and
assert on the resulting TextForms.
"""

from datetime import datetime, timezone

import pytest

from vouch.ingestion import Channel, IncomingMessage
from vouch.preprocessing.preprocess import preprocess


def canonical(text: str):
    """Run preprocess() over a minimal message carrying only `text`."""
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    msg = IncomingMessage(
        submission_id="sub-1",
        channel=Channel.WHATSAPP,
        provider="whatsapp",
        message_id="wamid.1",
        text=text,
        timestamp=now,
        sender_id="91111111111",
        recipient_id="919876543210",
        gateway_account_id=1,
        sent_at=now,
        received_at=now,
        is_frequently_forwarded=False,
        payload_digest="0" * 64,
    )
    return preprocess(msg)


class TestNfkcNormalization:
    def test_full_width_latin_folds_to_ascii(self):
        assert canonical("Ｙｏｕｒ").text_forms.nfkc_text == "Your"

    def test_ligature_expands(self):
        assert canonical("ﬁle").text_forms.nfkc_text == "file"

    def test_combining_sequence_composes(self):
        # "e" + combining acute composes under NFKC.
        assert canonical("e\u0301").text_forms.nfkc_text == "é"

    def test_plain_ascii_is_unchanged(self):
        assert canonical("hello 123").text_forms.nfkc_text == "hello 123"


class TestInvisibleCharacterStripping:
    @pytest.mark.parametrize(
        "invisible",
        ["​", "‌", "‍", "﻿", "⁠"],
        ids=["zwsp", "zwnj", "zwj", "bom", "word-joiner"],
    )
    def test_zero_width_characters_are_removed(self, invisible):
        forms = canonical(f"pay{invisible}now").text_forms

        assert forms.format_stripped_text == "paynow"

    def test_control_characters_are_removed(self):
        forms = canonical("hel\x00\x1flo").text_forms

        assert forms.format_stripped_text == "hello"

    def test_stripping_does_not_touch_visible_text(self):
        forms = canonical("verify your account").text_forms

        assert forms.format_stripped_text == "verify your account"

    def test_raw_text_is_preserved_untouched(self):
        # The raw form is kept for provenance; only later stages see the strip.
        forms = canonical("a​b").text_forms

        assert forms.raw_text == "a​b"
        assert forms.format_stripped_text == "ab"


class TestConfusableSkeleton:
    def test_cyrillic_a_folds_to_latin_a(self):
        # U+0430 CYRILLIC SMALL LETTER A is the classic homoglyph for "a".
        forms = canonical("аpple").text_forms

        assert forms.confusable_skeleton == "apple"
        assert forms.confusable_skeleton.isascii()

    def test_mixed_homoglyph_run_folds_entirely_to_ascii(self):
        forms = canonical("раy").text_forms

        assert forms.confusable_skeleton == "pay"

    def test_skeleton_is_derived_from_stripped_text(self):
        # The zero-width character between homoglyphs must not survive into
        # the skeleton even though it is invisible in the source.
        forms = canonical("а​pple").text_forms

        assert forms.confusable_skeleton == "apple"


class TestScriptProfiling:
    def test_latin_only_text_is_not_mixed(self):
        forms = canonical("hello world").text_forms

        assert forms.mixed_script is False
        assert forms.script_profile == ["Latin"]

    def test_cyrillic_plus_latin_is_mixed(self):
        forms = canonical("Привет hello").text_forms

        assert forms.mixed_script is True
        assert set(forms.script_profile) == {"Cyrillic", "Latin"}

    def test_cyrillic_only_text_is_not_mixed(self):
        forms = canonical("Привет").text_forms

        assert forms.mixed_script is False
        assert forms.script_profile == ["Cyrillic"]

    def test_script_profile_uses_the_stripped_text(self):
        # A zero-width joiner must not be profiled as a script of its own.
        forms = canonical("hello​ world").text_forms

        assert forms.mixed_script is False
        assert forms.script_profile == ["Latin"]


class TestCanonicalMessageShape:
    def test_carries_submission_id(self):
        assert canonical("hi").submission_id == "sub-1"

    def test_empty_text_is_a_valid_canonical_message(self):
        forms = canonical("").text_forms

        assert forms.raw_text == ""
        assert forms.nfkc_text == ""
        assert forms.format_stripped_text == ""
        assert forms.confusable_skeleton == ""
        assert forms.mixed_script is False

    def test_entities_are_extracted_from_stripped_text(self):
        # preprocess() wires extraction; the entity semantics live in
        # test_preprocessing_entities.py, this only pins the hand-off.
        msg = canonical("pay merchant@okaxis")

        assert [e.type.value for e in msg.entities] == ["upi_vpa"]
        assert msg.modalities_present == []
