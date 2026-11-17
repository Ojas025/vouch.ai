"""Contract tests for the vouch HTTP ingestion endpoints.

These tests exercise the public HTTP contract and mock only the two external
boundaries: provider authentication verification and ingestion persistence.
They do not test PostgreSQL idempotency, Redis Streams, or the cryptographic
verification algorithms themselves.

Project-specific assumption:
    The module defining ``app`` and these route handlers is importable as
    ``vouch.api.server``. Change that import if the actual module path differs.

Web request contract used here:
    ``WebInvestigationRequest`` accepts ``event_id`` and ``message``. If the
    actual public request schema has additional required fields, add them to
    ``WEB_REQUEST``; do not derive the expected payload by calling the model
    under test.

Expected provider contracts:
    * WhatsApp message IDs live at entry[].changes[].value.messages[].id.
    * Telegram update_id is an integer (not a bool, float, or string).
    * Invalid webhook authentication is rejected before payload parsing.
    * Malformed or structurally invalid provider payloads are rejected with
      400 and are never sent to ingestion.
    * A valid but irrelevant WhatsApp status notification is acknowledged and
      ignored, rather than rejected and retried by Meta.

Property-based tests use Hypothesis to exercise broad valid input ranges and
raw-body serialization variations. Add Hypothesis as a dev dependency:

    uv add --dev hypothesis
"""

from __future__ import annotations

import hashlib
import json
import string
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient
from hypothesis import given, settings, strategies as st

# Adapt this import to the actual module containing the FastAPI app/routes.
from vouch.api import server as api


WHATSAPP_PATH = "/v1/webhook/whatsapp"
TELEGRAM_PATH = "/v1/webhook/telegram"
WEB_PATH = "/v1/investigate"

WHATSAPP_SIGNATURE_HEADER = "X-Hub-Signature-256"
TELEGRAM_SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"

# This is an API request example, not a model-generated fixture. Keep it in
# sync with the documented WebInvestigationRequest contract.
WEB_REQUEST = {
    "event_id": "web-event-001",
    "message": "Investigate this suspicious URL: https://example.test",
}

SUBMISSION_ID = "sub_test_001"
ACCOUNT_ID = "0"


def _ingestion_result(*, is_duplicate: bool = False) -> SimpleNamespace:
    """Return the documented result shape consumed by the routes."""
    return SimpleNamespace(
        submission_id=SUBMISSION_ID,
        is_duplicate=is_duplicate,
    )


def _whatsapp_message_payload(message_id: str) -> dict:
    """Build a representative Meta WhatsApp Cloud API message webhook."""
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "business-account-id",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "15550001111",
                                "phone_number_id": "15550001111",
                            },
                            "contacts": [
                                {
                                    "profile": {"name": "Test Sender"},
                                    "wa_id": "15550002222",
                                }
                            ],
                            "messages": [
                                {
                                    "from": "15550002222",
                                    "id": message_id,
                                    "timestamp": "1700000000",
                                    "type": "text",
                                    "text": {"body": "Please investigate this."},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


def _whatsapp_status_payload() -> dict:
    """A delivery/read status is a valid WhatsApp webhook, not an input error."""
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "business-account-id",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "15550001111",
                                "phone_number_id": "15550001111",
                            },
                            "statuses": [
                                {
                                    "id": "wamid.outgoing-001",
                                    "status": "delivered",
                                    "timestamp": "1700000001",
                                    "recipient_id": "15550002222",
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


def _telegram_payload(update_id: int) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": 42,
            "text": "Please investigate this.",
            "chat": {"id": 12345, "type": "private"},
        },
    }


def _post_raw(
    client: TestClient,
    path: str,
    body: bytes,
    *,
    header_name: str,
    header_value: str = "test-provider-secret",
):
    return client.post(
        path,
        content=body,
        headers={
            "Content-Type": "application/json",
            header_name: header_value,
        },
    )


@pytest.fixture
def client() -> TestClient:
    # Not used as a context manager: these are isolated route tests and should
    # not start real database/Redis resources via application lifespan hooks.
    return TestClient(api.app, raise_server_exceptions=False)


@pytest.fixture
def ingestion_mock(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    mock = AsyncMock(return_value=_ingestion_result())
    monkeypatch.setattr(api.ingestion_service, "ingest", mock)
    return mock


@pytest.fixture
def whatsapp_verifier(monkeypatch: pytest.MonkeyPatch) -> Mock:
    verifier = Mock(return_value=True)
    monkeypatch.setattr(api, "verify_whatsapp_signature", verifier)
    return verifier


@pytest.fixture
def telegram_verifier(monkeypatch: pytest.MonkeyPatch) -> Mock:
    verifier = Mock(return_value=True)
    monkeypatch.setattr(api, "verify_telegram_token", verifier)
    return verifier


# ---------------------------------------------------------------------------
# Shared response and dispatch contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("is_duplicate", [False, True])
def test_webhook_response_reflects_ingestion_result(
    client: TestClient,
    ingestion_mock: AsyncMock,
    whatsapp_verifier: Mock,
    is_duplicate: bool,
) -> None:
    """The HTTP response must preserve the persisted submission result."""
    ingestion_mock.return_value = _ingestion_result(is_duplicate=is_duplicate)
    payload = _whatsapp_message_payload("wamid.response-contract")
    body = json.dumps(payload).encode()

    response = _post_raw(
        client,
        WHATSAPP_PATH,
        body,
        header_name=WHATSAPP_SIGNATURE_HEADER,
    )

    # Meta retries WhatsApp webhook deliveries when the response is not 202.
    assert response.status_code == 202
    assert response.json() == {
        "ok": True,
        "submission_id": SUBMISSION_ID,
        "is_duplicate": is_duplicate,
    }


@pytest.mark.parametrize("is_duplicate", [False, True])
def test_web_response_reflects_ingestion_result(
    client: TestClient,
    ingestion_mock: AsyncMock,
    is_duplicate: bool,
) -> None:
    ingestion_mock.return_value = _ingestion_result(is_duplicate=is_duplicate)

    response = client.post(WEB_PATH, json=WEB_REQUEST)

    assert response.status_code == 202
    assert response.json() == {
        "ok": True,
        "submission_id": SUBMISSION_ID,
        "is_duplicate": is_duplicate,
    }


# ---------------------------------------------------------------------------
# WhatsApp webhook contract
# ---------------------------------------------------------------------------


def test_whatsapp_accepts_valid_meta_message_and_dispatches_raw_body_digest(
    client: TestClient,
    ingestion_mock: AsyncMock,
    whatsapp_verifier: Mock,
) -> None:
    payload = _whatsapp_message_payload("wamid.contract-001")
    # Deliberately non-canonical JSON formatting. The provider digest contract
    # is over the exact bytes received, not a re-serialized object.
    body = json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")

    response = _post_raw(
        client,
        WHATSAPP_PATH,
        body,
        header_name=WHATSAPP_SIGNATURE_HEADER,
        header_value="sha256=test-signature",
    )

    # A durable ingestion acknowledgement must be HTTP 202 for Meta.
    assert response.status_code == 202
    whatsapp_verifier.assert_called_once()
    assert whatsapp_verifier.call_args.args[0:2] == (
        body,
        "sha256=test-signature",
    )

    ingestion_mock.assert_awaited_once_with(
        "whatsapp",
        ACCOUNT_ID,
        "wamid.contract-001",
        payload,
        hashlib.sha256(body).hexdigest(),
    )


@pytest.mark.parametrize(
    "header_value",
    [None, "", "invalid", "sha256=" + ("0" * 64)],
)
def test_whatsapp_rejects_missing_or_invalid_signature(
    client: TestClient,
    ingestion_mock: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
    header_value: str | None,
) -> None:
    verifier = Mock(return_value=False)
    monkeypatch.setattr(api, "verify_whatsapp_signature", verifier)

    headers = {"Content-Type": "application/json"}
    if header_value is not None:
        headers[WHATSAPP_SIGNATURE_HEADER] = header_value

    response = client.post(
        WHATSAPP_PATH,
        content=b"not-json",
        headers=headers,
    )

    assert response.status_code == 401
    verifier.assert_called_once()
    assert verifier.call_args.args[0] == b"not-json"
    assert verifier.call_args.args[1] == (header_value or "")
    ingestion_mock.assert_not_awaited()


def test_whatsapp_authentication_precedes_json_parsing(
    client: TestClient,
    ingestion_mock: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api, "verify_whatsapp_signature", Mock(return_value=False))

    response = client.post(
        WHATSAPP_PATH,
        content=b'{"entry":',
        headers={WHATSAPP_SIGNATURE_HEADER: "bad-signature"},
    )

    assert response.status_code == 401
    ingestion_mock.assert_not_awaited()


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"not-json",
        b"{}",
        b"[]",
        b"null",
        json.dumps({"entry": []}).encode(),
        json.dumps({"entry": [{"changes": []}]}).encode(),
        json.dumps({"entry": [{"changes": [{"value": {}}]}]}).encode(),
        json.dumps(
            {"entry": [{"changes": [{"value": {"messages": []}}]}]}
        ).encode(),
    ],
)
def test_whatsapp_rejects_malformed_or_incomplete_message_payloads(
    client: TestClient,
    ingestion_mock: AsyncMock,
    whatsapp_verifier: Mock,
    body: bytes,
) -> None:
    response = _post_raw(
        client,
        WHATSAPP_PATH,
        body,
        header_name=WHATSAPP_SIGNATURE_HEADER,
    )

    assert response.status_code == 400
    ingestion_mock.assert_not_awaited()


def test_whatsapp_rejects_invalid_message_ids_after_accepting_valid_provider_shape(
    client: TestClient,
    ingestion_mock: AsyncMock,
    whatsapp_verifier: Mock,
) -> None:
    # Positive control: this same provider envelope must be recognized first.
    valid_payload = _whatsapp_message_payload("wamid.valid-control")
    valid_response = client.post(
        WHATSAPP_PATH,
        json=valid_payload,
        headers={WHATSAPP_SIGNATURE_HEADER: "test-signature"},
    )
    assert valid_response.status_code == 202
    ingestion_mock.assert_awaited_once()

    # Reset the collaborator so each invalid case must independently avoid
    # creating an ingestion record.
    invalid_message_ids = [None, "", " ", 0, 1, True, [], {}]
    for invalid_id in invalid_message_ids:
        ingestion_mock.reset_mock()
        payload = _whatsapp_message_payload("placeholder")
        payload["entry"][0]["changes"][0]["value"]["messages"][0]["id"] = invalid_id

        response = client.post(
            WHATSAPP_PATH,
            json=payload,
            headers={WHATSAPP_SIGNATURE_HEADER: "test-signature"},
        )

        assert response.status_code == 400, f"message id accepted: {invalid_id!r}"
        ingestion_mock.assert_not_awaited()


def test_whatsapp_status_notification_is_acknowledged_without_ingestion(
    client: TestClient,
    ingestion_mock: AsyncMock,
    whatsapp_verifier: Mock,
) -> None:
    """Meta also sends delivery/read statuses through the messages webhook."""
    response = client.post(
        WHATSAPP_PATH,
        json=_whatsapp_status_payload(),
        headers={WHATSAPP_SIGNATURE_HEADER: "test-signature"},
    )

    assert response.status_code == 202
    assert response.json().get("ok") is True
    ingestion_mock.assert_not_awaited()


# ---------------------------------------------------------------------------
# Telegram webhook contract
# ---------------------------------------------------------------------------


def test_telegram_accepts_valid_update_and_dispatches_raw_body_digest(
    client: TestClient,
    ingestion_mock: AsyncMock,
    telegram_verifier: Mock,
) -> None:
    payload = _telegram_payload(731)
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")

    response = _post_raw(
        client,
        TELEGRAM_PATH,
        body,
        header_name=TELEGRAM_SECRET_HEADER,
        header_value="configured-webhook-secret",
    )

    assert response.status_code == 202
    assert response.json() == {
        "ok": True,
        "submission_id": SUBMISSION_ID,
        "is_duplicate": False,
    }
    telegram_verifier.assert_called_once()
    assert telegram_verifier.call_args.args[0:2] == (
        body,
        "configured-webhook-secret",
    )
    ingestion_mock.assert_awaited_once_with(
        "telegram",
        ACCOUNT_ID,
        "731",
        payload,
        hashlib.sha256(body).hexdigest(),
    )


@pytest.mark.parametrize("header_value", [None, "", "wrong-secret"])
def test_telegram_rejects_missing_or_invalid_secret_token(
    client: TestClient,
    ingestion_mock: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
    header_value: str | None,
) -> None:
    verifier = Mock(return_value=False)
    monkeypatch.setattr(api, "verify_telegram_token", verifier)

    headers = {"Content-Type": "application/json"}
    if header_value is not None:
        headers[TELEGRAM_SECRET_HEADER] = header_value

    response = client.post(TELEGRAM_PATH, content=b"not-json", headers=headers)

    assert response.status_code == 401
    verifier.assert_called_once()
    assert verifier.call_args.args[0] == b"not-json"
    assert verifier.call_args.args[1] == (header_value or "")
    ingestion_mock.assert_not_awaited()


def test_telegram_authentication_precedes_json_parsing(
    client: TestClient,
    ingestion_mock: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api, "verify_telegram_token", Mock(return_value=False))

    response = client.post(
        TELEGRAM_PATH,
        content=b'{"update_id":',
        headers={TELEGRAM_SECRET_HEADER: "bad-secret"},
    )

    assert response.status_code == 401
    ingestion_mock.assert_not_awaited()


@pytest.mark.parametrize("body", [b"", b"not-json", b"{}", b"[]", b"null"])
def test_telegram_rejects_malformed_or_incomplete_payloads(
    client: TestClient,
    ingestion_mock: AsyncMock,
    telegram_verifier: Mock,
    body: bytes,
) -> None:
    response = _post_raw(
        client,
        TELEGRAM_PATH,
        body,
        header_name=TELEGRAM_SECRET_HEADER,
    )

    assert response.status_code == 400
    ingestion_mock.assert_not_awaited()


@pytest.mark.parametrize("update_id", [None, "731", 731.5, True, [], {}, 0, -1])
def test_telegram_rejects_non_integer_update_ids(
    client: TestClient,
    ingestion_mock: AsyncMock,
    telegram_verifier: Mock,
    update_id,
) -> None:
    response = client.post(
        TELEGRAM_PATH,
        json={"update_id": update_id, "message": {"text": "hello"}},
        headers={TELEGRAM_SECRET_HEADER: "test-secret"},
    )

    assert response.status_code == 400
    ingestion_mock.assert_not_awaited()


def test_telegram_ingestion_failure_is_not_reported_as_accepted(
    client: TestClient,
    ingestion_mock: AsyncMock,
    telegram_verifier: Mock,
) -> None:
    ingestion_mock.side_effect = RuntimeError("storage unavailable")

    response = client.post(
        TELEGRAM_PATH,
        json=_telegram_payload(731),
        headers={TELEGRAM_SECRET_HEADER: "test-secret"},
    )

    assert response.status_code == 500


# ---------------------------------------------------------------------------
# Web investigation endpoint contract
# ---------------------------------------------------------------------------


def test_web_endpoint_dispatches_valid_request_with_canonical_payload_digest(
    client: TestClient,
    ingestion_mock: AsyncMock,
) -> None:
    response = client.post(WEB_PATH, json=WEB_REQUEST)

    assert response.status_code == 202
    args = ingestion_mock.await_args.args
    kwargs = ingestion_mock.await_args.kwargs

    assert kwargs == {}
    assert args[0:3] == ("web", ACCOUNT_ID, WEB_REQUEST["event_id"])
    assert args[3]["event_id"] == WEB_REQUEST["event_id"]
    assert args[3]["message"] == WEB_REQUEST["message"]
    assert args[4] == hashlib.sha256(
        json.dumps(args[3], sort_keys=True).encode("utf-8")
    ).hexdigest()


def test_web_endpoint_rejects_missing_event_id_before_ingestion(
    client: TestClient,
    ingestion_mock: AsyncMock,
) -> None:
    response = client.post(
        WEB_PATH,
        json={"message": WEB_REQUEST["message"]},
    )

    assert response.status_code == 422
    ingestion_mock.assert_not_awaited()


@pytest.mark.parametrize("event_id", [None, "", 0, 1, True, [], {}])
def test_web_endpoint_rejects_invalid_event_ids(
    client: TestClient,
    ingestion_mock: AsyncMock,
    event_id,
) -> None:
    response = client.post(
        WEB_PATH,
        json={"event_id": event_id, "message": WEB_REQUEST["message"]},
    )

    assert response.status_code == 422
    ingestion_mock.assert_not_awaited()


@pytest.mark.parametrize("message", [None, ""])
def test_web_endpoint_rejects_missing_or_empty_message(
    client: TestClient,
    ingestion_mock: AsyncMock,
    message,
) -> None:
    response = client.post(
        WEB_PATH,
        json={"event_id": "web-event-invalid", "message": message},
    )

    assert response.status_code == 422
    ingestion_mock.assert_not_awaited()


@pytest.mark.parametrize("body", [b"", b"not-json", b'{"event_id":'])
def test_web_endpoint_rejects_malformed_json(
    client: TestClient,
    ingestion_mock: AsyncMock,
    body: bytes,
) -> None:
    response = client.post(
        WEB_PATH,
        content=body,
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 422
    ingestion_mock.assert_not_awaited()


def test_web_endpoint_maps_ingestion_failure_to_generic_500(
    client: TestClient,
    ingestion_mock: AsyncMock,
) -> None:
    ingestion_mock.side_effect = RuntimeError("sensitive database detail")

    response = client.post(WEB_PATH, json=WEB_REQUEST)

    assert response.status_code == 500
    assert "sensitive database detail" not in response.text
    ingestion_mock.assert_awaited_once()


# ---------------------------------------------------------------------------
# Cross-endpoint failure behavior
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "header_name", "validator_name", "payload"),
    [
        (
            WHATSAPP_PATH,
            WHATSAPP_SIGNATURE_HEADER,
            "verify_whatsapp_signature",
            _whatsapp_message_payload("wamid.ingest-failure"),
        ),
        (
            TELEGRAM_PATH,
            TELEGRAM_SECRET_HEADER,
            "verify_telegram_token",
            _telegram_payload(731),
        ),
    ],
)
def test_webhook_ingestion_failure_returns_server_error(
    client: TestClient,
    ingestion_mock: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    header_name: str,
    validator_name: str,
    payload: dict,
) -> None:
    monkeypatch.setattr(api, validator_name, Mock(return_value=True))
    ingestion_mock.side_effect = RuntimeError("storage unavailable")

    response = client.post(
        path,
        json=payload,
        headers={header_name: "test-secret"},
    )

    assert response.status_code == 500
    ingestion_mock.assert_awaited_once()


# ---------------------------------------------------------------------------
# Property-based tests
# ---------------------------------------------------------------------------


_PROVIDER_ID_ALPHABET = string.ascii_letters + string.digits + "-_.:"
_PROVIDER_IDS = st.text(
    alphabet=_PROVIDER_ID_ALPHABET,
    min_size=1,
    max_size=96,
)


@settings(max_examples=40, deadline=None)
@given(
    suffix=_PROVIDER_IDS,
    indent=st.one_of(st.none(), st.integers(min_value=0, max_value=4)),
)
def test_property_whatsapp_message_ids_and_exact_raw_body_digest(
    suffix: str,
    indent: int | None,
) -> None:
    """Every non-empty provider message ID is preserved; digest uses raw bytes."""
    message_id = f"wamid.{suffix}"
    payload = _whatsapp_message_payload(message_id)
    body = json.dumps(payload, indent=indent, ensure_ascii=False).encode("utf-8")

    with pytest.MonkeyPatch.context() as patch:
        ingestion = AsyncMock(return_value=_ingestion_result())
        verifier = Mock(return_value=True)
        patch.setattr(api.ingestion_service, "ingest", ingestion)
        patch.setattr(api, "verify_whatsapp_signature", verifier)

        response = TestClient(api.app, raise_server_exceptions=False).post(
            WHATSAPP_PATH,
            content=body,
            headers={
                "Content-Type": "application/json",
                WHATSAPP_SIGNATURE_HEADER: "valid-signature",
            },
        )

    assert response.status_code == 202
    ingestion.assert_awaited_once_with(
        "whatsapp",
        ACCOUNT_ID,
        message_id,
        payload,
        hashlib.sha256(body).hexdigest(),
    )


@settings(max_examples=40, deadline=None)
@given(update_id=st.integers(min_value=1, max_value=2**63 - 1))
def test_property_telegram_update_ids_are_forwarded_as_decimal_strings(
    update_id: int,
) -> None:
    """Telegram integer IDs are converted losslessly to the ingestion ID type."""
    payload = _telegram_payload(update_id)
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")

    with pytest.MonkeyPatch.context() as patch:
        ingestion = AsyncMock(return_value=_ingestion_result())
        verifier = Mock(return_value=True)
        patch.setattr(api.ingestion_service, "ingest", ingestion)
        patch.setattr(api, "verify_telegram_token", verifier)

        response = TestClient(api.app, raise_server_exceptions=False).post(
            TELEGRAM_PATH,
            content=body,
            headers={
                "Content-Type": "application/json",
                TELEGRAM_SECRET_HEADER: "valid-secret",
            },
        )

    assert response.status_code == 202
    ingestion.assert_awaited_once_with(
        "telegram",
        ACCOUNT_ID,
        str(update_id),
        payload,
        hashlib.sha256(body).hexdigest(),
    )


@settings(max_examples=40, deadline=None)
@given(
    event_id=_PROVIDER_IDS,
    message=st.text(min_size=1, max_size=256).filter(lambda value: bool(value.strip())),
)
def test_property_web_request_is_forwarded_and_digest_matches_normalized_payload(
    event_id: str,
    message: str,
) -> None:
    """Valid text and IDs survive the web adapter; digest covers normalized data."""
    request_payload = {"event_id": event_id, "message": message}

    with pytest.MonkeyPatch.context() as patch:
        ingestion = AsyncMock(return_value=_ingestion_result())
        patch.setattr(api.ingestion_service, "ingest", ingestion)

        response = TestClient(api.app, raise_server_exceptions=False).post(
            WEB_PATH,
            json=request_payload,
        )

    assert response.status_code == 202
    args = ingestion.await_args.args
    assert args[0:3] == ("web", ACCOUNT_ID, event_id)
    assert args[3]["event_id"] == event_id
    assert args[3]["message"] == message
    assert args[4] == hashlib.sha256(
        json.dumps(args[3], sort_keys=True).encode("utf-8")
    ).hexdigest()


@settings(max_examples=30, deadline=None)
@given(suffix=st.binary(max_size=128))
def test_property_webhooks_reject_non_utf8_bodies_as_bad_requests(
    suffix: bytes,
) -> None:
    """Arbitrary bytes prefixed with invalid UTF-8 must never reach ingestion."""
    body = b"\xff" + suffix

    with pytest.MonkeyPatch.context() as patch:
        ingestion = AsyncMock(return_value=_ingestion_result())
        wa_verifier = Mock(return_value=True)
        tg_verifier = Mock(return_value=True)
        patch.setattr(api.ingestion_service, "ingest", ingestion)
        patch.setattr(api, "verify_whatsapp_signature", wa_verifier)
        patch.setattr(api, "verify_telegram_token", tg_verifier)

        client = TestClient(api.app, raise_server_exceptions=False)
        whatsapp_response = client.post(
            WHATSAPP_PATH,
            content=body,
            headers={WHATSAPP_SIGNATURE_HEADER: "valid-signature"},
        )
        telegram_response = client.post(
            TELEGRAM_PATH,
            content=body,
            headers={TELEGRAM_SECRET_HEADER: "valid-secret"},
        )

    assert whatsapp_response.status_code == 400
    assert telegram_response.status_code == 400
    ingestion.assert_not_awaited()
