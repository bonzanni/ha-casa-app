"""Casa's own callers of `op`, unchanged, against the built-in store.

Each test calls the code Casa runs (the reference resolver, the drop-off,
the bot-token store and the lifecycle around it, the vault tools and the
secret exploration) and lets it start `op` as it always does. The `op` it
finds is the built-in store's (`vault_harness.builtin_op`), so what is
checked is the whole path: the caller's command line, the answer it parses,
and what ends up in the store's file.
"""

import json

import pytest

import own_voice_lifecycle as ovl
import secrets_resolver
import tools
import vault_drop_off
from own_voice_world import (
    ALEX_BOT, OPERATOR, FakeHost, World, created, make_lifecycle, specialist,
)
from vault_harness import builtin_op, run_op  # noqa: F401

ALEX_TOKEN = f"{ALEX_BOT}:alex-token-aaaaaaaaaaaaaaaa"
ALEX_TOKEN_2 = f"{ALEX_BOT}:alex-token-rotated-cccccccc"
GITHUB_KEY = "ghp_from-the-store"


@pytest.fixture(autouse=True)
def _nothing_resolved_before():
    """The resolver remembers what it resolved for the life of the process."""
    secrets_resolver.invalidate_cache()
    yield
    secrets_resolver.invalidate_cache()


def api_credential(title, *assignments):
    done = run_op("item", "create", "--category", "API Credential", "--title", title,
                  "--vault", "Casa", "--tags", "seeded", *assignments)
    assert done.returncode == 0, done.stderr


def payload(result):
    """What a tool handed back, out of its envelope."""
    return json.loads(result["content"][0]["text"])


def password(item):
    assert [field.label for field in item.fields] == ["password"]
    return item.fields[0].value


# -- the resolver --------------------------------------------------------------

def test_resolver_reads_a_reference(builtin_op):
    api_credential("GitHub", f"credential[password]={GITHUB_KEY}")
    assert secrets_resolver.resolve("op://Casa/GitHub/credential") == GITHUB_KEY
    with pytest.raises(RuntimeError, match="isn't an item"):
        secrets_resolver.resolve("op://Casa/Nothing/credential")


# -- the drop-off --------------------------------------------------------------

def test_drop_off_store_then_replace_leaves_one_item(builtin_op):
    first = "https://signin.example.test/magic?code=one"
    second = "https://signin.example.test/magic?code=abc==&x=[y]\\z"
    assert vault_drop_off.store("bank-feed", "signin_link", first) == {"status": "ok"}
    (item,) = builtin_op.items()
    assert password(item) == first

    assert vault_drop_off.store("bank-feed", "signin_link", second) == {"status": "ok"}
    (item,) = builtin_op.items()
    assert item.title == vault_drop_off.item_title("bank-feed", "signin_link")
    assert item.tags == (vault_drop_off.TAG,)
    assert password(item) == second


# -- the bot tokens ------------------------------------------------------------

def test_token_store_save_load_edit_delete(builtin_op, tmp_path):
    tokens = ovl.TokenStore(tmp_path / "tokens")

    item_id = tokens.store("finance", ALEX_TOKEN, None)
    (item,) = builtin_op.items()
    assert (item.id, item.title, item.tags) == (item_id, "Telegram Finance", (ovl.TOKEN_TAG,))
    assert tokens.load("finance", item_id) == ALEX_TOKEN
    assert tokens.load("finance", None) == ALEX_TOKEN       # found by title and tag

    assert tokens.store("finance", ALEX_TOKEN_2, item_id) == item_id
    assert tokens.store("finance", ALEX_TOKEN_2, None) == item_id   # found, then edited
    (item,) = builtin_op.items()
    assert (item.id, password(item)) == (item_id, ALEX_TOKEN_2)
    assert tokens.load("finance", item_id) == ALEX_TOKEN_2

    tokens.delete("finance", item_id)
    assert tokens.load("finance", item_id) is None
    assert tokens.load("finance", None) is None
    assert builtin_op.items() == []
    tokens.delete("finance", item_id)                        # already gone counts as deleted
    assert not (tmp_path / "tokens").exists()                # nothing went to a file


def test_token_store_load_of_an_unknown_role_is_none(builtin_op, tmp_path):
    tokens = ovl.TokenStore(tmp_path / "tokens")
    tokens.store("finance", ALEX_TOKEN, None)
    assert tokens.load("browser", None) is None
    assert tokens.load("browser", "aaaaaaaaaaaaaaaaaaaaaaaaaa") is None


async def test_a_token_only_in_the_old_file_is_fetched_again_and_lands_in_the_store(
        builtin_op, tmp_path):
    world = World()
    world.configs = {"finance": specialist("Alex")}
    world.queues = {"finance"}
    world.tokens = {ALEX_BOT: ALEX_TOKEN}
    root = tmp_path / "data"

    # Before: an install without 1Password, which kept the bot's token in a file.
    before, _host = make_lifecycle(world, root, onepassword=False)
    await before.post_offer("finance", "HeyAlexTestBot")
    await before.on_created(created(ALEX_BOT, "Alex", "HeyAlexTestBot"), OPERATOR)
    assert before.store.bots[ALEX_BOT]["phase"] == ovl.BOUND
    await before.close()
    old_file = root / "agent-voices" / "tokens" / "finance"
    assert old_file.read_text() == ALEX_TOKEN
    assert builtin_op.items() == [] and builtin_op.calls() == []

    def boot():
        """A new process over the same disk, with Casa's own token store."""
        host = FakeHost(world)
        lifecycle = ovl.Lifecycle(root=root, host=host, retry_delays=None)
        host.lifecycle = lifecycle
        return lifecycle, host

    # After: the same install starts with the built-in store. The token is
    # not in it, so Telegram is asked for it once and the answer is stored.
    asked = len(world.fetches)
    after, host = boot()
    await after.boot()
    await after.close()
    assert world.fetches[asked:] == [ALEX_BOT]
    (item,) = builtin_op.items()
    assert (item.title, item.tags) == ("Telegram Finance", (ovl.TOKEN_TAG,))
    assert password(item) == ALEX_TOKEN
    assert after.store.agents["finance"]["token_item"] == item.id
    assert host.endpoints[ALEX_BOT].bot_token == ALEX_TOKEN
    assert old_file.read_text() == ALEX_TOKEN

    # From then on it is read from the store.
    asked = len(world.fetches)
    later, host = boot()
    await later.boot()
    await later.close()
    assert world.fetches[asked:] == []
    assert host.endpoints[ALEX_BOT].bot_token == ALEX_TOKEN
    assert [stored.id for stored in builtin_op.items()] == [item.id]


# -- the vault tools and the exploration ---------------------------------------

async def test_vault_tools_list_and_get(builtin_op):
    api_credential("GitHub", f"credential[password]={GITHUB_KEY}", "username[text]=octocat")
    api_credential("Something else", "credential[password]=other")
    (github,) = [item for item in builtin_op.items() if item.title == "GitHub"]

    listed = payload(await tools.list_vault_items.handler({"query": "git"}))
    assert listed == {"items": [
        {"matched_query": "git", "id": github.id, "category": "API_CREDENTIAL"}]}

    got = payload(await tools.get_item_fields.handler({"item": github.id}))
    assert got == {"fields": [
        {"id": github.fields[0].id, "role": "credential", "type": "CONCEALED"},
        {"id": "username", "role": "username", "type": "STRING"}]}
    assert payload(await tools.get_item_fields.handler({"item": "GitHub"})) == got

    # What the configurator wires from those two answers resolves.
    reference = f"op://Casa/{listed['items'][0]['id']}/{got['fields'][0]['id']}"
    assert secrets_resolver.resolve(reference) == GITHUB_KEY


def test_secret_exploration_lists_candidates(builtin_op, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    api_credential("GitHub", f"credential[password]={GITHUB_KEY}")
    api_credential("Something else", "credential[password]=other")
    (github,) = [item for item in builtin_op.items() if item.title == "GitHub"]

    assert tools._secret_candidates("github", ["GITHUB_TOKEN"]) == {"secret_candidates": {
        "vault": "Casa",
        "queries": ["github"],
        "items": [{"matched_query": "github", "id": github.id, "fields": [
            {"id": github.fields[0].id, "role": "credential", "type": "CONCEALED"}]}],
        "unresolved": ["GITHUB_TOKEN"],
    }}


# -- which callers ask for a token first ---------------------------------------

async def test_what_each_caller_does_without_the_marker(builtin_op, tmp_path, monkeypatch):
    monkeypatch.delenv("OP_SERVICE_ACCOUNT_TOKEN")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    # These three look for the token before anything else and run no `op`.
    assert vault_drop_off.store("bank-feed", "signin_link", "https://signin.example.test/x") == {
        "error": "no_token"}
    assert tools._secret_candidates("github", ["GITHUB_TOKEN"]) == {}
    tokens = ovl.TokenStore(tmp_path / "tokens")
    assert tokens.store("finance", ALEX_TOKEN, None) is None
    assert (tmp_path / "tokens" / "finance").read_text() == ALEX_TOKEN
    assert tokens.load("finance", None) == ALEX_TOKEN
    assert builtin_op.calls() == [] and builtin_op.items() == []

    # The resolver and the two vault tools do not look, and run `op` all the same.
    api_credential("GitHub", f"credential[password]={GITHUB_KEY}")
    (github,) = builtin_op.items()
    seeded = len(builtin_op.calls())
    assert secrets_resolver.resolve("op://Casa/GitHub/credential") == GITHUB_KEY
    assert builtin_op.calls()[seeded:] == ["read op://Casa/GitHub/credential"]
    listed = payload(await tools.list_vault_items.handler({"query": "git"}))
    assert [row["id"] for row in listed["items"]] == [github.id]
    assert builtin_op.calls()[seeded + 1:] == ["item list"]
    got = payload(await tools.get_item_fields.handler({"item": github.id}))
    assert [field["role"] for field in got["fields"]] == ["credential"]
    assert builtin_op.calls()[seeded + 2:] == ["item get"]
