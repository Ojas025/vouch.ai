"""Unit tests for vouch.security.security webhook verification."""

import hashlib
import hmac

import pytest

from vouch.security.security import verify_telegram_token, verify_whatsapp_signature

SECRET = "app-secret-123"
BODY = b'{"message_id":"wamid.ABC","text":"hello"}'
VALID_SIG = "sha256=" + hmac.new(SECRET.encode(), BODY, hashlib.sha256).hexdigest()


class TestVerifyWhatsappSignature:
    def test_accepts_correct_signature(self):
        assert verify_whatsapp_signature(BODY, VALID_SIG, SECRET) is True

    @pytest.mark.parametrize(
        "body",
        [b"", b"{}", BODY + b" ", BODY[:-1], b"x" + BODY],
        ids=["empty", "empty-object", "trailing-space", "truncated", "prepended"],
    )
    def test_rejects_any_body_mutation(self, body):
        assert verify_whatsapp_signature(body, VALID_SIG, SECRET) is False

    def test_rejects_different_secret(self):
        assert verify_whatsapp_signature(BODY, VALID_SIG, "other-secret") is False

    @pytest.mark.parametrize("signature", ["", None, "not-a-signature"])
    def test_rejects_unusable_signature(self, signature):
        assert verify_whatsapp_signature(BODY, signature, SECRET) is False

    @pytest.mark.parametrize(
        "signature",
        [
            hmac.new(SECRET.encode(), BODY, hashlib.sha256).hexdigest(),
            "sha1=" + VALID_SIG.split("=", 1)[1],
            "sha256=" + hmac.new(SECRET.encode(), BODY, hashlib.sha512).hexdigest(),
            "sha256=" + hmac.new(b"attacker", BODY, hashlib.sha256).hexdigest(),
        ],
        ids=["bare-hex-no-prefix", "wrong-prefix", "sha512", "wrong-secret"],
    )
    def test_rejects_wrongly_formed_or_foreign_signature(self, signature):
        assert verify_whatsapp_signature(BODY, signature, SECRET) is False

    def test_unicode_body_is_signed_over_raw_bytes(self):
        body = '{"text":"नमस्ते 🙏"}'.encode()
        sig = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
        assert verify_whatsapp_signature(body, sig, SECRET) is True




    def test_empty_secret_does_not_authorise(self):
        # An unconfigured secret must never validate a signature.
        sig = "sha256=" + hmac.new(b"", BODY, hashlib.sha256).hexdigest()
        assert verify_whatsapp_signature(BODY, sig, "") is False


class TestVerifyTelegramToken:
    def test_accepts_matching_token(self):
        assert verify_telegram_token(BODY, "tok-abc", "tok-abc") is True

    @pytest.mark.parametrize(
        "token, secret",
        [("", "tok-abc"), (None, "tok-abc"), ("tok-abc", "tok-xyz"),
         ("tok ", "tok"), ("TOK", "tok")],
        ids=["empty-token", "missing-token", "mismatch", "trailing-space", "case"],
    )
    def test_rejects_unauthorised_token(self, token, secret):
        assert verify_telegram_token(BODY, token, secret) is False

    def test_rejects_empty_token_even_when_secret_is_empty(self):
        # The empty-token guard fires before comparison, so an unconfigured
        # secret does not accidentally authorise an empty token.
        assert verify_telegram_token(BODY, "", "") is False

    def test_body_argument_is_ignored(self):
        # Telegram's scheme is a shared secret sent in a header and compared for
        # equality, so the body is legitimately not part of the check.
        assert verify_telegram_token(b"completely-different", "tok", "tok") is True

