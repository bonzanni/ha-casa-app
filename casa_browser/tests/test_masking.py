"""Task 7: secret-looking text and raw addresses are masked before any text reaches a model."""
from __future__ import annotations

import pytest

from casa_browser import masking
from casa_browser.masking import MAX_INPUT, mask_text, mask_whole

# Known-format canaries, assembled at run time so no literal key shape sits in the source
# (the push gate's secret scanner would — rightly — refuse one).
J = "".join
CANARIES = {
    "aws": J(["AKIA", "Q3EXAMPLE7CANAR0"]),
    "github": J(["ghp_", "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"]),
    "slack": J(["xox", "b-123456789012-1234567890123-AbCdEfGhIjKlMnOpQrStUvWx"]),
    "stripe": J(["sk_", "live_", "51Hc0aNYCa7c3n4ryK3yAbCdEfGh"]),
    "google": J(["AIza", "SyA1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q"]),
    "openai": J(["sk-", "proj-", "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"]),
    "anthropic": J(["sk-", "ant-", "api03-", "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"]),
    "onepassword": J(["ops_", "eyJzaWduSW5BZGRyZXNzIjoiY2FuYXJ5In0"]),
    "jwt": J(["eyJhbGciOiJIUzI1NiJ9", ".", "eyJzdWIiOiJjYW5hcnkifQ", ".", "c2lnbmF0dXJlY2FuYXJ5MDE"]),
    "entropy": "Zq7Lp2Xv9Rt4Mw8Kc3Nb6Hy1Fd5Gs0Ja",
    "bearer": "Bearer " + "x7R2pQ9vL4mT8kW3nZ6yB1cF5hJ0",
}


@pytest.mark.parametrize("name", sorted(CANARIES))
def test_known_formats_and_entropy_masked(name):
    secret = CANARIES[name]
    out = mask_text(f"Your key is {secret} — keep it safe.")
    assert secret not in out.text
    core = secret.split(" ")[-1]
    assert core[:12] not in out.text and core[-12:] not in out.text
    assert "Your key is" in out.text and "keep it safe" in out.text
    assert out.spans  # where the original text was masked


def test_private_key_block_masked():
    block = "-----BEGIN " + "PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASC\n-----END " + "PRIVATE KEY-----"
    out = mask_text("cert:\n" + block + "\nend")
    assert "MIIEvQIBADANBgkqhkiG9w0BAQEFAASC" not in out.text
    assert "cert:" in out.text and "end" in out.text


@pytest.mark.parametrize("url", [
    "https://files.example.com/invoices/AbC123XyZ/download?token=tok_9f8e7d6c5b4a3210&sig=x",
    "http://example.org/reset#code=123456",
    "https://user:pw" + "@" + "example.net/a",   # userinfo (built so no address shape sits in source)
])
def test_url_text_becomes_origin_and_reference(url):
    out = mask_text(f"Open {url} now.")
    assert "/invoices" not in out.text and "token=" not in out.text and "#code" not in out.text
    assert "user:pw" not in out.text and "/a" not in out.text.split("now")[0][-4:]
    origin = url.split("/")[2].split("@")[-1]
    assert origin in out.text
    assert "Open" in out.text and "now." in out.text


def test_same_url_same_reference_different_url_different():
    a = mask_text("x https://e.example/p/1 y https://e.example/p/1 z https://e.example/p/2").text
    refs = [w for w in a.split() if w.startswith("⟨")]
    assert refs[0] == refs[1] != refs[2]


@pytest.mark.parametrize("plain", [
    "Invoice 2026-10-13 total EUR 1,234.56", "Order number 4711", "Welcome back, Robin",
    "The quick brown fox jumps over the lazy dog", "accountsettingspreferencesoverview",
    "ISBN 978-3-16-148410-0",
])
def test_ordinary_text_is_left_alone(plain):
    assert mask_text(plain).text == plain


def test_short_recovery_code_residual():
    """Documented limit: entropy and known formats do not catch a short code such as a
    recovery code; the page-view gate (only approved pages are observed) is the first layer."""
    assert mask_text("Recovery code: abcd-1234").text == "Recovery code: abcd-1234"


def test_mask_is_idempotent_and_bounded():
    once = mask_text(CANARIES["github"] + " " + "https://a.example/b").text
    assert mask_text(once).text == once
    assert len(mask_text("x" * 100000, limit=500).text) <= 520


def test_entropy_threshold():
    assert masking.looks_random("Zq7Lp2Xv9Rt4Mw8Kc3Nb6Hy1Fd5Gs0Ja")
    assert not masking.looks_random("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa1")
    assert not masking.looks_random("abcdefghijklmnopqrstuvwxyz")  # letters only



def test_a_credential_inside_an_address_does_not_spare_the_rest_of_it():
    key = CANARIES["github"]
    out = mask_text(f"see https://site.example/private/download?key={key}&invoice=123#secret ok")
    assert "/private" not in out.text and "invoice=123" not in out.text and "#secret" not in out.text
    assert key not in out.text and "https://site.example ⟨r1⟩" in out.text


def test_an_address_is_reduced_wherever_it_starts():
    """An address glued to a preceding word character is still an address."""
    out = mask_text("Open_https://site.example/reset?code=private-code").text
    assert "private-code" not in out and "/reset" not in out and "https://site.example" in out


def test_the_whole_text_is_masked_before_it_is_cut():
    """A credential straddling the output limit is masked whole, never cut into a prefix."""
    key = CANARIES["github"]
    text = "ok " * 6660 + key + " tail"
    out = mask_text(text).text
    assert len(out) <= masking.MAX_TEXT
    assert key[:8] not in out
    long = "w " * ((masking.MAX_INPUT - 10) // 2) + key        # the input cut falls inside the key
    assert key[:8] not in mask_text(long, limit=masking.MAX_INPUT).text


def test_an_address_runs_to_whitespace():
    """Characters a page may leave inside an address (an apostrophe in a name) do not end it."""
    out = mask_text("https://site.example/people/O'Connor/private-invoice?account=7781").text
    assert out == "https://site.example ⟨r1⟩"
    assert mask_text("see 'https://a.example/x?y=1'.").text == "see 'https://a.example ⟨r1⟩'."


@pytest.mark.parametrize("text,expected", [
    ("https://site.example\\private-invoice\\account-7781?code=991", "https://site.example ⟨r1⟩"),
    ("ftp://user:Hunter2pw" + chr(64) + "files.bank.example/statements/", "ftp://files.bank.example ⟨r1⟩"),
    ("wss://bank.example/socket?session=ab12cd34", "wss://bank.example ⟨r1⟩"),
    ("https://site.example%2Fprivate/x", "link ⟨r1⟩"),
    ("https://[::1]:8443/p?q=1", "https://[::1]:8443 ⟨r1⟩"),
    ("x://files.example/private/invoice?token=abc123", "x://files.example ⟨r1⟩"),
])
def test_an_address_of_any_scheme_shows_only_a_plain_origin(text, expected):
    """Any scheme is an address; a backslash a browser reads as a path separator ends the
    host; a host that is not plainly a host shows as ``link``."""
    assert mask_text(text).text == expected


def test_masking_stays_linear_on_long_words():
    """A page-long word must not make address matching quadratic."""
    import time
    start = time.monotonic()
    mask_text("a" * masking.MAX_INPUT)
    mask_text("a1" * (masking.MAX_INPUT // 2))
    assert time.monotonic() - start < 3



@pytest.mark.parametrize("text,expected", [
    ("https:\\\\site.example\\private-invoice?account=7781", "https://site.example ⟨r1⟩"),
    ("https:site.example/reset?code=991", "link ⟨r1⟩"),         # no separators: no host shown
    ("https:/private-invoice?account=7781", "link ⟨r1⟩"),       # one separator: a path
    ("file:///private-invoice/account-7781", "link ⟨r1⟩"),      # file: never shows a host
    ("HTTPS:/\\site.example/x?y=1", "https://site.example ⟨r1⟩"),
    ("wss:\\\\live.example\\socket?session=ab12", "wss://live.example ⟨r1⟩"),
    ("Open_https:site.example/reset?c=1", "Open_link ⟨r1⟩"),
    ("file: notes.txt", "file: notes.txt"),          # a colon and a space is not an address
    ("x:/foo/bar", "x:/foo/bar"),                    # other schemes need //
])
def test_special_schemes_follow_the_url_standard(text, expected):
    """For http, https, ws, wss, ftp and file the URL standard accepts any run of slashes or
    backslashes after the colon, or none: every such form is an address (operator ruling
    E-C2-5 A); a host is shown only after two or more separators, never for file:."""
    assert mask_text(text).text == expected


def test_a_page_full_of_addresses_masks_whole_and_quickly():
    """Masking lengthens this text past its input limit: nothing is cut; and it stays linear, so
    a page of the input limit's size never holds up the controller."""
    import time
    raw = "\n".join(f"https://a.example/{i % 7}" for i in range(20_000))
    assert len(raw) < MAX_INPUT
    started = time.monotonic()
    text, whole, masked = mask_whole(raw)
    assert time.monotonic() - started < 3
    assert whole and masked and len(text) > MAX_INPUT and text.count("⟨r") == 20_000
    assert mask_text(raw).text == text[:20_000]
    assert mask_whole("plain words") == ("plain words", True, False)
    over = "word " * (MAX_INPUT // 5 + 1)
    text, whole, _ = mask_whole(over)
    assert not whole and len(text) <= MAX_INPUT
