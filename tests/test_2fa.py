"""Tests for the iCloud 2FA / 2SA branches."""

from collections.abc import Callable

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

    def test_trusted_device_failure_short_circuits(self, monkeypatch):
        monkeypatch.setattr(merge, "_validate_2fa_trusted_device", lambda api: False)
        trust_calls = []
        monkeypatch.setattr(merge, "_request_session_trust", lambda api: trust_calls.append(api))
        api = fake_api(requires_2fa=True)

        assert merge.validate_2fa(api) is False
        # Trust must not be requested when validation failed.
        assert trust_calls == []

    def test_requests_session_trust_after_success(self, monkeypatch):
        monkeypatch.setattr(merge, "_validate_2fa_trusted_device", lambda api: True)
        trust_calls = []
        monkeypatch.setattr(merge, "_request_session_trust", lambda api: trust_calls.append(api))
        api = fake_api(requires_2fa=True)

        assert merge.validate_2fa(api) is True
        assert trust_calls == [api]

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


class TestValidate2faTrustedDevice:
    def test_disables_sms_fallback(self, monkeypatch):
        # pyicloud would otherwise switch delivery to SMS when the
        # trusted-device bridge times out, rejecting the pushed code.
        monkeypatch.setattr(merge, "prompt_telegram_reply", lambda prompt, after_send=None: "123456")
        api = fake_api(requires_2fa=True)

        merge._validate_2fa_trusted_device(api)

        assert api._can_request_sms_2fa_code() is False

    def test_returns_false_when_no_code_arrives(self, monkeypatch, quiet_terminal):
        monkeypatch.setattr(merge, "prompt_telegram_reply", lambda prompt, after_send=None: None)
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
        monkeypatch.setattr(merge, "prompt_telegram_reply", lambda prompt, after_send=None: "654321")

        assert merge._validate_2fa_trusted_device(api) is True
        assert seen["code"] == "654321"

    def test_reports_rejected_code(self, monkeypatch, quiet_terminal):
        monkeypatch.setattr(merge, "prompt_telegram_reply", lambda prompt, after_send=None: "000000")
        api = fake_api(requires_2fa=True, validate_result=False)

        assert merge._validate_2fa_trusted_device(api) is False
        assert any("Failed to verify security code" in line for line in quiet_terminal)

    def test_after_send_callback_requests_the_code(self, monkeypatch, quiet_terminal):
        # The callback is what actually triggers Apple's push; run it to be sure
        # it reports the delivery method rather than throwing.
        requested = []
        api = fake_api(requires_2fa=True)
        api.request_2fa_code = lambda: requested.append(True)

        def fake_prompt(prompt, after_send=None):
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

        def fake_prompt(prompt, after_send=None):
            after_send()
            return "222222"

        monkeypatch.setattr(merge, "prompt_telegram_reply", fake_prompt)

        # A failed push must not abort the flow: the code often still arrives.
        assert merge._validate_2fa_trusted_device(api) is True
        assert any("2FA request warning: bridge timeout" in line for line in quiet_terminal)


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
