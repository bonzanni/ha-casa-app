"""Task 14: the operator pages in the effective locale's language — every catalog complete and
consistent, every page rendered in every language with ``lang`` naming the language shown, and
Home Assistant's option translations in step with the schema."""
from __future__ import annotations

import html as htmllib
import json
import re

import pytest
import yaml

from casa_browser.i18n import BASE, Text, available, catalog, language_for, placeholders
from casa_browser.protocol import EffectiveLocale
from casa_browser.ui import make_ui
from conftest import APP_DIR
from controllerfakes import Rig
from uihelpers import Operator, waiting_run

LANGUAGES = available()


def test_catalogs_are_complete_and_consistent():
    assert BASE in LANGUAGES and len(LANGUAGES) >= 2
    base = catalog(BASE)
    for language in LANGUAGES:
        own = catalog(language)
        assert set(own) == set(base), (language, set(own) ^ set(base))
        for key, template in own.items():
            assert template.strip(), (language, key)
            assert placeholders(template) == placeholders(base[key]), (language, key)
            # a template is text: markup comes only from the page code around it
            assert "<" not in template and ">" not in template, (language, key)


@pytest.mark.parametrize("tag, expected", [
    ("it-IT", "it"), ("nl", "nl"), ("de-CH", "de"), ("en-GB", "en"), ("it_IT", "it"),
    ("fr-FR", "en"), (None, "en"), ("", "en"), ("../en", "en"), ("EN", "en")])
def test_language_for(tag, expected):
    if expected != "en" and expected not in LANGUAGES:
        pytest.skip(f"no {expected} catalog")
    assert language_for(tag) == expected


def test_html_escapes_the_template_and_keeps_the_values():
    t = Text("en")
    out = t.html("run.by", requester='<strong>S</strong>', mode="&lt;t&gt;")
    assert out == 'Requested by <strong>S</strong> · &lt;t&gt;'
    # run.by has nothing to escape; a shipped template with a quote shows the escaping itself
    assert t.html("sites.forgotten") == "The site&#x27;s sign-in was forgotten."


def test_option_translations_agree_with_the_schema():
    schema = yaml.safe_load((APP_DIR / "config.yaml").read_text())["schema"]
    files = sorted((APP_DIR / "translations").glob("*.yaml"))
    assert {f.stem for f in files} == set(LANGUAGES)
    for f in files:
        conf = yaml.safe_load(f.read_text())["configuration"]
        assert set(conf) == set(schema), f.name
        for key, entry in conf.items():
            assert set(entry) == {"name", "description"} and entry["name"] and entry["description"], (f.name, key)


@pytest.fixture
def login_store():
    """Where the ``localized`` rig keeps logins; a test parametrizes it to change that."""
    return "onepassword"


@pytest.fixture(params=[*LANGUAGES, "fr"])
async def localized(request, tmp_path, aiohttp_client, login_store):
    tag = {"en": "en-GB", "it": "it-IT", "nl": "nl-NL", "de": "de-DE", "fr": "fr-FR"}.get(
        request.param, request.param)
    rig = Rig(tmp_path, locale=EffectiveLocale(tag, "Europe/Amsterdam", "home_assistant"),
              login_store=login_store)
    await rig.start()
    client = await aiohttp_client(make_ui(rig.ctl))
    yield rig, Operator(client), (request.param if request.param in LANGUAGES else BASE)
    await rig.live.end_all()
    await rig.ctl.shutdown()
    rig.close()


def _text(page: str) -> str:
    return htmllib.unescape(re.sub(r"<[^>]+>", " ", page))


async def test_every_page_in_the_effective_language(localized):
    rig, operator, language = localized
    t = Text(language)
    run_id = await waiting_run(rig)
    pages = {}
    for path in ("/", f"/runs/{run_id}", "/sites", "/sites/new", "/setup", "/pair"):
        status, page, _ = await operator.get(path)
        assert status == 200, (path, page[:300])
        assert f'<html lang="{language}">' in page, path
        assert "{" not in _text(page), path          # no template left unfilled
        pages[path] = page
    assert t("home.wait_live") in _text(pages["/"])
    assert t("sites.add") in _text(pages["/sites"])
    assert t("sites.register_heading") in _text(pages["/sites/new"])
    assert t("setup.guest_heading") in _text(pages["/setup"])
    assert t("pair.heading") in _text(pages["/pair"])
    # the viewer: its own strings and the live.js table, in the same language
    await operator.open_view(run_id)
    viewer = operator.html
    assert f'<html lang="{language}">' in viewer and t("viewer.done") in viewer
    table = json.loads(htmllib.unescape(re.search(r'data-strings="([^"]*)"', viewer).group(1)))
    assert table["js.reconnecting"] == t("js.reconnecting")
    assert table["js.copy"].count("{text}") == 1
    # Done → the closing page (one tap, slice 5g)
    status, closing, _ = await operator.post(f"/view/{run_id}/done")
    assert status == 200
    assert t("finish.done") in _text(closing)
    if language != BASE:      # the English copy is gone, not merely joined by a translation
        assert catalog(BASE)["finish.done"] not in _text(closing)


@pytest.mark.parametrize("login_store", ["builtin"])
async def test_builtin_pages_in_every_language_name_no_1password_extension_or_guest(
        localized, login_store):
    """With the app's own store, Setup (with the store opened, and with it unavailable), Sites
    (with its extra-fields banner), a site's page and the new-site form say nothing of
    1Password, its extension or a guest, in any language."""
    rig, operator, language = localized
    assert rig.ctl.c.login_store == "builtin"
    t = Text(language)
    words = ("1password", "extension", "extensie", "erweiterung", "estensione", "guest", "gast",
             "ospite")
    rows = {True: "setup.store_builtin", False: "setup.store_builtin_unavailable"}
    pages = []
    for available in (True, False):
        rig.broker.status_reply = {"configured": True, "store": "builtin", "available": available}
        status, page, _ = await operator.get("/setup")
        assert status == 200, (available, page[:300])
        assert t.html(rows[available]) in page and t.html(rows[not available]) not in page, (
            language, available)
        pages.append((f"/setup available={available}", page))
    for path in ("/sites", "/sites?note=registered&fields=recovery%20codes",
                 "/sites/sa", "/sites/new"):
        status, page, _ = await operator.get(path)
        assert status == 200, (path, page[:300])
        pages.append((path, page))
    for path, page in pages:
        assert f'<html lang="{language}">' in page, path
        # a form's nonce is random letters, not text of the page
        low = re.sub(r'name="nonce" value="[^"]*"', "", page).lower()
        for word in words:
            assert word not in low, (language, path, word)


@pytest.mark.parametrize("login_store", ["builtin"])
async def test_builtin_sites_speak_of_a_saved_login_in_every_language(localized, login_store):
    """With the app's own store a site with a login says it has a saved login, on the Sites
    list and on its own page, and that page and Setup lead to the saved logins, in every
    language."""
    rig, operator, language = localized
    t = Text(language)
    rig.broker.status_reply = {"configured": True, "store": "builtin", "available": True,
                               "links": {"sa": "item-a"}}
    for path in ("/sites", "/sites/sa"):
        status, page, _ = await operator.get(path)
        assert status == 200, path
        assert t("sites.item_set.builtin") in _text(page), (language, path)
        assert t("sites.item_set") not in _text(page), (language, path)
        if language != BASE:
            assert catalog(BASE)["sites.item_set.builtin"] not in _text(page), (language, path)
    assert t("sites.saved_logins") in _text(page) and "/logins/" in page
    status, page, _ = await operator.get("/setup")
    assert status == 200 and "/logins/" in page
    if language != BASE:
        assert catalog(BASE)["sites.saved_logins"] != t("sites.saved_logins")


async def test_a_page_value_is_escaped_in_every_language(localized):
    rig, operator, _ = localized
    from dataclasses import replace
    site = rig.store.get_site("sa")
    rig.store.put_site(replace(site, display_name='<img src=x onerror=1>', revision=site.revision + 1))
    for path in ("/sites",):
        _, page, _ = await operator.get(path)
        assert "<img src=x" not in page and "&lt;img src=x" in page, path


async def test_restored_binding_needs_a_live_login(localized):
    """A provider binding learned under the proxy's current key shows "bound"; after an HA
    restore the proxy makes a new key (``proxy/*`` is not backed up) and the same site shows
    that its provider sign-in needs a live login; forgetting the site clears it."""
    from dataclasses import replace

    from casa_browser.protocol import ProviderBinding
    rig, operator, language = localized
    t = Text(language)
    site = rig.store.get_site("sa")
    binding = ProviderBinding("https://accounts.provider.example", "a" * 64, "b" * 64, "c" * 64)
    rig.store.put_site(replace(site, provider_bindings=(binding,), revision=site.revision + 1))
    rig.store.put_binding_key("sa", rig.proxy.binding_key_id)
    _, page, _ = await operator.get("/sites/sa")       # the site's own page names its provider
    assert t("sites.bound", origins="https://accounts.provider.example") in _text(page)
    assert rig.ctl.bindings_lost() == set()
    rig.proxy.binding_key_id = "k2"           # the restored app's proxy made a new key
    assert await rig.ctl._recover()
    assert rig.ctl.bindings_lost() == {"sa"}
    _, page, _ = await operator.get("/sites/sa")       # the site's own page names its provider
    assert t("sites.binding_lost") in _text(page)
    assert t("sites.bound", origins="https://accounts.provider.example") not in _text(page)


def test_every_viewer_string_reaches_the_page():
    """casa-test F39: every T('js.…') key live.js uses is in the strings the viewer page gets,
    and every catalog has it (else the viewer falls back to English)."""
    import re
    from pathlib import Path
    from casa_browser import ui
    app = Path(ui.__file__).resolve().parent
    used = set(re.findall(r"T\('(js\.[a-z_]+)'", (app / "static/live.js").read_text()))
    assert used and used <= set(ui._JS_KEYS), sorted(used - set(ui._JS_KEYS))
    for lang in ("en", "it", "nl", "de"):
        catalog = json.loads((app / "locales" / f"{lang}.json").read_text())
        assert all(k in catalog for k in set(ui._JS_KEYS) | set(ui._VIEWER_KEYS)), lang
