"""Task 10 in the image: branded Chrome as the browser user, the downloader as the controller
user, through the enforcing proxy (harness on the production port) to a synthetic site.

* A button that builds a ``blob:`` PDF and clicks a temporary download link (the measured
  RunPod shape): Chrome downloads it into the shared downloads directory and the controller
  keeps it — only from a page that belongs to the session — and Chrome's copy is removed.
* A token link that opens in a new tab (the measured Bergfreunde shape): the controller fetches
  the address itself, no popup and no download event needed, with no cookie on the
  capability host.
* A static ``blob:`` link: read inside the session's page.
* A link that redirects off the site to a file host (design §4): the downloader's hops meet the
  floor only, so the file arrives — with the account cookie on the site's own hop and none on
  the file host's.
"""
from __future__ import annotations

import base64

import pytest

import proxyharness as h
from browserprobe import run_scenario
from casa_browser.proxy_client import encode_policy
from sitefixture import ChromeSites

pytestmark = pytest.mark.docker

SITE, CAP = "site-a.example", "files-b.example"
TOKEN = "Zq81Yt7Wm2Pk5Rn9Xs4Bv6Cd3Lh0Fg"  # gitleaks:allow - synthetic fixture; this test exists to prove masking works
TOKEN_URL = f"https://{CAP}/i/invoice?token={TOKEN}"
PDF = b"%PDF-1.7\n% synthetic invoice\n" + b"1" * 4000 + b"\n%%EOF\n"
PDF_JS = base64.b64encode(PDF).decode()
PAGE = f"""<html><head><title>Billing</title></head><body>
<h1>Billing</h1>
<button id=gen>Download invoice PDF</button>
<a id=tok href="{TOKEN_URL}" target=_blank>Download factuur</a>
<a id=saved download="saved copy.pdf">Saved PDF</a>
<script>
  document.getElementById('gen').addEventListener('click', () => {{
    const bytes = Uint8Array.from(atob('{PDF_JS}'), c => c.charCodeAt(0));
    const url = URL.createObjectURL(new Blob([bytes], {{type: 'application/pdf'}}));
    const a = document.createElement('a');
    a.href = url;
    a.download = 'runpod invoice 2026-10.pdf';
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 5000);
  }});
  const b = Uint8Array.from(atob('{PDF_JS}'), c => c.charCodeAt(0));
  document.getElementById('saved').href = URL.createObjectURL(new Blob([b], {{type: 'application/pdf'}}));
  document.cookie = 'sess=SiteSession123; path=/; secure';
</script>
</body></html>""".encode()


def _policy(on_site=True):
    """The account session of the site; ``on_site=False``: a site whose origins no longer
    hold the billing page, so that page does not belong to the session."""
    site = h.site(origins=(f"https://{SITE}" if on_site else "https://moved.example",), login=())
    return h.account_policy(sites=(site,))


def test_blob_and_popup_pdf():
    with ChromeSites([SITE, CAP]) as sites:
        sites.pages["/billing"] = ("text/html", PAGE, {})
        sites.pages["/i/invoice"] = ("application/octet-stream", PDF,
                                     {"Content-Disposition": 'attachment; filename="INV_7_2026.pdf"'})
        pol = _policy()
        sites.install(pol)
        wire = encode_policy(pol)
        out = run_scenario([
            {"open": {"site_id": "s1"}},
            {"goto": f"https://{SITE}/billing"},
            {"capture_download": {"click": "#gen", "policy": wire}},
            {"fetch_link": {"label": "Download factuur", "policy": wire}},
            {"fetch_link": {"label": "Saved PDF", "policy": wire}},
            # The same page, no longer the session's page: nothing is kept.
            {"capture_download": {"click": "#gen", "policy": encode_policy(_policy(False))}},
        ], timeout=240)
        cap_requests = [r for r in sites.receipts.requests if r[1] == CAP]
        cap_cookies = [c for c in sites.receipts.cookies if c[0] == CAP]
    _, status, blob, link, saved, off_session = out
    assert status == 200
    # Chrome wrote its copy as the browser user into the shared directory; the controller
    # read it and removed it.
    assert blob["landed_dir"] == "/data/downloads" and blob["landed_uid"] == 1100
    assert blob["left_in_downloads"] == []
    assert blob["file"]["mime"] == "application/pdf"
    assert blob["file"]["safe_name"] == "runpod invoice 2026-10.pdf"
    assert base64.b64decode(blob["file"]["head"]).startswith(b"%PDF-")
    assert blob["file"]["size"] == len(PDF) and blob["file"]["mode"] == "0o600"
    # The token link: fetched by the controller through the proxy; no popup was opened.
    assert link["pages"] == 1
    assert (link["file"]["mime"], link["file"]["safe_name"]) == ("application/pdf", "INV_7_2026.pdf")
    assert TOKEN not in str(link) and len(cap_requests) == 1 and cap_cookies == []
    assert (saved["file"]["mime"], saved["file"]["safe_name"]) == ("application/pdf", "saved copy.pdf")
    assert off_session["refused"] == "blocked" and off_session["left_in_downloads"] == []


def test_offsite_redirect_download_reaches_the_file_host_without_the_account_cookie():
    with ChromeSites([SITE, CAP]) as sites:
        sites.pages["/billing"] = ("text/html", PAGE.replace(
            f'href="{TOKEN_URL}"'.encode(), f'href="https://{SITE}/dl/7"'.encode()), {})
        sites.pages["/dl/7"] = ("text/plain", b"", {"Location": f"https://{CAP}/cdn/7.pdf"})
        sites.status["/dl/7"] = 302
        sites.pages["/cdn/7.pdf"] = ("application/pdf", PDF, {})
        pol = _policy()
        sites.install(pol)
        out = run_scenario([
            {"open": {"site_id": "s1"}},
            {"goto": f"https://{SITE}/billing"},
            {"wait": 1},
            {"fetch_link": {"label": "Download factuur", "policy": encode_policy(pol)}},
        ], timeout=240)
        hops = [(r[1].split(":")[0], r[2]) for r in sites.receipts.requests if r[2] in ("/dl/7", "/cdn/7.pdf")]
        site_cookies = [c for c in sites.receipts.cookies if c[0].split(":")[0] == SITE and c[1] == "/dl/7"]
        cap_cookies = [c for c in sites.receipts.cookies if c[0].split(":")[0] == CAP]
        events = [(e.listener, e.allow, e.reason) for e in sites.events() if e.origin == f"https://{CAP}"]
    fetched = out[3]
    assert fetched.get("file", {}).get("mime") == "application/pdf", fetched
    assert fetched["file"]["size"] == len(PDF)
    assert hops == [(SITE, "/dl/7"), (CAP, "/cdn/7.pdf")], hops
    # the account cookie went back to its own site's hop (the positive control) and never to
    # the file host
    assert site_cookies and "SiteSession123" in site_cookies[0][2], site_cookies
    assert cap_cookies == []
    # the hop was decided on the downloader's own listener, as a download hop
    assert ("download", True, "download_hop") in events, events
