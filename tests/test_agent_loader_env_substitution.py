"""``${VAR}`` is resolved as each scalar is CONSTRUCTED, not before the parse
(#409).

The loader used to substitute into a file's TEXT and parse the result, which
handed the parser whatever the variable contained. The substituted value was
re-lexed as part of the document, so its punctuation decided what the document
meant: a `#` truncated the scalar at a comment, a quote character ended it early
and usually broke the parse outright, a newline split it. For a resident, a file
that fails to parse stops boot — so the value of an environment variable could
stop Casa starting.

Resolving inside the constructor is what makes the document's shape independent
of the environment while keeping the one thing a post-parse walk would have
thrown away: the scalar's own STYLE. Quoting is how YAML says "this is text", so
a quoted placeholder is a string always, and a plain one is read back as a lone
value — which is what the old pipeline did, and what keeps a list-valued
variable a list rather than a string some consumer then iterates CHARACTER by
character.

Each hostile value gets its own case: they fail differently (silent truncation
vs. a hard parse error), and a combined test would let a regression in one hide
behind another.
"""
from __future__ import annotations

import textwrap

import pytest
import yaml

import agent_loader
import config


# --------------------------------------------------------------------------
# The values that used to change or break the document
# --------------------------------------------------------------------------

@pytest.mark.parametrize("label,value", [
    ("comment marker", "bins # tonight"),
    ("single quote", "it's tonight"),
    ("double quote", 'she said "tonight"'),
    ("newline", "line one\nline two"),
    ("mapping-looking", "key: value"),
    ("list-looking", "- not a list item"),
    ("leading indicator", "*emphasis*"),
    ("braces", "{not: a mapping}"),
    ("trailing colon", "tonight:"),
])
@pytest.mark.parametrize("quoting", ["plain", "single", "double"])
def test_a_placeholder_value_survives_whatever_punctuation_it_contains(
        label, value, quoting, monkeypatch):
    """The value arrives intact, and the document still parses — for every
    quoting style the file could have been authored (or re-emitted) in."""
    monkeypatch.setenv("DETAIL", value)
    scalar = {
        "plain": "Send ${DETAIL}",
        "single": "'Send ${DETAIL}'",
        "double": '"Send ${DETAIL}"',
    }[quoting]
    doc = agent_loader.parse_yaml_text(f"prompt: {scalar}\n", "<test>")
    assert doc == {"prompt": f"Send {value}"}, label


def test_a_hostile_value_can_no_longer_stop_the_file_loading(monkeypatch):
    """The boot-fatal half, stated on its own. A resident whose YAML fails to
    parse stops the process, so this was an environment variable's punctuation
    deciding whether Casa starts."""
    monkeypatch.setenv("DETAIL", "\"'\n#{}[]&*!|>%@`")
    doc = agent_loader.parse_yaml_text('prompt: "Send ${DETAIL}"\n', "<test>")
    assert doc["prompt"] == "Send \"'\n#{}[]&*!|>%@`"


def test_a_QUOTED_placeholder_is_text_whatever_the_value_looks_like(
        monkeypatch):
    """Quoting is how YAML says "this is text", and it is what an author
    reaches for when a value might contain punctuation. It decided the type
    before this change too (the quotes survived text substitution), so a rule
    that ignored them retyped `prompt: "${V}"` from `"true"` to `True` and
    dropped a comment suffix off the end of a value. The style is the authority.
    """
    for value in ("true", "5", "null", "[foo]", "{a: b}", "true # ignored",
                  "[foo] # ignored", "# secret", "---", "2026-08-07",
                  "!!binary L2NvbmZpZw==", "bins # tonight"):
        monkeypatch.setenv("V", value)
        for text in ('k: "${V}"\n', "k: '${V}'\n"):
            assert agent_loader.parse_yaml_text(text, "<t>") == {"k": value}, (
                f"{value!r} via {text!r}")


def test_a_PLAIN_lone_placeholder_keeps_its_value_s_type(monkeypatch):
    """`minutes: ${M}` is a number, `writable: ${DIRS}` a list — as before.

    Stringifying these was tried and reverted, because the schemas leave some
    values untyped on purpose — a hook policy's parameters, a disclosure
    override — so a stringified list does not fail validation. It reaches a
    consumer that iterates it and gets CHARACTERS, and for `path_scope.writable`
    a lone `/` prefix matches every absolute path: the guard fails OPEN. The
    type is read from one lone value, never from the document, so it still
    cannot change the document's shape.
    """
    monkeypatch.setenv("PORT", "8080")
    monkeypatch.setenv("FLAG", "true")
    monkeypatch.setenv("DIRS", "[/config/workspace, /tmp]")
    monkeypatch.setenv("OVERRIDES", "{categories: {}}")
    doc = agent_loader.parse_yaml_text(
        "port: ${PORT}\nflag: ${FLAG}\nwritable: ${DIRS}\n"
        "overrides: ${OVERRIDES}\n", "<t>")
    assert doc == {
        "port": 8080, "flag": True,
        "writable": ["/config/workspace", "/tmp"],
        "overrides": {"categories": {}},
    }
    # Including the shapes a filter would have called exotic. `path_scope`
    # accepts any iterable, so a `!!set` excluded as "nothing asks for that"
    # came back a STRING and reopened the fail-open above — which is how the
    # filter was found to be the wrong idea rather than the wrong list.
    monkeypatch.setenv("DIRS", "!!set {/tmp: null, /var: null}")
    assert agent_loader.parse_yaml_text("writable: ${DIRS}\n", "<t>") == {
        "writable": {"/tmp", "/var"}}


@pytest.mark.parametrize("label,value", [
    ("double quote", 'she said "tonight"'),
    ("single quote", "it's tonight"),
    ("unclosed quote", "'unclosed"),
    ("a date that is not one", "2026-99-99"),
    ("bare tag indicator", "%tag"),
    ("block indicator", "|block"),
    ("unclosed flow", "["),
    ("tab", "tab\there"),
])
def test_a_PLAIN_value_that_is_not_YAML_ON_ITS_OWN_is_read_as_text(
        label, value, monkeypatch):
    """The one thing the read-back does that plain YAML would not.

    A value that is not a valid YAML document on its own is text — the same
    reading YAML gives any scalar it cannot parse as anything else. It is not a
    judgment about types, and it is what keeps PyYAML's constructors from taking
    the process down: `2026-99-99` raises `ValueError` out of the timestamp
    constructor, not a `YAMLError`, and every one of these used to make the
    WHOLE FILE fail to parse.
    """
    monkeypatch.setenv("DETAIL", value)
    assert agent_loader.parse_yaml_text("prompt: ${DETAIL}\n", "<t>") == {
        "prompt": value}, label


@pytest.mark.parametrize("label,value,expected", [
    ("comment suffix", "bins # tonight", "bins"),
    ("comment only", "# secret", None),
    ("document marker", "---", None),
    ("empty", "", None),
    ("folded newline", "line one\nline two", "line one line two"),
    ("anchor", "&anchor tonight", "tonight"),
    ("stripped padding", "  padded  ", "padded"),
])
def test_a_PLAIN_lone_placeholder_means_what_its_text_means_as_YAML(
        label, value, expected, monkeypatch):
    """No filter sits on the read-back, and that is the point.

    Three review rounds each rejected a different one. "Always a string"
    stringified a list and made `path_scope` iterate CHARACTERS, so a lone `/`
    prefix matched every absolute path and the guard failed OPEN. "Anything but
    a string" lost the file's own quoting and retyped `"true"` to `True`. An
    allowlist of safe-looking types excluded `!!set` and reopened the fail-open
    the first round had closed. Each was a guess at what an operator meant, and
    each grew a new hole.

    So an unquoted scalar means what YAML says it means — substantially what it
    meant before this change. "Substantially" because the value is read on its
    own rather than in place, and `---` is where that shows: alone it is a
    document marker and reads as the empty document, while in place it would
    have been the string. Every value here loses something in the reading, and
    an author who does not want that says so by quoting: each case also pins
    that the quoted form keeps the value whole.
    """
    monkeypatch.setenv("DETAIL", value)
    assert agent_loader.parse_yaml_text("prompt: ${DETAIL}\n", "<t>") == {
        "prompt": expected}, label
    assert agent_loader.parse_yaml_text('prompt: "${DETAIL}"\n', "<t>") == {
        "prompt": value}, f"{label} (quoted)"


def test_an_EXPLICIT_TAG_says_text_at_least_as_loudly_as_quoting(monkeypatch):
    """`!!str ${V}` is a stronger declaration than quoting is, and its scalar is
    plain, so reading style alone would have retyped it."""
    monkeypatch.setenv("V", "true")
    assert agent_loader.parse_yaml_text("k: !!str ${V}\n", "<t>") == {"k": "true"}


@pytest.mark.parametrize("form", [
    'k: "${V}"\n',                              # double-quoted
    "k: '${V}'\n",                              # single-quoted
    "k: |-\n  ${V}\n",                          # literal block
    "k: >-\n  ${V}\n",                          # folded block
    "k: !!str ${V}\n",                           # the tag, shorthand
    "k: !<tag:yaml.org,2002:str> ${V}\n",        # the same tag, verbatim
    "%TAG !y! tag:yaml.org,2002:\n---\nk: !y!str ${V}\n",   # ...via a handle
    "k: !!str &x ${V}\n",                        # tag before anchor
    "k: &x !!str ${V}\n",                        # anchor before tag
    'k: !!str "${V}"\n',                         # both at once
])
def test_every_way_of_declaring_text_is_recognised_as_one(form, monkeypatch):
    """Quoting has several spellings and the tag has arbitrarily many — a
    `%TAG` directive can bind any handle to it — and either may be written
    before or after an anchor. A form missed here is missed in BOTH directions:
    the loader reads it as text while `config.text_has_lone_placeholder`, which
    decides whether a rewrite may touch the file, says nothing is at stake. That
    is why the predicate RESOLVES the tag rather than matching spellings.
    """
    import config
    monkeypatch.setenv("V", "true")
    assert agent_loader.parse_yaml_text(form, "<t>") == {"k": "true"}, form
    assert config.text_has_lone_placeholder(form), form


def test_an_explicitly_tagged_scalar_PyYAML_cannot_build_is_a_LoadError(
        monkeypatch):
    """`!!int` hands the value to PyYAML's own int constructor, which raises a
    plain `ValueError` — not a `YAMLError`. Unfolded it escapes every caller,
    and at boot that is the process."""
    monkeypatch.setenv("V", "nope")
    with pytest.raises(agent_loader.LoadError):
        agent_loader.parse_yaml_text("k: !!int ${V}\n", "<t>")


def test_the_residual_a_PLAIN_lone_placeholder_holding_a_MAPPING_becomes_one(
        monkeypatch):
    """The sharp edge the type rule leaves, stated rather than hidden.

    `tonight: yes` is a YAML mapping literal, so an UNQUOTED scalar that is
    nothing but a placeholder holding it becomes a mapping — the same rule that
    makes `writable: ${DIRS}` a list. It is not a silent loss: the field's
    schema rejects it, and the old text substitution did not preserve it either
    (it made the whole file fail to parse). Quoting the placeholder — which is
    what the documentation and the reminder writer's own files do — is text in
    every case.
    """
    monkeypatch.setenv("DETAIL", "tonight: yes")
    assert agent_loader.parse_yaml_text("prompt: ${DETAIL}\n", "<t>") == {
        "prompt": {"tonight": True}}
    assert agent_loader.parse_yaml_text('prompt: "${DETAIL}"\n', "<t>") == {
        "prompt": "tonight: yes"}


def test_an_embedded_placeholder_is_never_re_read(monkeypatch):
    """Only a scalar that is NOTHING BUT a placeholder is typed. A placeholder
    with any text around it was always a string and stays one."""
    monkeypatch.setenv("PORT", "8080")
    doc = agent_loader.parse_yaml_text("label: port ${PORT}\n", "<t>")
    assert doc == {"label": "port 8080"}


def test_a_resolved_value_is_not_substituted_AGAIN(monkeypatch):
    """One pass, matching the single text-level substitution it replaces: a
    variable whose value happens to contain `${X}` resolves no further, so no
    variable can name another and no cycle exists to fall into."""
    monkeypatch.setenv("OUTER", "${INNER}")
    monkeypatch.setenv("INNER", "resolved")
    assert agent_loader.parse_yaml_text("a: ${OUTER}\nb: x ${OUTER}\n",
                                        "<t>") == {
        "a": "${INNER}", "b": "x ${INNER}"}


# --------------------------------------------------------------------------
# What must NOT change
# --------------------------------------------------------------------------

def test_a_typed_schema_field_still_validates_through_a_placeholder(
        monkeypatch):
    """`minutes` is `type: integer`, and a document using a placeholder for it
    still passes its schema — against the REAL schema, not a fixture, because
    the whole risk of the type rule is that it disagrees with validation."""
    import agent_loader as al
    monkeypatch.setenv("MINUTES", "60")
    doc = al.parse_yaml_text(
        "schema_version: 1\ntriggers:\n  - name: hb\n    type: interval\n"
        "    minutes: ${MINUTES}\n    channel: telegram\n    prompt: hi\n",
        "<t>")
    assert doc["triggers"][0]["minutes"] == 60
    al._validate(doc, "triggers", "<t>")     # raises LoadError if invalid


def test_an_unset_variable_keeps_its_placeholder(monkeypatch):
    """`resolve_model` treats an unresolved `${VAR}` as a DEFERRED value, which
    is what keeps `validate_config_repo` environment-independent. Post-parse
    substitution has to preserve that, so the placeholder must survive an
    unset variable rather than becoming empty."""
    monkeypatch.delenv("NOT_SET_ANYWHERE", raising=False)
    doc = agent_loader.parse_yaml_text("model: ${NOT_SET_ANYWHERE}\n", "<t>")
    assert doc == {"model": "${NOT_SET_ANYWHERE}"}
    assert config.resolve_model(doc["model"]) == "${NOT_SET_ANYWHERE}"


def test_substitution_reaches_every_nesting_depth(monkeypatch):
    monkeypatch.setenv("V", "resolved # value")
    # Quoted throughout: this is about REACH, and an unquoted placeholder would
    # mix in the separate question of what its value means.
    doc = agent_loader.parse_yaml_text(textwrap.dedent("""\
        top: "${V}"
        list:
          - "${V}"
          - nested:
              deeper: ["${V}"]
        mapping:
          a:
            b: "${V}"
        """), "<t>")
    assert doc["top"] == "resolved # value"
    assert doc["list"][0] == "resolved # value"
    assert doc["list"][1]["nested"]["deeper"] == ["resolved # value"]
    assert doc["mapping"]["a"]["b"] == "resolved # value"


def test_the_file_must_now_be_valid_yaml_BEFORE_substitution():
    """The one narrowing, stated rather than discovered.

    Substituting first meant the text only had to become valid YAML *once the
    variable was in place*, so a placeholder could sit in a position that is not
    a scalar at all — inside a flow collection, where `{` is an indicator. Now
    the document's structure is fixed before any variable is read, which is the
    property that makes a file's meaning environment-independent. The cost is
    that such a file is refused, loudly, instead of depending on the
    environment. Nothing shipped uses a placeholder at all, and the quoted form
    (`["${V}"]`) works.
    """
    with pytest.raises(agent_loader.LoadError, match="YAML parse error"):
        agent_loader.parse_yaml_text("deeper: [${V}]\n", "<t>")


def test_a_parse_error_is_still_a_LoadError_naming_the_source():
    with pytest.raises(agent_loader.LoadError, match="YAML parse error"):
        agent_loader.parse_yaml_text("triggers: [unclosed\n", "/config/x.yaml")


def test_an_empty_document_reads_as_an_empty_mapping():
    assert agent_loader.parse_yaml_text("", "<t>") == {}
    assert agent_loader.parse_yaml_text("# only a comment\n", "<t>") == {}


def test_read_yaml_runs_the_same_pipeline(tmp_path, monkeypatch):
    """The file reader is the pipeline plus a read; nothing substitutes early."""
    monkeypatch.setenv("DETAIL", "bins # tonight")
    path = tmp_path / "triggers.yaml"
    path.write_text('schema_version: 1\ntriggers:\n  - name: n\n    prompt: "${DETAIL}"\n',
                    encoding="utf-8")
    doc = agent_loader._read_yaml(str(path))
    assert doc["triggers"][0]["prompt"] == "bins # tonight"


# --------------------------------------------------------------------------
# Resolution at construction time
# --------------------------------------------------------------------------

def test_mapping_KEYS_are_substituted_too(monkeypatch):
    """Resolving inside the constructor reaches a key as readily as a value, so
    the one behaviour the text substitution had that a post-parse walk would
    have quietly dropped is kept."""
    monkeypatch.setenv("K", "resolved")
    assert agent_loader.parse_yaml_text("${K}: value\n", "<t>") == {
        "resolved": "value"}


def test_a_key_whose_value_is_not_hashable_is_a_LOAD_ERROR(monkeypatch):
    """A plain key resolving to a list is refused by YAML itself, loudly — the
    same refusal the pre-fix pipeline produced for the same document, and a
    `yaml.YAMLError`, so it lands inside the LoadError contract rather than
    escaping as something no caller catches."""
    monkeypatch.setenv("K", "[a, b]")
    with pytest.raises(agent_loader.LoadError):
        agent_loader.parse_yaml_text("${K}: value\n", "<t>")


def test_a_document_that_aliases_an_ancestor_still_loads(monkeypatch):
    """A self-referential document is expressible in plain YAML, and these
    files are parsed permissively. Resolving inside the constructor means each
    scalar is resolved as it is built, so there is no second traversal of the
    finished document to recurse forever — the resolution step must not become
    a new way to kill boot."""
    monkeypatch.setenv("V", "resolved")
    doc = agent_loader.parse_yaml_text("root: &a\n  self: *a\n  value: ${V}\n",
                                       "<t>")
    assert doc["root"]["value"] == "resolved"
    assert doc["root"]["self"] is doc["root"]


def test_deep_nesting_does_not_lower_the_parser_s_own_ceiling(monkeypatch):
    """`RecursionError` is not in the LoadError contract, so anything the
    resolution step added on top of the parse would escape `_read_yaml`'s
    callers as an unhandled crash at boot.

    The depth is one PyYAML itself accepts: the parser is recursive and gives
    out first, so anything deeper tests the parser rather than this change.
    """
    monkeypatch.setenv("V", "resolved")
    depth = 100
    doc = agent_loader.parse_yaml_text(
        "root: " + "[" * depth + "'${V}'" + "]" * depth, "<t>")
    node = doc["root"]
    for _ in range(depth - 1):
        node = node[0]
    assert node == ["resolved"]


def test_a_shared_subtree_is_resolved_once_and_stays_shared(monkeypatch):
    """Two entries sharing one anchored block stay one object — re-emitting the
    document must not silently duplicate it, and the shared scalar must not be
    resolved a second time.

    The value is chosen so a second resolution is DETECTABLE: `${MODE}` resolves
    to the literal text `${NEXT}`, so resolving twice would resolve that too and
    yield `hmac`. A value that resolved to something inert would pass whether
    the node was visited once or twice.
    """
    monkeypatch.setenv("MODE", "${NEXT}")
    monkeypatch.setenv("NEXT", "hmac")
    doc = agent_loader.parse_yaml_text(textwrap.dedent("""\
        triggers:
          - name: a
            auth: &shared
              mode: ${MODE}
          - name: b
            auth: *shared
        """), "<t>")
    a, b = doc["triggers"][0]["auth"], doc["triggers"][1]["auth"]
    assert a is b
    assert a["mode"] == "${NEXT}"


def test_the_rewrite_guard_reads_TEXT_not_the_constructed_document():
    """Its conservatism, pinned so it stays a decision rather than a surprise.

    `config.text_has_lone_placeholder` scans tokens, so a declared-text
    placeholder that construction then DISCARDS — the losing side of a merge
    key, an overridden duplicate key — is still reported, and costs that file
    its entry-level reconciliation even though no rewrite could change it.
    Chasing it would mean tracking which scalars survive construction: a second
    model of YAML's semantics living beside PyYAML's. The conservatism is the
    cheaper error, and it is bounded to configuration that is already dead.
    """
    discarded = ('k:\n  <<: {prompt: "${DETAIL}"}\n  prompt: put the bins out\n')
    assert yaml.safe_load(discarded)["k"]["prompt"] == "put the bins out"
    assert config.text_has_lone_placeholder(discarded)


# --- carrying the declared-text form through a rewrite (#512) --------------
#
# The guard above tells a writer that re-emitting a file would change what one
# of its scalars means. These pin the other half, for the one writer that
# cannot refuse: the declaration itself survives the dump, so the rewrite
# neither retypes the scalar nor erases the property the guard tests.


@pytest.mark.parametrize("form", [
    'k: "${V}"\n',                              # double-quoted
    "k: '${V}'\n",                              # single-quoted
    "k: |-\n  ${V}\n",                          # literal block
    "k: >-\n  ${V}\n",                          # folded block
    "k: !!str ${V}\n",                           # the tag, shorthand
    "k: !<tag:yaml.org,2002:str> ${V}\n",        # the same tag, verbatim
    "%TAG !y! tag:yaml.org,2002:\n---\nk: !y!str ${V}\n",   # ...via a handle
    "k: !!str &x ${V}\n",                        # tag before anchor
    "k: &x !!str ${V}\n",                        # anchor before tag
    'k: !!str "${V}"\n',                         # both at once
])
def test_a_rewrite_keeps_every_declared_text_form_declared(form, monkeypatch):
    """Each spelling the guard recognises must ALSO survive the dump.

    A form the loader marks but the dumper drops is the #512 defect restored
    for that spelling — and silently, since the value re-parses as a boolean
    only once the variable happens to hold one. Asserted through the live
    loader, on the emitted text, with the value that makes the difference
    visible: `"true"` the string against `True` the boolean.
    """
    monkeypatch.setenv("V", "true")
    out = config.dump_yaml_declared_text(config.load_yaml_declared_text(form))
    assert agent_loader.parse_yaml_text(out, "<t>") == {"k": "true"}, out
    assert config.text_has_lone_placeholder(out), out


def test_a_rewrite_leaves_a_PLAIN_lone_placeholder_PLAIN(monkeypatch):
    """The inverse, and the reason the marker exists rather than a rule keyed
    on the VALUE: after loading, `"${V}"` and `${V}` are the same `str`, so
    quoting both would turn a plain placeholder's value-read into text — the
    same retyping in the other direction, on the operator's own file.
    """
    monkeypatch.setenv("V", "7")
    out = config.dump_yaml_declared_text(
        config.load_yaml_declared_text("k: ${V}\n"))
    assert out == "k: ${V}\n"
    assert agent_loader.parse_yaml_text(out, "<t>") == {"k": 7}


def test_one_file_can_hold_both_forms_and_keep_both(monkeypatch):
    monkeypatch.setenv("V", "true")
    text = 'declared: "${V}"\nplain: ${V}\nembedded: say ${V}\n'
    out = config.dump_yaml_declared_text(config.load_yaml_declared_text(text))
    assert agent_loader.parse_yaml_text(out, "<t>") == {
        "declared": "true", "plain": True, "embedded": "say true"}


def test_a_declared_text_KEY_survives_too(monkeypatch):
    """The guard scans every scalar token, keys included, so a rewrite that
    kept only values would still disarm it."""
    monkeypatch.setenv("V", "true")
    out = config.dump_yaml_declared_text(
        config.load_yaml_declared_text('"${V}": 1\n'))
    assert config.text_has_lone_placeholder(out), out
    assert agent_loader.parse_yaml_text(out, "<t>") == {"true": 1}


def test_a_document_with_nothing_to_preserve_dumps_as_safe_dump_would():
    """The pair is not a second emitter: a file with no declared-text
    placeholder must come out byte-identical to today's output, or every
    rewrite of every ordinary triggers.yaml is a diff in the operator's repo.
    """
    doc = {"schema_version": 2, "triggers": [
        {"name": "op", "prompt": "put the bins out", "minutes": 60}]}
    assert (config.dump_yaml_declared_text(doc, sort_keys=False)
            == yaml.safe_dump(doc, sort_keys=False))


def test_the_marker_reaching_a_PLAIN_dump_is_loud_rather_than_silent():
    """PyYAML keys representers on the EXACT type, so a marker that escapes to
    `yaml.safe_dump` raises instead of quietly losing its quoting. Pinned
    because that is the failure mode the design accepts in exchange for not
    mutating the global `SafeDumper`: loud, at the one call site that would
    have to be found by hand otherwise.
    """
    doc = config.load_yaml_declared_text('k: "${V}"\n')
    with pytest.raises(yaml.representer.RepresenterError):
        yaml.safe_dump(doc)


def test_the_marker_is_a_str_everywhere_it_is_not_being_dumped():
    """Consumers must not have to know about it: equality, hashing, membership
    and `str`-ness are the whole contract outside the dumper."""
    value = config.load_yaml_declared_text('k: "${V}"\n')["k"]
    assert isinstance(value, str)
    assert value == "${V}" and hash(value) == hash("${V}")
    assert {value: 1} == {"${V}": 1}
