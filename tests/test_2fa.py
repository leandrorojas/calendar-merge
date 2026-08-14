"""Tests for the iCloud 2FA / 2SA branches."""

from collections.abc import Callable

import pytest

import merge
from tests.conftest import fake_api


def recording(calls: list[str], label: str, result: bool = True) -> Callable[..., bool]:
    """Return a stub that records `label` when called and returns `result`."""

    def _stub(*args, **kwargs):
        calls.append(label)
        return result

    return _stub


class TestValidate2faRouting:
    def test_no_2fa_required_returns_true(self):
        api = fake_api(requires_2fa=False, requires_2sa=False)

        assert merge.validate_2fa(api) is True

    def test_prefers_security_key_when_available(self, monkeypatch):
        calls: list[str] = []
        monkeypatch.setattr(merge, "_validate_2fa_fido2", recording(calls, "fido2"))
        monkeypatch.setattr(merge, "_validate_2fa_trusted_device", recording(calls, "device"))
        api = fake_api(requires_2fa=True, security_key_names=["yubikey"])

        assert merge.validate_2fa(api) is True
        assert calls == ["fido2"]

    def test_falls_back_to_trusted_device(self, monkeypatch):
        calls: list[str] = []
        monkeypatch.setattr(merge, "_validate_2fa_trusted_device", recording(calls, "device"))
        api = fake_api(requires_2fa=True, security_key_names=[])

        assert merge.validate_2fa(api) is True
        assert calls == ["device"]

    def test_still_requests_trust_when_validation_fails(self, monkeypatch, quiet_terminal):
        """Trust is attempted even after a rejected code, and the run still fails.

        Apple can refuse a code while still granting trust. Skipping the request
        made the run honest but threw away that recovery, so every later run
        prompted again.
        """
        trust_calls = []

        def record_trust(api):
            trust_calls.append(api)
            return merge.SessionTrust.refused

        monkeypatch.setattr(merge, "_validate_2fa_trusted_device", lambda api: False)
        monkeypatch.setattr(merge, "_request_session_trust", record_trust)
        monkeypatch.setattr(merge, "send_telegram_message", lambda *a, **k: None)
        api = fake_api(requires_2fa=True)

        assert merge.validate_2fa(api) is False
        assert trust_calls == [api]

    def test_reports_when_trust_survives_a_failed_code(self, monkeypatch, quiet_terminal):
        """The failure alert alone reads as "nothing was achieved"."""
        sent = []
        monkeypatch.setattr(merge, "_validate_2fa_trusted_device", lambda api: False)
        monkeypatch.setattr(merge, "_request_session_trust", lambda api: merge.SessionTrust.granted)
        monkeypatch.setattr(merge, "send_telegram_message", lambda msg, **k: sent.append(msg))
        api = fake_api(requires_2fa=True)

        assert merge.validate_2fa(api) is False
        assert sent == [merge.TELEGRAM_2FA_TRUSTED_AFTER_FAILURE_MESSAGE]
        assert any("next run should not prompt" in line for line in quiet_terminal)

    def test_stays_quiet_when_the_session_was_already_trusted(self, monkeypatch, quiet_terminal):
        """No request was made, so nothing changed and nothing may be promised.

        requires_2fa is true whenever hsaChallengeRequired is set, even on a
        trusted session, so this run prompted, failed, and requested nothing.
        Telling the user the next run will be quiet would be a false reassurance —
        the trust flag demonstrably did not stop *this* run from prompting.

        Uses the real _request_session_trust so the already_trusted path is
        exercised end to end.
        """
        sent = []
        api = fake_api(requires_2fa=True, is_trusted_session=True)
        monkeypatch.setattr(merge, "_validate_2fa_trusted_device", lambda api: False)
        monkeypatch.setattr(merge, "send_telegram_message", lambda msg, **k: sent.append(msg))

        assert merge.validate_2fa(api) is False
        assert sent == []

    def test_stays_quiet_when_both_code_and_trust_fail(self, monkeypatch, quiet_terminal):
        sent = []
        monkeypatch.setattr(merge, "_validate_2fa_trusted_device", lambda api: False)
        monkeypatch.setattr(merge, "_request_session_trust", lambda api: merge.SessionTrust.refused)
        monkeypatch.setattr(merge, "send_telegram_message", lambda msg, **k: sent.append(msg))
        api = fake_api(requires_2fa=True)

        assert merge.validate_2fa(api) is False
        assert sent == []

    def test_requests_session_trust_after_success(self, monkeypatch):
        trust_calls = []

        def record_trust(api):
            trust_calls.append(api)
            return merge.SessionTrust.granted

        monkeypatch.setattr(merge, "_validate_2fa_trusted_device", lambda api: True)
        monkeypatch.setattr(merge, "_request_session_trust", record_trust)
        api = fake_api(requires_2fa=True)

        assert merge.validate_2fa(api) is True
        assert trust_calls == [api]

    def test_success_does_not_send_the_trusted_after_failure_message(self, monkeypatch, quiet_terminal):
        sent = []
        monkeypatch.setattr(merge, "_validate_2fa_trusted_device", lambda api: True)
        monkeypatch.setattr(merge, "_request_session_trust", lambda api: merge.SessionTrust.granted)
        monkeypatch.setattr(merge, "send_telegram_message", lambda msg, **k: sent.append(msg))
        api = fake_api(requires_2fa=True)

        assert merge.validate_2fa(api) is True
        assert merge.TELEGRAM_2FA_TRUSTED_AFTER_FAILURE_MESSAGE not in sent

    def test_routes_to_2sa_when_only_2sa_required(self, monkeypatch):
        calls: list[str] = []
        monkeypatch.setattr(merge, "_validate_2fa_2sa", recording(calls, "2sa"))
        api = fake_api(requires_2fa=False, requires_2sa=True)

        assert merge.validate_2fa(api) is True
        assert calls == ["2sa"]

    def test_2sa_failure_propagates(self, monkeypatch):
        monkeypatch.setattr(merge, "_validate_2fa_2sa", lambda api: False)
        api = fake_api(requires_2fa=False, requires_2sa=True)

        assert merge.validate_2fa(api) is False


class TestRequestSessionTrust:
    def test_skips_when_session_already_trusted(self, quiet_terminal):
        api = fake_api(is_trusted_session=True)

        merge._request_session_trust(api)

        assert quiet_terminal == []

    def test_requests_trust_when_untrusted(self, quiet_terminal):
        api = fake_api(is_trusted_session=False, trust_result=True)

        merge._request_session_trust(api)

        assert any("Requesting trust" in line for line in quiet_terminal)
        assert any("trust result True" in line for line in quiet_terminal)

    def test_warns_when_trust_request_fails(self, quiet_terminal):
        api = fake_api(is_trusted_session=False, trust_result=False)

        merge._request_session_trust(api)

        assert any("Failed to request trust" in line for line in quiet_terminal)

    def test_returns_true_when_already_trusted(self, quiet_terminal):
        assert merge._request_session_trust(fake_api(is_trusted_session=True)) is merge.SessionTrust.already_trusted

    def test_does_not_re_request_trust_when_already_trusted(self, quiet_terminal):
        """Guards the early return: an established session needs no new request."""
        calls = []
        api = fake_api(is_trusted_session=True)
        api.trust_session = lambda: calls.append(True)

        assert merge._request_session_trust(api) is merge.SessionTrust.already_trusted
        assert calls == []
        assert quiet_terminal == []

    def test_logs_the_raw_trust_response(self, quiet_terminal):
        """Coercing before logging would hide what pyicloud actually returned."""
        api = fake_api(is_trusted_session=False)
        api.trust_session = lambda: {"status": "granted"}

        assert merge._request_session_trust(api) is merge.SessionTrust.granted
        assert any("Session trust result {'status': 'granted'}" in line for line in quiet_terminal)

    def test_reports_granted_when_trust_is_newly_established(self, quiet_terminal):
        api = fake_api(is_trusted_session=False, trust_result=True)

        assert merge._request_session_trust(api) is merge.SessionTrust.granted

    def test_reports_refused_when_trust_is_denied(self, quiet_terminal):
        api = fake_api(is_trusted_session=False, trust_result=False)

        assert merge._request_session_trust(api) is merge.SessionTrust.refused

    def test_survives_trust_session_raising(self, quiet_terminal):
        """pyicloud lets PyiCloudFailedLoginException escape trust_session().

        Its own except clause only covers PyiCloudAPIResponseException and
        PyiCloud2FARequiredException, and _authenticate_with_token() raises a
        PyiCloudFailedLoginException that is neither. Since this call now also
        runs on the failure path -- where the session is least healthy -- letting
        it escape would relabel an accurate "2FA validation failed" as the
        generic "2FA validation error".
        """
        api = fake_api(is_trusted_session=False)

        def boom():
            raise RuntimeError("No session token available")

        api.trust_session = boom

        assert merge._request_session_trust(api) is merge.SessionTrust.refused
        assert any("Session trust request failed: No session token available" in line for line in quiet_terminal)

    def test_a_raising_trust_session_does_not_break_validate_2fa(self, monkeypatch, quiet_terminal):
        sent = []
        api = fake_api(requires_2fa=True, is_trusted_session=False)
        api.trust_session = lambda: (_ for _ in ()).throw(RuntimeError("No session token available"))
        monkeypatch.setattr(merge, "_validate_2fa_trusted_device", lambda api: False)
        monkeypatch.setattr(merge, "send_telegram_message", lambda msg, **k: sent.append(msg))

        assert merge.validate_2fa(api) is False
        assert sent == []

    def test_coerces_a_truthy_non_bool_trust_result(self, quiet_terminal):
        api = fake_api(is_trusted_session=False)
        api.trust_session = lambda: "yes"

        assert merge._request_session_trust(api) is merge.SessionTrust.granted


class TestValidate2faTrustedDevice:
    def test_disables_sms_fallback(self, monkeypatch):
        # pyicloud would otherwise switch delivery to SMS when the
        # trusted-device bridge times out, rejecting the pushed code.
        monkeypatch.setattr(merge, "prompt_telegram_reply", lambda prompt, after_send=None, accept=None: "123456")
        api = fake_api(requires_2fa=True)

        merge._validate_2fa_trusted_device(api)

        assert api._can_request_sms_2fa_code() is False

    def test_returns_false_when_no_code_arrives(self, monkeypatch, quiet_terminal):
        monkeypatch.setattr(merge, "prompt_telegram_reply", lambda prompt, after_send=None, accept=None: None)
        api = fake_api(requires_2fa=True)

        assert merge._validate_2fa_trusted_device(api) is False
        assert any("No code received" in line for line in quiet_terminal)

    def test_validates_received_code(self, monkeypatch):
        seen = {}

        def record_code(code):
            seen["code"] = code
            return True

        api = fake_api(requires_2fa=True)
        api.validate_2fa_code = record_code
        monkeypatch.setattr(merge, "prompt_telegram_reply", lambda prompt, after_send=None, accept=None: "654321")

        assert merge._validate_2fa_trusted_device(api) is True
        assert seen["code"] == "654321"

    def test_reports_rejected_code(self, monkeypatch, quiet_terminal):
        monkeypatch.setattr(merge, "prompt_telegram_reply", lambda prompt, after_send=None, accept=None: "000000")
        api = fake_api(requires_2fa=True, validate_result=False)

        assert merge._validate_2fa_trusted_device(api) is False
        assert any("Failed to verify security code" in line for line in quiet_terminal)

    def test_after_send_callback_requests_the_code(self, monkeypatch, quiet_terminal):
        # The callback is what actually triggers Apple's push; run it to be sure
        # it reports the delivery method rather than throwing.
        requested = []
        api = fake_api(requires_2fa=True)
        api.request_2fa_code = lambda: requested.append(True)

        def fake_prompt(prompt, after_send=None, accept=None):
            after_send()
            return "111111"

        monkeypatch.setattr(merge, "prompt_telegram_reply", fake_prompt)

        assert merge._validate_2fa_trusted_device(api) is True
        assert requested == [True]
        assert any("delivery method: trusteddevice" in line for line in quiet_terminal)

    def test_swallows_error_from_code_request(self, monkeypatch, quiet_terminal):
        api = fake_api(requires_2fa=True)

        def boom():
            raise RuntimeError("bridge timeout")

        api.request_2fa_code = boom

        def fake_prompt(prompt, after_send=None, accept=None):
            after_send()
            return "222222"

        monkeypatch.setattr(merge, "prompt_telegram_reply", fake_prompt)

        # A failed push must not abort the flow: the code often still arrives.
        assert merge._validate_2fa_trusted_device(api) is True
        assert any("2FA request warning: bridge timeout" in line for line in quiet_terminal)


class TestIsTwoFactorCode:
    @pytest.mark.parametrize("value", ["123456", "000000", " 123456 ", "123456\n"])
    def test_accepts_six_digits(self, value):
        assert merge._is_two_factor_code(value) is True

    @pytest.mark.parametrize(
        "value",
        ["12345", "1234567", "12345a", "ok", "", "what code?", "123 456", "-12345"],
    )
    def test_rejects_anything_else(self, value):
        """Chatter must not be submitted to Apple as the code."""
        assert merge._is_two_factor_code(value) is False


class TestValidateTwoFactorCode:
    def test_returns_true_when_apple_accepts(self, quiet_terminal):
        api = fake_api(requires_2fa=True)

        assert merge._validate_two_factor_code(api, "123456") is True

    def test_returns_false_when_apple_rejects(self, quiet_terminal):
        api = fake_api(requires_2fa=True, validate_result=False)

        assert merge._validate_two_factor_code(api, "123456") is False
        assert any("Failed to verify security code" in line for line in quiet_terminal)

    def test_treats_a_raised_error_as_a_rejection(self, quiet_terminal):
        """An expired code can raise rather than return False.

        Aborting there would skip the retry loop entirely, so the error is
        reported and counted as a failed attempt.
        """
        api = fake_api(requires_2fa=True)

        def expired(code):
            raise RuntimeError("code expired")

        api.validate_2fa_code = expired

        assert merge._validate_two_factor_code(api, "123456") is False
        assert any("Code rejected by Apple: code expired" in line for line in quiet_terminal)

    @pytest.mark.parametrize("reply", ["123456\n", " 123456 ", "\t123456\r\n"])
    def test_strips_the_code_before_submitting(self, reply, quiet_terminal):
        """Apple must receive the digits, not the raw Telegram text.

        `_is_two_factor_code` accepts surrounding whitespace and Telegram clients
        readily append a trailing newline, so submitting the raw reply would have
        Apple reject a code this module already judged valid.
        """
        submitted = []

        def capture(code):
            submitted.append(code)
            return True

        api = fake_api(requires_2fa=True)
        api.validate_2fa_code = capture

        assert merge._validate_two_factor_code(api, reply) is True
        assert submitted == ["123456"]

    def test_coerces_a_truthy_non_bool_result(self, quiet_terminal):
        api = fake_api(requires_2fa=True)
        api.validate_2fa_code = lambda code: "yes"

        assert merge._validate_two_factor_code(api, "123456") is True


class TestTwoFactorRetries:
    def test_retries_after_a_rejected_code(self, monkeypatch, quiet_terminal):
        """A mistyped digit must not abort the whole merge."""
        codes = iter(["111111", "222222", "333333"])
        submitted = []

        def accept_third(code):
            submitted.append(code)
            return code == "333333"

        api = fake_api(requires_2fa=True)
        api.validate_2fa_code = accept_third
        monkeypatch.setattr(merge, "prompt_telegram_reply", lambda prompt, after_send=None, accept=None: next(codes))

        assert merge._validate_2fa_trusted_device(api) is True
        assert submitted == ["111111", "222222", "333333"]

    def test_gives_up_after_the_attempt_limit(self, monkeypatch, quiet_terminal):
        submitted: list[str] = []

        def always_reject(code):
            submitted.append(code)
            return False

        api = fake_api(requires_2fa=True, validate_result=False)
        api.validate_2fa_code = always_reject
        monkeypatch.setattr(merge, "prompt_telegram_reply", lambda prompt, after_send=None, accept=None: "000000")

        assert merge._validate_2fa_trusted_device(api) is False
        assert len(submitted) == merge.TWO_FACTOR_CODE_ATTEMPTS
        assert any(f"after {merge.TWO_FACTOR_CODE_ATTEMPTS} attempts" in line for line in quiet_terminal)

    def test_requests_apples_push_only_once(self, monkeypatch, quiet_terminal):
        """Re-requesting would issue a fresh code and invalidate the held one."""
        requested = []
        api = fake_api(requires_2fa=True, validate_result=False)
        api.request_2fa_code = lambda: requested.append(True)

        def fake_prompt(prompt, after_send=None, accept=None):
            if after_send:
                after_send()
            return "000000"

        monkeypatch.setattr(merge, "prompt_telegram_reply", fake_prompt)

        merge._validate_2fa_trusted_device(api)

        assert requested == [True]

    def test_retry_prompt_explains_the_rejection(self, monkeypatch, quiet_terminal):
        prompts = []
        api = fake_api(requires_2fa=True, validate_result=False)

        def fake_prompt(prompt, after_send=None, accept=None):
            prompts.append(prompt)
            return "000000"

        monkeypatch.setattr(merge, "prompt_telegram_reply", fake_prompt)

        merge._validate_2fa_trusted_device(api)

        assert prompts[0] == "provide the Apple 2FA code"
        assert "rejected" in prompts[1]
        assert f"2/{merge.TWO_FACTOR_CODE_ATTEMPTS}" in prompts[1]

    def test_keeps_retrying_when_the_push_request_raised(self, monkeypatch, quiet_terminal):
        """A raised request_2fa_code does NOT mean the code went undelivered.

        pyicloud's bridge posts step0 -- which makes Apple push the code -- before
        the wait that times out, and when the bridge state is left unset
        validate_2fa_code() falls back to the legacy trusted-device endpoint, which
        validates real codes. So the user usually does hold a working code here.
        Disabling the retries would abort on a single mistyped digit in exactly the
        bridge state this deployment hits most often.
        """
        prompts = []
        api = fake_api(requires_2fa=True, validate_result=False)

        def bridge_down():
            raise RuntimeError("Failed to bootstrap the trusted-device bridge")

        api.request_2fa_code = bridge_down

        def fake_prompt(prompt, after_send=None, accept=None):
            prompts.append(prompt)
            if after_send:
                after_send()
            return "123456"

        monkeypatch.setattr(merge, "prompt_telegram_reply", fake_prompt)

        assert merge._validate_2fa_trusted_device(api) is False
        assert len(prompts) == merge.TWO_FACTOR_CODE_ATTEMPTS
        assert any("2FA request warning" in line for line in quiet_terminal)

    def test_a_delivered_code_still_authenticates_after_a_bridge_error(self, monkeypatch, quiet_terminal):
        """The realistic case: bridge times out, code arrives, second try works."""
        codes = iter(["111111", "222222"])
        api = fake_api(requires_2fa=True)
        api.request_2fa_code = lambda: (_ for _ in ()).throw(RuntimeError("bridge timeout"))
        api.validate_2fa_code = lambda code: code == "222222"
        monkeypatch.setattr(merge, "send_telegram_message", lambda *a, **k: None)
        monkeypatch.setattr(
            merge,
            "prompt_telegram_reply",
            lambda prompt, after_send=None, accept=None: (after_send() if after_send else None, next(codes))[1],
        )

        assert merge._validate_2fa_trusted_device(api) is True

    def test_still_retries_when_the_push_succeeded(self, monkeypatch, quiet_terminal):
        """A plain wrong code must keep its retries."""
        prompts = []
        api = fake_api(requires_2fa=True, validate_result=False)

        def fake_prompt(prompt, after_send=None, accept=None):
            prompts.append(prompt)
            if after_send:
                after_send()
            return "000000"

        monkeypatch.setattr(merge, "prompt_telegram_reply", fake_prompt)

        assert merge._validate_2fa_trusted_device(api) is False
        assert len(prompts) == merge.TWO_FACTOR_CODE_ATTEMPTS

    def test_does_not_retry_when_no_code_arrives(self, monkeypatch, quiet_terminal):
        """A timeout means nobody is answering, so retrying is pointless."""
        calls = []

        def fake_prompt(prompt, after_send=None, accept=None):
            calls.append(prompt)
            return None

        api = fake_api(requires_2fa=True)
        monkeypatch.setattr(merge, "prompt_telegram_reply", fake_prompt)

        assert merge._validate_2fa_trusted_device(api) is False
        assert len(calls) == 1

    def test_whitespace_wrapped_reply_reaches_apple_clean(self, monkeypatch, quiet_terminal):
        """End to end: a reply with a trailing newline still authenticates."""
        submitted = []

        def capture(code):
            submitted.append(code)
            return True

        api = fake_api(requires_2fa=True)
        api.validate_2fa_code = capture
        monkeypatch.setattr(merge, "prompt_telegram_reply", lambda prompt, after_send=None, accept=None: "123456\n")

        assert merge._validate_2fa_trusted_device(api) is True
        assert submitted == ["123456"]

    def test_passes_the_code_validator_to_the_prompt(self, monkeypatch, quiet_terminal):
        seen = {}

        def fake_prompt(prompt, after_send=None, accept=None):
            seen["accept"] = accept
            return "123456"

        api = fake_api(requires_2fa=True)
        monkeypatch.setattr(merge, "prompt_telegram_reply", fake_prompt)

        merge._validate_2fa_trusted_device(api)

        assert seen["accept"] is merge._is_two_factor_code


class TestTwoFactorAcceptedNotification:
    """The user submits the code on Telegram, so success has to land there too."""

    def test_notifies_telegram_when_the_code_is_accepted(self, monkeypatch, quiet_terminal):
        sent = []
        monkeypatch.setattr(merge, "send_telegram_message", lambda msg, **k: sent.append(msg))
        monkeypatch.setattr(merge, "prompt_telegram_reply", lambda prompt, after_send=None, accept=None: "123456")
        api = fake_api(requires_2fa=True)

        assert merge._validate_2fa_trusted_device(api) is True
        assert sent == [merge.TELEGRAM_2FA_ACCEPTED_MESSAGE]

    def test_message_says_it_was_accepted(self):
        # Guards the wording: this is the only signal the user gets on Telegram.
        # Asserted literally: this is the only success signal the user gets on
        # Telegram, and comparing against the constant elsewhere would let an
        # unintended rewording through unnoticed.
        assert merge.TELEGRAM_2FA_ACCEPTED_MESSAGE == "✅ Apple 2FA code accepted"

    def test_stays_quiet_when_every_attempt_is_rejected(self, monkeypatch, quiet_terminal):
        sent = []
        monkeypatch.setattr(merge, "send_telegram_message", lambda msg, **k: sent.append(msg))
        monkeypatch.setattr(merge, "prompt_telegram_reply", lambda prompt, after_send=None, accept=None: "000000")
        api = fake_api(requires_2fa=True, validate_result=False)

        assert merge._validate_2fa_trusted_device(api) is False
        assert sent == []

    def test_stays_quiet_when_no_code_arrives(self, monkeypatch, quiet_terminal):
        sent = []
        monkeypatch.setattr(merge, "send_telegram_message", lambda msg, **k: sent.append(msg))
        monkeypatch.setattr(merge, "prompt_telegram_reply", lambda prompt, after_send=None, accept=None: None)
        api = fake_api(requires_2fa=True)

        assert merge._validate_2fa_trusted_device(api) is False
        assert sent == []

    def test_notifies_once_even_after_retries(self, monkeypatch, quiet_terminal):
        sent = []
        codes = iter(["111111", "222222", "333333"])
        monkeypatch.setattr(merge, "send_telegram_message", lambda msg, **k: sent.append(msg))
        monkeypatch.setattr(merge, "prompt_telegram_reply", lambda prompt, after_send=None, accept=None: next(codes))
        api = fake_api(requires_2fa=True)
        api.validate_2fa_code = lambda code: code == "333333"

        assert merge._validate_2fa_trusted_device(api) is True
        assert sent == [merge.TELEGRAM_2FA_ACCEPTED_MESSAGE]

    def test_2sa_path_does_not_send_the_accepted_message(self, monkeypatch, quiet_terminal):
        """2SA prompts on the terminal, so it must not claim Telegram acceptance.

        It does send its own Telegram message announcing the challenge, so the
        assertion is that the accepted message specifically is absent rather than
        that nothing was sent at all.
        """
        sent = []
        monkeypatch.setattr(merge, "send_telegram_message", lambda msg, **k: sent.append(msg))
        monkeypatch.setattr(merge.click, "prompt", lambda *a, **k: 0)
        api = fake_api(requires_2fa=False, requires_2sa=True)

        assert merge.validate_2fa(api) is True
        assert merge.TELEGRAM_2FA_ACCEPTED_MESSAGE not in sent
        assert any("two-step authentication" in message for message in sent)

    def test_fido2_path_does_not_notify(self, monkeypatch, quiet_terminal):
        """validate_2fa ignores the FIDO2 result, so success there is not proven.

        The key is confirmed at the terminal anyway, so there is nobody waiting on
        Telegram to reassure.
        """
        sent = []
        monkeypatch.setattr(merge, "send_telegram_message", lambda msg, **k: sent.append(msg))
        monkeypatch.setattr(merge.click, "prompt", lambda *a, **k: 1)
        api = fake_api(requires_2fa=True, security_key_names=["yubikey"])

        assert merge.validate_2fa(api) is True
        assert sent == []


class TestValidate2faFido2:
    def test_confirms_selected_device(self, monkeypatch, quiet_terminal):
        confirmed = []
        api = fake_api(requires_2fa=True, security_key_names=["yubikey"])
        api.fido2_devices = ["key-a", "key-b"]
        api.confirm_security_key = lambda device: confirmed.append(device)
        monkeypatch.setattr(merge.click, "prompt", lambda *a, **k: 2)

        assert merge._validate_2fa_fido2(api) is True
        assert confirmed == ["key-b"]

    def test_lists_available_devices(self, monkeypatch, quiet_terminal):
        api = fake_api(requires_2fa=True, security_key_names=["yubikey"])
        api.fido2_devices = ["key-a"]
        monkeypatch.setattr(merge.click, "prompt", lambda *a, **k: 1)

        merge._validate_2fa_fido2(api)

        assert any("plug in one of the following keys: yubikey" in line for line in quiet_terminal)
        assert any("1: key-a" in line for line in quiet_terminal)


class TestValidate2fa2sa:
    def test_happy_path(self, monkeypatch):
        api = fake_api(requires_2sa=True)
        monkeypatch.setattr(merge.click, "prompt", lambda *a, **k: 0)
        monkeypatch.setattr(merge, "send_telegram_message", lambda *a, **k: None)

        assert merge._validate_2fa_2sa(api) is True

    def test_returns_false_when_send_fails(self, monkeypatch, quiet_terminal):
        api = fake_api(requires_2sa=True)
        api.send_verification_code = lambda device: False
        monkeypatch.setattr(merge.click, "prompt", lambda *a, **k: 0)

        assert merge._validate_2fa_2sa(api) is False
        assert any("Failed to send verification code" in line for line in quiet_terminal)

    def test_returns_false_when_code_rejected(self, monkeypatch, quiet_terminal):
        api = fake_api(requires_2sa=True)
        api.validate_verification_code = lambda device, code: False
        monkeypatch.setattr(merge.click, "prompt", lambda *a, **k: 0)
        monkeypatch.setattr(merge, "send_telegram_message", lambda *a, **k: None)

        assert merge._validate_2fa_2sa(api) is False
        assert any("Failed to verify verification code" in line for line in quiet_terminal)

    def test_notifies_user_over_telegram(self, monkeypatch):
        api = fake_api(requires_2sa=True)
        sent = []
        monkeypatch.setattr(merge.click, "prompt", lambda *a, **k: 0)
        monkeypatch.setattr(merge, "send_telegram_message", lambda msg, **k: sent.append(msg))

        merge._validate_2fa_2sa(api)

        assert len(sent) == 1
        assert "two-step authentication" in sent[0]

    def test_falls_back_to_phone_number_label(self, monkeypatch, quiet_terminal):
        api = fake_api(requires_2sa=True)
        api.trusted_devices = [{"phoneNumber": "+5491100000000"}]
        monkeypatch.setattr(merge.click, "prompt", lambda *a, **k: 0)
        monkeypatch.setattr(merge, "send_telegram_message", lambda *a, **k: None)

        merge._validate_2fa_2sa(api)

        assert any("SMS to +5491100000000" in line for line in quiet_terminal)
