"""Tests for log_redact.py -- secret redaction."""

import pytest

from log_redact import redact

# Without this marker the tier2 unit gate (-m "unit and not docker and not
# slow") silently skips this whole file.
pytestmark = pytest.mark.unit


class TestRedact:
    def test_redacts_sk_token(self):
        text = "Using key sk-abcdefghijklmnopqrstuvwxyz1234567890"
        result = redact(text)
        assert "sk-abcdefghijklmnopqrst" in result
        assert "uvwxyz1234567890" not in result

    def test_redacts_ghp_token(self):
        text = "token: ghp_abcd1234567890abcdef1234567890abcdef"  # gitleaks:allow - synthetic fixture; this test exists to prove redaction works
        result = redact(text)
        assert "ghp_abcd" in result
        assert "1234567890abcdef1234567890abcdef" not in result

    def test_redacts_bearer_token(self):
        text = "Authorization: Bearer eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.long.token"
        result = redact(text)
        assert "Bearer ***" in result
        assert "eyJhbGci" not in result

    def test_redacts_key_value_pattern(self):
        text = 'api_key: "sk_test_abcdefghijklmnop1234567890"'  # gitleaks:allow - synthetic fixture; this test exists to prove redaction works
        result = redact(text)
        assert "sk_test_" in result
        assert "1234567890" not in result

    def test_preserves_normal_text(self):
        text = "This is a normal log message with no secrets"
        assert redact(text) == text

    def test_preserves_short_values(self):
        text = "token: abc"
        # Short values (< 8 chars after key) should not be redacted
        assert redact(text) == text


class TestRedactAnthropicKeys:
    """M19 (v0.50.0): Casa's own primary key format (sk-ant-...) must be
    redacted. Pre-fix the sk- pattern required 20 contiguous alphanumerics
    after 'sk-', which the hyphen after 'ant' broke, so the key passed
    through logs unredacted."""

    def test_redacts_anthropic_api_key(self):
        secret_body = "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789-AA"
        text = f"spawned claude with sk-ant-api03-{secret_body}"
        result = redact(text)
        assert secret_body not in result
        assert "sk-ant-api03-" in result  # prefix retained for identification

    def test_redacts_anthropic_oauth_token(self):
        # mirrors sdk_logging.py's verbatim CLI-stderr echo path
        secret_body = "AbCd_EfGh-IjKlMnOpQrStUv"
        text = f"stderr Error: invalid credential sk-ant-oat01-{secret_body}"
        result = redact(text)
        assert secret_body not in result
        assert "sk-ant-oat01-" in result


class TestExactValueRegistration:
    """Release A (Task 6): per-trigger webhook secrets are opaque and match no
    generic pattern, so they are registered for exact-value redaction."""

    def setup_method(self):
        from log_redact import _reset_registered_secrets
        _reset_registered_secrets()

    def teardown_method(self):
        from log_redact import _reset_registered_secrets
        _reset_registered_secrets()

    def test_registered_value_is_redacted(self):
        from log_redact import register_secret
        register_secret("whsec_opaqueProviderValue123")
        out = redact("delivering with secret whsec_opaqueProviderValue123 now")
        assert "whsec_opaqueProviderValue123" not in out
        assert "delivering with secret" in out

    def test_unregistered_value_untouched(self):
        out = redact("plain harmless text 12345")
        assert "plain harmless text 12345" in out

    def test_short_or_empty_values_not_registered(self):
        from log_redact import register_secret
        register_secret("")      # ignored — would blank everything
        register_secret("abc")   # too short to be a meaningful secret
        out = redact("abc and more")
        assert "abc and more" in out


class TestRedactingFilterArgs:
    """#214: python-telegram-bot logs Bot API call parameters as a DICT
    positional arg at DEBUG (``telegram._bot`` line 732: ``"Calling Bot API
    endpoint `%s` with parameters `%s`", endpoint, data``). The setWebhook
    ``secret_token`` rides inside that non-string ``data`` dict, which the
    filter's per-arg redaction skipped (only top-level ``str`` args were
    redacted) and which redaction never saw because it ran before %-format.
    The filter must redact secrets embedded in ANY arg (dict/list/str)."""

    def setup_method(self):
        from log_redact import _reset_registered_secrets
        _reset_registered_secrets()

    def teardown_method(self):
        from log_redact import _reset_registered_secrets
        _reset_registered_secrets()

    def _emit(self, record):
        import logging as _logging
        from log_redact import RedactingFilter
        assert RedactingFilter().filter(record) is True
        # Mirror what a handler's formatter does — this is what actually
        # reaches stdout, and the secret must be gone from it.
        return _logging.Formatter("%(message)s").format(record)

    def _record(self, msg, args):
        import logging as _logging
        return _logging.LogRecord(
            name="telegram.Bot", level=_logging.DEBUG,
            pathname=__file__, lineno=1, msg=msg, args=args, exc_info=None,
        )

    def test_registered_secret_in_dict_arg_redacted(self):
        from log_redact import register_secret
        register_secret("AAsecretTokenValue12345")
        record = self._record(
            "Calling Bot API endpoint `%s` with parameters `%s`",
            ("setWebhook",
             {"url": "https://x/y", "secret_token": "AAsecretTokenValue12345"}),
        )
        out = self._emit(record)
        assert "AAsecretTokenValue12345" not in out
        assert "setWebhook" in out  # non-secret context preserved

    def test_pattern_secret_in_dict_arg_redacted(self):
        record = self._record(
            "params `%s`",
            ({"secret_token": "sk-abcdefghijklmnopqrstuvwxyz1234567890"},),
        )
        out = self._emit(record)
        assert "uvwxyz1234567890" not in out

    def test_plain_string_arg_still_redacted(self):
        from log_redact import register_secret
        register_secret("whsec_opaqueProviderValue123")
        record = self._record("delivering %s now", ("whsec_opaqueProviderValue123",))
        out = self._emit(record)
        assert "whsec_opaqueProviderValue123" not in out
        assert "delivering" in out and "now" in out

    def test_no_args_message_redacted(self):
        record = self._record(
            "token: ghp_abcd1234567890abcdef1234567890abcdef", None)  # gitleaks:allow - synthetic fixture; this test exists to prove redaction works
        out = self._emit(record)
        assert "1234567890abcdef1234567890abcdef" not in out

    def test_short_secret_token_masked_by_key_name(self):
        # Sol review: a short secret_token is < the 8-char registration floor
        # and matches no generic pattern; key-name masking is what guarantees
        # it can't leak from PTB's parameter dict.
        record = self._record(
            "Calling Bot API endpoint `%s` with parameters `%s`",
            ("setWebhook", {"url": "https://x/y", "secret_token": "sh0rt"}),
        )
        out = self._emit(record)
        assert "sh0rt" not in out
        assert "setWebhook" in out and "https://x/y" in out

    def test_benign_key_label_not_over_redacted(self):
        # The bare word "key" is a common non-secret dict key (cache/routing);
        # it must survive so logs stay useful.
        record = self._record(
            "%s", ({"key": "voice-latency", "resume": "False"},))
        out = self._emit(record)
        assert "voice-latency" in out


class TestExceptionRedaction:
    """#285: a secret inside an exception message or traceback must not
    reach the log. Redaction happens where the traceback becomes a string —
    Casa's formatters (log_cid) — because the RedactingFilter runs before
    formatting and never sees the rendered text."""

    def _record_with_exc(self, secret):
        import logging as _logging
        import sys as _sys
        try:
            raise RuntimeError(f"boom {secret}")
        except RuntimeError:
            record = _logging.LogRecord(
                name="casa", level=_logging.ERROR, pathname=__file__,
                lineno=1, msg="handled failure", args=(),
                exc_info=_sys.exc_info(),
            )
        record.cid = "-"  # normally injected by the record factory
        return record

    def test_registered_secret_in_exception_redacted_json(self):
        from log_cid import JsonFormatter
        from log_redact import register_secret
        register_secret("whsec_excJsonSecret9876")
        record = self._record_with_exc("whsec_excJsonSecret9876")
        out = JsonFormatter().format(record)
        assert "whsec_excJsonSecret9876" not in out
        assert "RuntimeError" in out  # traceback context preserved

    def test_registered_secret_in_exception_redacted_human(self):
        from log_cid import _human_formatter
        from log_redact import register_secret
        register_secret("whsec_excHumanSecret9876")
        record = self._record_with_exc("whsec_excHumanSecret9876")
        out = _human_formatter().format(record)
        assert "whsec_excHumanSecret9876" not in out
        assert "RuntimeError" in out

    def test_pattern_secret_in_exception_redacted_json(self):
        from log_cid import JsonFormatter
        record = self._record_with_exc(
            "sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWxYz123456")
        out = JsonFormatter().format(record)
        assert "AbCdEfGhIjKlMnOpQrStUvWxYz123456" not in out

    def test_precached_exc_text_redacted_human(self):
        # Another handler's formatter may have already cached record.exc_text
        # (unredacted) before Casa's formatter runs; the cache must not
        # bypass redaction.
        import logging as _logging
        from log_cid import _human_formatter
        from log_redact import register_secret
        register_secret("whsec_cachedExcSecret42")
        record = self._record_with_exc("whsec_cachedExcSecret42")
        record.exc_text = _logging.Formatter().formatException(record.exc_info)
        assert "whsec_cachedExcSecret42" in record.exc_text
        out = _human_formatter().format(record)
        assert "whsec_cachedExcSecret42" not in out

    def test_stack_info_redacted_human(self):
        import logging as _logging
        from log_cid import _human_formatter
        from log_redact import register_secret
        register_secret("whsec_stackSecret4242")
        record = _logging.LogRecord(
            name="casa", level=_logging.ERROR, pathname=__file__, lineno=1,
            msg="m", args=(), exc_info=None,
        )
        record.cid = "-"
        record.stack_info = "Stack:\n  token whsec_stackSecret4242 here"
        out = _human_formatter().format(record)
        assert "whsec_stackSecret4242" not in out


class TestExtrasRedaction:
    """#285 companion gap: structured extras are flattened into the payload
    by the formatters after the filter has run — redact them there with the
    same per-value walk used for record.args (sensitive key names masked
    wholesale; benign labels like key=voice-latency preserved)."""

    def _record_with_extra(self, extra):
        import logging as _logging
        record = _logging.LogRecord(
            name="casa", level=_logging.INFO, pathname=__file__, lineno=1,
            msg="evt", args=(), exc_info=None,
        )
        record.cid = "-"  # normally injected by the record factory
        for k, v in extra.items():
            setattr(record, k, v)
        return record

    def test_sensitive_key_extra_masked_json(self):
        from log_cid import JsonFormatter
        record = self._record_with_extra({"api_key": "sh0rt"})
        out = JsonFormatter().format(record)
        assert "sh0rt" not in out

    def test_sensitive_key_extra_masked_human(self):
        from log_cid import _human_formatter
        record = self._record_with_extra({"secret_token": "sh0rt"})
        out = _human_formatter().format(record)
        assert "sh0rt" not in out

    def test_registered_secret_in_extra_value_redacted_json(self):
        from log_cid import JsonFormatter
        from log_redact import register_secret
        register_secret("whsec_extraValSecret77")
        record = self._record_with_extra(
            {"detail": "delivery whsec_extraValSecret77 failed"})
        out = JsonFormatter().format(record)
        assert "whsec_extraValSecret77" not in out
        assert "delivery" in out

    def test_benign_extra_preserved(self):
        from log_cid import JsonFormatter, _human_formatter
        record = self._record_with_extra({"key": "voice-latency", "n": 3})
        assert "voice-latency" in JsonFormatter().format(record)
        record2 = self._record_with_extra({"key": "voice-latency"})
        assert "voice-latency" in _human_formatter().format(record2)


class TestNonStringValueRedaction:
    """#285 round 2 (Sol + Terra): non-str values leaked because they were
    stringified only at render time — after every redaction pass. A bytes or
    list value under a credential key bypassed wholesale masking; an
    exception object carrying a registered secret bypassed redact()."""

    def test_bytes_under_credential_key_masked(self):
        from log_redact import redact_extras
        out = redact_extras({"api_key": b"opaque-secret"})
        assert b"opaque-secret" not in repr(out).encode()
        assert out["api_key"] == "«redacted»"

    def test_list_under_credential_key_masked(self):
        from log_redact import redact_extras
        out = redact_extras({"api_key": ["sh0rt"]})
        assert "sh0rt" not in repr(out)

    def test_exception_object_extra_is_stringified_and_redacted(self):
        from log_cid import JsonFormatter
        from log_redact import register_secret
        import logging as _logging
        register_secret("whsec_objExtraSecret55")
        record = _logging.LogRecord(
            name="casa", level=_logging.ERROR, pathname=__file__, lineno=1,
            msg="evt", args=(), exc_info=None,
        )
        record.cid = "-"
        record.error = RuntimeError("boom whsec_objExtraSecret55")
        out = JsonFormatter().format(record)
        assert "whsec_objExtraSecret55" not in out
        assert "boom" in out  # non-secret text preserved

    def test_exception_object_arg_is_redacted_at_filter(self):
        # logger.error("failed: %s", exc) — the exception is stringified by
        # %-formatting at render time; the filter must pre-stringify it.
        import logging as _logging
        from log_redact import RedactingFilter, register_secret
        register_secret("whsec_argObjSecret66")
        record = _logging.LogRecord(
            name="casa", level=_logging.ERROR, pathname=__file__, lineno=1,
            msg="failed: %s", args=(RuntimeError("whsec_argObjSecret66"),),
            exc_info=None,
        )
        RedactingFilter().filter(record)
        assert "whsec_argObjSecret66" not in record.getMessage()
        assert "failed:" in record.getMessage()

    def test_numeric_args_pass_through_unchanged(self):
        # %d/%f formatting must keep working — safe scalars are not touched.
        import logging as _logging
        from log_redact import RedactingFilter
        record = _logging.LogRecord(
            name="casa", level=_logging.INFO, pathname=__file__, lineno=1,
            msg="count=%d ratio=%.2f", args=(5, 0.25), exc_info=None,
        )
        RedactingFilter().filter(record)
        assert record.getMessage() == "count=5 ratio=0.25"


class TestMappingArgsRedaction:
    """#285 round 3 (Terra): logger.info("%(api_key)s", {...}) hits the
    args-is-dict branch, which walked VALUES only — the credential-key
    wholesale masking never ran, so an opaque non-str value under a
    credential key leaked at %-format time."""

    def _mapping_record(self, msg, mapping):
        import logging as _logging
        # Canonical construction: logging unwraps a single-Mapping tuple
        # into record.args — the same path a real logger.info() takes.
        return _logging.LogRecord(
            name="casa", level=_logging.INFO, pathname=__file__, lineno=1,
            msg=msg, args=(mapping,), exc_info=None,
        )

    def test_mapping_args_credential_key_masked(self):
        from log_redact import RedactingFilter
        record = self._mapping_record(
            "key=%(api_key)s status=%(status)s",
            {"api_key": b"opaque-secret", "status": "ok"},
        )
        RedactingFilter().filter(record)
        out = record.getMessage()
        assert "opaque-secret" not in out
        assert "status=ok" in out

    def test_mapping_args_userdict_masked(self):
        # LogRecord unwraps ANY Mapping — UserDict must not bypass (Sol r3).
        from collections import UserDict
        from log_redact import RedactingFilter
        record = self._mapping_record(
            "key=%(api_key)s", UserDict({"api_key": b"opaque-secret"}),
        )
        RedactingFilter().filter(record)
        assert "opaque-secret" not in record.getMessage()

    def test_mapping_merge_leak_via_benign_key_closed(self):
        # "password=%(value)s" with the secret under a BENIGN key name: only
        # the MERGED text shows the credential context (Sol r3).
        from log_redact import RedactingFilter
        record = self._mapping_record(
            "password=%(value)s", {"value": "opaqueCredential12345"},
        )
        RedactingFilter().filter(record)
        assert "opaqueCredential12345" not in record.getMessage()


class TestMsgObjectAndNumericSafety:
    """#285 round 3 (Sol): a non-str record.msg OBJECT is stringified by
    getMessage() after the filter (leaking a registered secret); and the
    round-2 stringify fallback must not break numeric formatting (Decimal
    under %.2f) or mask numeric telemetry (input_tokens)."""

    def test_msg_object_with_registered_secret_redacted(self):
        import logging as _logging
        from log_redact import RedactingFilter, register_secret
        register_secret("whsec_msgObjSecret88")
        record = _logging.LogRecord(
            name="casa", level=_logging.ERROR, pathname=__file__, lineno=1,
            msg=RuntimeError("boom whsec_msgObjSecret88"), args=(),
            exc_info=None,
        )
        RedactingFilter().filter(record)
        assert "whsec_msgObjSecret88" not in record.getMessage()
        assert "boom" in record.getMessage()

    def test_decimal_arg_still_formats_under_percent_f(self):
        import logging as _logging
        from decimal import Decimal
        from log_redact import RedactingFilter
        record = _logging.LogRecord(
            name="casa", level=_logging.INFO, pathname=__file__, lineno=1,
            msg="ratio=%.2f", args=(Decimal("0.25"),), exc_info=None,
        )
        RedactingFilter().filter(record)
        assert record.getMessage() == "ratio=0.25"

    def test_numeric_token_telemetry_extras_not_masked(self):
        from log_redact import redact_extras
        out = redact_extras(
            {"input_tokens": 123, "token_count": 7, "token_budget": 800})
        assert out == {
            "input_tokens": 123, "token_count": 7, "token_budget": 800}

    def test_numeric_credential_masked(self):
        # Terra r3: a numeric PIN/password IS a credential — the numeric
        # exemption is ONLY for token-count telemetry keys.
        from log_redact import redact_extras
        out = redact_extras({"password": 123456789, "secret_pin": 9999})
        assert 123456789 not in out.values()
        assert 9999 not in out.values()

    def test_literal_value_starting_with_percent_paren_still_redacted(self):
        # Terra r3: the placeholder lookahead must only skip COMPLETE
        # placeholders, not any opaque value that happens to start with "%(".
        assert "opaqueSecretValue" not in redact("api_key=%(opaqueSecretValue")

    def test_registered_secret_as_extras_key_redacted(self):
        from log_redact import redact_extras, register_secret
        register_secret("whsec_dynamicKey9876")
        out = redact_extras({"whsec_dynamicKey9876": "ok"})
        assert "whsec_dynamicKey9876" not in out


def test_a_telegram_bot_token_keeps_its_bot_id_and_loses_its_secret():
    """A bot token in a Bot API URL or on its own: the id after redaction,
    never the secret part."""
    from log_redact import redact
    secret = "AAH" + "x9_Kq-" * 6
    for line in (f"HTTP Request: POST https://api.telegram.org/bot123456789:{secret}/getMe",
                 f"123456789:{secret}"):
        out = redact(line)
        assert secret not in out and "123456789:***" in out, out
    assert secret not in redact(f"token=123456789:{secret}")
