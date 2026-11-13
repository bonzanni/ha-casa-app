"""The declarative ingress identity table (#203 + #204).

#204: `/invoke` and `/webhook/{name}` stamp a trusted per-peer identity instead
of falling back to the unattributed ``system``.

#203: a channel that is authenticated but forgets to stamp must fail LOUDLY
rather than degrade silently. The enforcement is structural — every external
ingress resolves its identity through :func:`ingress_identity`, which raises
instead of returning nothing — plus a boot-time check that the table covers
every route the handlers can ask for.
"""

from dataclasses import replace as dataclasses_replace

import pytest


class TestPeerSeparation:
    def test_a_telegram_sender_and_an_automation_never_share_a_peer(self):
        # The namespaces are disjoint by construction, so no sender id can
        # spell an automation peer and no trigger name can spell a person's.
        from ingress_identity import ingress_identity

        sender = ingress_identity("telegram", sender_id="42").user_peer
        automations = {
            ingress_identity("invoke").user_peer,
            ingress_identity(
                "webhook_trigger", webhook_name="nightly").user_peer,
            ingress_identity("voice_sse").user_peer,
        }
        assert sender not in automations

    def test_invoke_and_webhook_do_not_share_a_peer(self):
        # The core decision: both are HMAC-gated, but a shared bearer secret is
        # not evidence of a common author.
        from ingress_identity import ingress_identity

        invoke = ingress_identity("invoke")
        webhook = ingress_identity("webhook_trigger", webhook_name="nightly")
        assert invoke.user_peer != webhook.user_peer

    def test_invoke_is_never_attributed_to_a_person(self):
        # Trap 2: user_peer_for_channel used to default to the operator's
        # constant peer, which would have recorded every signed call as
        # authored by the operator.
        from ingress_identity import ingress_identity

        assert ingress_identity("invoke").user_peer == "invoke_caller"

    def test_webhook_is_never_attributed_to_a_person(self):
        from ingress_identity import _TELEGRAM_PEER_PREFIX, ingress_identity

        peer = ingress_identity("webhook_trigger", webhook_name="nightly").user_peer
        assert peer == "webhook:nightly"
        assert not peer.startswith(_TELEGRAM_PEER_PREFIX)

    def test_each_trigger_gets_its_own_peer(self):
        from ingress_identity import ingress_identity

        kitchen = ingress_identity("webhook_trigger", webhook_name="kitchen")
        garage = ingress_identity("webhook_trigger", webhook_name="garage")
        assert kitchen.user_peer != garage.user_peer

    def test_a_plugin_trigger_keeps_its_qualified_name(self):
        from ingress_identity import ingress_identity

        origin = ingress_identity(
            "webhook_trigger", webhook_name="plg-elevenlabs--call_ended")
        assert origin.user_peer == "webhook:plg-elevenlabs--call_ended"


class TestStampedOrigins:
    def test_invoke_stamps_the_invoke_surface(self):
        from ingress_identity import ingress_identity

        origin = ingress_identity("invoke")
        assert origin.surface == "invoke"
        assert origin.server_origin.route == "invoke"
        assert origin.server_origin.is_authenticated is True
        assert origin.authenticated_user is None

    def test_webhook_stamps_the_webhook_surface(self):
        from ingress_identity import ingress_identity

        origin = ingress_identity("webhook_trigger", webhook_name="nightly")
        assert origin.surface == "webhook"
        assert origin.server_origin.route == "webhook"

    def test_a_webhook_carries_its_declared_clearance(self):
        from ingress_identity import ingress_identity

        origin = ingress_identity(
            "webhook_trigger", webhook_name="nightly", clearance="friends")
        assert origin.server_origin.clearance == "friends"

    def test_a_webhook_without_a_declared_clearance_floors_at_public(self):
        from ingress_identity import ingress_identity

        origin = ingress_identity("webhook_trigger", webhook_name="nightly")
        assert origin.server_origin.clearance == "public"

    def test_both_automation_routes_produce_automation_provenance(self):
        from ingress_identity import ingress_identity
        from speaker_provenance import UserProvenance

        for origin in (
            ingress_identity("invoke"),
            ingress_identity("webhook_trigger", webhook_name="nightly"),
        ):
            value = UserProvenance.from_origin(
                surface=origin.surface, server_origin=origin.server_origin,
                authenticated_user=origin.authenticated_user,
                user_peer=origin.user_peer,
            )
            assert value.speaker_kind == "automation"


class TestTelegramSenderIdentity:
    """#336: identity and private clearance are granted per-SENDER, never as a
    route-wide constant — with telegram_chat_id empty ("accept all"), any
    Telegram user can reach this route.

    The operator-peer collapse went one step further: there is no operator peer
    literal at all. Every accepted sender is named by its own id and CLEARANCE
    is the only difference, so these tests pin a derived peer — a regression to
    any shared constant fails them.
    """

    def test_operator_sender_is_named_by_its_own_id_at_private_clearance(self):
        from ingress_identity import ingress_identity

        origin = ingress_identity(
            "telegram", sender_id="42", sender_display_name="Operator",
            sender_is_operator=True)
        assert origin.user_peer == "telegram:42"
        assert origin.surface == "telegram"
        assert origin.server_origin.clearance == "private"
        assert origin.authenticated_user.stable_id == "42"

    def test_the_operator_is_not_named_by_a_shared_constant(self):
        # The red case for the collapse: two senders, one of them the
        # operator, must not share a peer — AND the operator must be named
        # inside the human namespace by its own id. Inequality alone is too
        # weak: an operator branch emitting `operator:42` would satisfy it
        # while reintroducing a name that is not the sender's own
        # (Terra review, INV-HTTP-006 binding).
        from ingress_identity import _TELEGRAM_PEER_PREFIX, ingress_identity

        operator = ingress_identity(
            "telegram", sender_id="42", sender_is_operator=True)
        other = ingress_identity("telegram", sender_id="99")
        assert operator.user_peer != other.user_peer
        assert operator.user_peer == _TELEGRAM_PEER_PREFIX + "42"
        assert other.user_peer == _TELEGRAM_PEER_PREFIX + "99"

    def test_non_operator_sender_gets_its_own_peer(self):
        # The pre-#336 behavior: sender 42 resolved to the operator's constant
        # peer with private clearance regardless of who sent the message.
        from ingress_identity import ingress_identity

        origin = ingress_identity(
            "telegram", sender_id="42", sender_display_name="Mallory")
        assert origin.user_peer == "telegram:42"
        assert origin.authenticated_user.stable_id == "42"

    def test_non_operator_sender_reads_at_public_clearance(self):
        from ingress_identity import ingress_identity

        origin = ingress_identity("telegram", sender_id="42")
        assert origin.server_origin.clearance == "public"

    def test_non_operator_clearance_cannot_be_escalated_by_the_caller(self):
        # The clearance override parameter exists for webhook triggers; a
        # telegram non-operator turn must not be liftable through it.
        from ingress_identity import ingress_identity

        origin = ingress_identity(
            "telegram", sender_id="42", clearance="private")
        assert origin.server_origin.clearance == "public"

    def test_two_senders_resolve_to_distinct_peers(self):
        from ingress_identity import ingress_identity

        a = ingress_identity("telegram", sender_id="42")
        b = ingress_identity("telegram", sender_id="43")
        assert a.user_peer != b.user_peer

    def test_a_telegram_turn_without_a_sender_fails_loudly(self):
        # #203 doctrine: an ingress that cannot resolve an author raises —
        # it must never silently borrow the operator's identity (the
        # pre-#336 behavior for anonymous group/channel posts).
        from ingress_identity import IngressIdentityError, ingress_identity

        with pytest.raises(IngressIdentityError):
            ingress_identity("telegram")

    def test_operator_flag_without_a_sender_still_fails(self):
        from ingress_identity import IngressIdentityError, ingress_identity

        with pytest.raises(IngressIdentityError):
            ingress_identity("telegram", sender_is_operator=True)


class TestGuestRoute:
    """A friend summoning a specialist's bot in a group the operator allowed
    arrives on its own route: a Telegram person, named by their own sender
    id, reading at public clearance whatever the handler says about them."""

    def test_the_guest_route_names_a_public_person(self):
        from ingress_identity import _INGRESS_IDENTITY, ingress_identity
        from speaker_provenance import UserProvenance

        origin = ingress_identity(
            "telegram_guest", sender_id="777", sender_display_name="Mara")
        assert origin.surface == "telegram"
        assert origin.server_origin.is_authenticated is True
        assert origin.server_origin.clearance == "public"
        assert origin.user_peer == "telegram:777"
        assert origin.authenticated_user.stable_id == "777"
        assert origin.authenticated_user.configured_display_name == "Mara"
        assert UserProvenance.from_origin(
            surface=origin.surface, server_origin=origin.server_origin,
            authenticated_user=origin.authenticated_user,
            user_peer=origin.user_peer,
        ).speaker_kind == "user"
        # The table's own clearance is public, so even an operator flag
        # yields public here; the handler passes False regardless.
        assert _INGRESS_IDENTITY["telegram_guest"].clearance == "public"
        assert ingress_identity(
            "telegram_guest", sender_id="100", sender_is_operator=True,
        ).server_origin.clearance == "public"

    def test_a_guest_turn_without_a_sender_fails_loudly(self):
        from ingress_identity import IngressIdentityError, ingress_identity

        with pytest.raises(IngressIdentityError, match="sender"):
            ingress_identity("telegram_guest")


class TestExistingRoutesUnchanged:

    def test_voice_sse_is_the_anonymous_trusted_speaker(self):
        from ingress_identity import ingress_identity

        origin = ingress_identity("voice_sse")
        assert origin.surface == "voice"
        assert origin.user_peer == "voice_speaker"
        assert origin.authenticated_user is None
        assert origin.server_origin.clearance == "friends"

    def test_voice_ws_matches_voice_sse(self):
        from ingress_identity import ingress_identity

        assert ingress_identity("voice_ws") == ingress_identity("voice_sse")


class TestFailsLoudly:
    def test_an_unknown_route_raises(self):
        # A future authenticated ingress that forgets a table entry cannot
        # silently degrade to the unattributed ``system`` identity (#203).
        from ingress_identity import IngressIdentityError, ingress_identity

        with pytest.raises(IngressIdentityError, match="unknown ingress route"):
            ingress_identity("matrix")

    def test_a_webhook_without_a_name_raises(self):
        from ingress_identity import IngressIdentityError, ingress_identity

        with pytest.raises(IngressIdentityError, match="webhook_name"):
            ingress_identity("webhook_trigger")

    def test_a_blank_webhook_name_raises(self):
        from ingress_identity import IngressIdentityError, ingress_identity

        with pytest.raises(IngressIdentityError, match="webhook_name"):
            ingress_identity("webhook_trigger", webhook_name="")

    def test_an_oversized_webhook_name_raises_rather_than_truncating(self):
        from ingress_identity import IngressIdentityError, ingress_identity

        with pytest.raises(IngressIdentityError):
            ingress_identity("webhook_trigger", webhook_name="n" * 400)

    def test_an_unknown_clearance_raises(self):
        from ingress_identity import IngressIdentityError, ingress_identity

        with pytest.raises(IngressIdentityError, match="clearance"):
            ingress_identity(
                "webhook_trigger", webhook_name="nightly", clearance="secret")


class TestBootValidator:
    def test_the_shipped_table_is_valid(self):
        from ingress_identity import validate_ingress_identity_table

        validate_ingress_identity_table()

    def test_it_rejects_a_route_missing_a_peer(self, monkeypatch):
        import ingress_identity as ii

        broken = dict(ii._INGRESS_IDENTITY)
        broken["invoke"] = ii.IngressIdentityPolicy(
            surface="invoke", authenticated=True, clearance="private",
            peer_strategy="fixed", peer="",
        )
        monkeypatch.setattr(ii, "_INGRESS_IDENTITY", broken)
        with pytest.raises(ii.IngressIdentityError, match="peer"):
            ii.validate_ingress_identity_table()

    def test_it_rejects_an_unrecognized_peer_strategy(self, monkeypatch):
        import ingress_identity as ii

        broken = dict(ii._INGRESS_IDENTITY)
        broken["invoke"] = ii.IngressIdentityPolicy(
            surface="invoke", authenticated=True, clearance="private",
            peer_strategy="telepathy", peer="invoke_caller",
        )
        monkeypatch.setattr(ii, "_INGRESS_IDENTITY", broken)
        with pytest.raises(ii.IngressIdentityError, match="strategy"):
            ii.validate_ingress_identity_table()

    def test_it_rejects_a_dropped_route(self, monkeypatch):
        # A route silently deleted from the table would send its ingress back
        # to the unattributed ``system`` identity — exactly #203's failure.
        import ingress_identity as ii

        broken = dict(ii._INGRESS_IDENTITY)
        broken.pop("webhook_trigger")
        monkeypatch.setattr(ii, "_INGRESS_IDENTITY", broken)
        with pytest.raises(ii.IngressIdentityError, match="webhook_trigger"):
            ii.validate_ingress_identity_table()

    def test_every_authenticated_route_resolves_to_valid_provenance(self):
        # The invariant #203 actually wants: an authenticated ingress stamps a
        # usable user_peer. NOT "it names a human" — voice is authenticated and
        # anonymous by design.
        from ingress_identity import _INGRESS_IDENTITY, ingress_identity
        from speaker_provenance import UserProvenance, validate_speaker_provenance

        for route, policy in _INGRESS_IDENTITY.items():
            if not policy.authenticated:
                continue
            kwargs = {}
            if policy.peer_strategy == "webhook_name":
                kwargs["webhook_name"] = "probe"
            elif policy.peer_strategy == "telegram_sender":
                # #336: a telegram turn now REQUIRES a sender identity.
                kwargs["sender_id"] = "424242"
            origin = ingress_identity(route, **kwargs)
            assert origin.user_peer
            validate_speaker_provenance(UserProvenance.from_origin(
                surface=origin.surface, server_origin=origin.server_origin,
                authenticated_user=origin.authenticated_user,
                user_peer=origin.user_peer,
            ))


class TestBootValidatorEnforcesRouteContracts:
    """Review round 1 (Terra + Sol, independently): the first validator checked
    only INTERNAL coherence — coverage, clearance spelling, strategy spelling,
    non-empty peer. Every misattribution below therefore passed boot, which is
    exactly the defect class #203's check claims to catch.
    """

    def _broken(self, monkeypatch, route, **overrides):
        import dataclasses

        import ingress_identity as ii

        table = dict(ii._INGRESS_IDENTITY)
        table[route] = dataclasses.replace(table[route], **overrides)
        monkeypatch.setattr(ii, "_INGRESS_IDENTITY", table)
        return ii

    def test_a_webhook_given_a_fixed_peer_fails_boot(self, monkeypatch):
        # The single worst regression this table could suffer: every
        # third-party webhook silently recorded as authored by one identity.
        # Caught by the contract, which pins the per-trigger strategy.
        ii = self._broken(
            monkeypatch, "webhook_trigger",
            peer_strategy="fixed", peer="somebody",
        )
        with pytest.raises(ii.IngressIdentityError, match="webhook_trigger"):
            ii.validate_ingress_identity_table()

    def test_voice_repointed_at_another_peer_fails_boot(self, monkeypatch):
        ii = self._broken(monkeypatch, "voice_sse", peer="telegram:42")
        with pytest.raises(ii.IngressIdentityError, match="voice_sse"):
            ii.validate_ingress_identity_table()

    def test_a_machine_route_inside_the_human_namespace_fails_boot(
            self, monkeypatch):
        # The namespace guard itself, exercised on a route the contract
        # AGREES with — so nothing upstream of the guard can be what fires.
        # A fixed peer spelled inside the human namespace means a machine
        # caller would be persisted as a person.
        import ingress_identity as ii

        table = dict(ii._INGRESS_IDENTITY)
        contract = dict(ii._ROUTE_CONTRACT)
        table["matrix"] = ii.IngressIdentityPolicy(
            surface="invoke", authenticated=True, clearance="private",
            peer_strategy="fixed", peer="telegram:42",
        )
        contract["matrix"] = ("invoke", True, "fixed", "telegram:42", "automation")
        monkeypatch.setattr(ii, "_INGRESS_IDENTITY", table)
        monkeypatch.setattr(ii, "_ROUTE_CONTRACT", contract)
        with pytest.raises(ii.IngressIdentityError, match="human peer namespace"):
            ii.validate_ingress_identity_table()

    def test_a_machine_route_wearing_the_telegram_surface_fails_boot(
            self, monkeypatch):
        # Sol review: the namespace guards bound what a peer is NAMED, but
        # `from_origin` derives speaker_kind from the SURFACE — so this
        # arrangement (machine peer, telegram surface) passed every other
        # check and was still persisted as a user. Patched in BOTH the table
        # and the contract, because they agree by construction and the
        # contract comparison therefore cannot catch it.
        import ingress_identity as ii

        table = dict(ii._INGRESS_IDENTITY)
        contract = dict(ii._ROUTE_CONTRACT)
        table["invoke"] = dataclasses_replace(table["invoke"], surface="telegram")
        contract["invoke"] = ("telegram", True, "fixed", "invoke_caller", "user")
        monkeypatch.setattr(ii, "_INGRESS_IDENTITY", table)
        monkeypatch.setattr(ii, "_ROUTE_CONTRACT", contract)
        with pytest.raises(ii.IngressIdentityError, match="imply each other"):
            ii.validate_ingress_identity_table()

    def test_a_route_wearing_an_unknown_surface_fails_boot(self, monkeypatch):
        # Terra re-review: the biconditional only separates "telegram" from
        # everything else, but speaker_provenance classifies from its OWN
        # automation set — so a coordinated edit introducing a surface neither
        # module knows ("matrix") passed boot and fell through to
        # speaker_kind="user". The Literal annotation is a hint, not a gate.
        import ingress_identity as ii

        table = dict(ii._INGRESS_IDENTITY)
        contract = dict(ii._ROUTE_CONTRACT)
        table["invoke"] = dataclasses_replace(table["invoke"], surface="matrix")
        contract["invoke"] = ("matrix", True, "fixed", "invoke_caller", "automation")
        monkeypatch.setattr(ii, "_INGRESS_IDENTITY", table)
        monkeypatch.setattr(ii, "_ROUTE_CONTRACT", contract)
        with pytest.raises(ii.IngressIdentityError, match="not one of"):
            ii.validate_ingress_identity_table()

    def test_a_machine_route_wearing_the_voice_surface_fails_boot(
            self, monkeypatch):
        # Sol re-review: this arrangement satisfied namespace containment AND
        # the telegram biconditional — "voice" is a legitimate surface and
        # `voice_speaker` is outside the human namespace — yet an anonymous
        # voice speaker is classified as a PERSON, so the automation would be
        # persisted as one. Caught by comparing against the real classifier
        # rather than by reasoning about which surfaces look machine-like.
        import ingress_identity as ii

        table = dict(ii._INGRESS_IDENTITY)
        contract = dict(ii._ROUTE_CONTRACT)
        table["invoke"] = dataclasses_replace(
            table["invoke"], surface="voice", peer="voice_speaker")
        contract["invoke"] = ("voice", True, "fixed", "voice_speaker", "automation")
        monkeypatch.setattr(ii, "_INGRESS_IDENTITY", table)
        monkeypatch.setattr(ii, "_ROUTE_CONTRACT", contract)
        with pytest.raises(ii.IngressIdentityError, match="classified as 'user'"):
            ii.validate_ingress_identity_table()

    def test_a_human_route_downgraded_to_an_automation_fails_boot(
            self, monkeypatch):
        # The opposite direction: a person's turns recorded as machine output
        # loses the authorship the whole provenance model rests on.
        import ingress_identity as ii

        table = dict(ii._INGRESS_IDENTITY)
        contract = dict(ii._ROUTE_CONTRACT)
        table["voice_ws"] = dataclasses_replace(
            table["voice_ws"], surface="webhook")
        contract["voice_ws"] = ("webhook", True, "fixed", "voice_speaker", "user")
        monkeypatch.setattr(ii, "_INGRESS_IDENTITY", table)
        monkeypatch.setattr(ii, "_ROUTE_CONTRACT", contract)
        with pytest.raises(
                ii.IngressIdentityError, match="classified as 'automation'"):
            ii.validate_ingress_identity_table()

    def test_every_shipped_route_is_classified_as_its_contract_declares(self):
        # The positive statement, so the comparisons above cannot become
        # vacuous — and so the contract's declared kinds are not merely
        # self-consistent but match what the classifier really does.
        from ingress_identity import _INGRESS_IDENTITY, _ROUTE_CONTRACT, _classify

        for route, policy in _INGRESS_IDENTITY.items():
            expected_kind = _ROUTE_CONTRACT[route][4]
            probe = (
                "telegram:probe" if policy.peer_strategy == "telegram_sender"
                else "webhook:probe" if policy.peer_strategy == "webhook_name"
                else policy.peer or ""
            )
            assert _classify(policy, probe) == expected_kind, route
        kinds = {route: contract[4] for route, contract in _ROUTE_CONTRACT.items()}
        assert kinds == {
            "telegram": "user", "telegram_guest": "user",
            "voice_sse": "user", "voice_ws": "user",
            "invoke": "automation", "webhook_trigger": "automation",
        }

    def test_a_human_route_stripped_of_its_surface_fails_boot(self, monkeypatch):
        # The other half of the biconditional: the telegram route demoted to a
        # fixed peer would name every sender identically AND stop being a
        # person's surface. Caught even though table and contract agree.
        import ingress_identity as ii

        table = dict(ii._INGRESS_IDENTITY)
        contract = dict(ii._ROUTE_CONTRACT)
        table["telegram"] = dataclasses_replace(
            table["telegram"], peer_strategy="fixed", peer="somebody")
        contract["telegram"] = ("telegram", True, "fixed", "somebody", "user")
        monkeypatch.setattr(ii, "_INGRESS_IDENTITY", table)
        monkeypatch.setattr(ii, "_ROUTE_CONTRACT", contract)
        with pytest.raises(ii.IngressIdentityError, match="imply each other"):
            ii.validate_ingress_identity_table()

    def test_a_human_route_outside_the_human_namespace_fails_boot(
            self, monkeypatch):
        # The other direction: senders named by something outside the human
        # namespace is how the hardcoded operator constant looked. Patching
        # only the prefix leaves _HUMAN_PEER_PREFIXES pinned to the real one,
        # which is exactly the drift this must catch.
        import ingress_identity as ii

        monkeypatch.setattr(ii, "_TELEGRAM_PEER_PREFIX", "guest_")
        with pytest.raises(
                ii.IngressIdentityError, match="outside the human namespace"):
            ii.validate_ingress_identity_table()

    def test_a_wrong_surface_fails_boot(self, monkeypatch):
        # invoke -> surface "telegram" would make from_origin emit a USER
        # provenance instead of an automation: a machine promoted to a person.
        ii = self._broken(monkeypatch, "invoke", surface="telegram")
        with pytest.raises(ii.IngressIdentityError, match="invoke"):
            ii.validate_ingress_identity_table()

    def test_an_ingress_demoted_to_unauthenticated_fails_boot(self, monkeypatch):
        ii = self._broken(monkeypatch, "telegram", authenticated=False)
        with pytest.raises(ii.IngressIdentityError, match="telegram"):
            ii.validate_ingress_identity_table()

    def test_every_automation_route_still_yields_automation_at_boot(self):
        # The property the contract exists to protect, asserted directly.
        from ingress_identity import _INGRESS_IDENTITY, ingress_identity
        from speaker_provenance import UserProvenance

        for route in ("invoke", "webhook_trigger"):
            policy = _INGRESS_IDENTITY[route]
            kwargs = {"webhook_name": "probe"} if policy.peer_strategy == (
                "webhook_name") else {}
            origin = ingress_identity(route, **kwargs)
            assert UserProvenance.from_origin(
                surface=origin.surface, server_origin=origin.server_origin,
                authenticated_user=origin.authenticated_user,
                user_peer=origin.user_peer,
            ).speaker_kind == "automation"


class TestWebhookNameCanonicalization:
    """Terra r1: ``ingress_identity`` composes ``webhook:{name}`` from the RAW
    name, but ``from_origin`` NFC-normalizes ``user_peer``. Two names differing
    only in canonical form would collapse to one peer and one document id. The
    shipped trigger schema is ASCII-only so this is unreachable today — which
    is exactly why it must be closed here rather than left to schema drift.
    """

    def test_the_two_forms_really_are_distinct_inputs(self):
        # The decomposed and precomposed spellings below are visually
        # IDENTICAL. This pins that they are still two different strings, so a
        # future editor "tidying" the file cannot silently defang the test
        # below into asserting nothing.
        import unicodedata

        decomposed = "cafe\u0301"
        precomposed = "caf\u00e9"
        assert decomposed != precomposed
        assert unicodedata.normalize("NFC", decomposed) == precomposed

    def test_a_non_nfc_name_is_rejected_not_silently_folded(self):
        from ingress_identity import IngressIdentityError, ingress_identity

        # "cafe" + COMBINING ACUTE — NFC-folds onto the precomposed "café".
        with pytest.raises(IngressIdentityError, match="canonical"):
            ingress_identity("webhook_trigger", webhook_name="café")

    def test_the_precomposed_equivalent_still_works(self):
        from ingress_identity import ingress_identity

        origin = ingress_identity("webhook_trigger", webhook_name="café")
        assert origin.user_peer == "webhook:café"

    def test_the_composed_peer_survives_from_origin_unchanged(self):
        # The actual invariant: what the table stamps is what gets persisted.
        from ingress_identity import ingress_identity
        from speaker_provenance import UserProvenance

        origin = ingress_identity("webhook_trigger", webhook_name="plg-a--b")
        provenance = UserProvenance.from_origin(
            surface=origin.surface, server_origin=origin.server_origin,
            authenticated_user=origin.authenticated_user,
            user_peer=origin.user_peer,
        )
        assert provenance.user_peer == origin.user_peer


class TestOnlyTheTableMintsIdentities:
    """Terra r1: the "every ingress goes through the table" rule was stated in
    a docstring and enforced by nothing. One grep makes it structural."""

    def test_ingress_identity_is_the_sole_production_construction_site(self):
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent / (
            "casa/rootfs/opt/casa")
        sites = sorted(
            path.relative_to(root).as_posix()
            for path in root.rglob("*.py")
            if "TrustedUserOriginInput(" in path.read_text(encoding="utf-8")
        )
        assert sites == ["ingress_identity.py"], (
            "a trusted ingress identity was minted outside the ingress table; "
            f"found in: {sites}"
        )


class TestContractCoversTheWholeTable:
    """Re-review (Sol r2): the contract check was OPTIONAL for any route the
    contract did not mention (`if contract is not None`), so a new route added
    to the table alone got no semantic validation at all — the single-edit
    bypass the contract exists to prevent."""

    def test_a_route_added_without_a_contract_fails_boot(self, monkeypatch):
        import ingress_identity as ii

        table = dict(ii._INGRESS_IDENTITY)
        table["matrix"] = ii.IngressIdentityPolicy(
            surface="telegram", authenticated=True, clearance="private",
            peer_strategy="fixed", peer="matrix_caller",
        )
        monkeypatch.setattr(ii, "_INGRESS_IDENTITY", table)
        with pytest.raises(ii.IngressIdentityError, match="matrix"):
            ii.validate_ingress_identity_table()

    def test_the_table_and_the_contract_describe_the_same_routes(self):
        from ingress_identity import _INGRESS_IDENTITY, _ROUTE_CONTRACT

        assert set(_INGRESS_IDENTITY) == set(_ROUTE_CONTRACT)


class TestHumanPeersAreUnreachableFromTheWebhookNamespace:
    """Re-review (Sol r2): the guard probed one composed value
    (`webhook:probe`), so it proved nothing about the namespace. Emptying the
    prefix would let a trigger named after a person resolve to that person's
    peer while boot still passed.

    Since the operator-peer collapse the property is stated as namespace
    disjointness rather than against a literal set of operator peers — there is
    no literal left to compare with.
    """

    def test_both_namespace_prefixes_are_non_empty(self):
        from ingress_identity import (
            _TELEGRAM_PEER_PREFIX,
            _WEBHOOK_PEER_PREFIX,
        )

        assert _WEBHOOK_PEER_PREFIX
        assert _TELEGRAM_PEER_PREFIX

    def test_the_human_and_webhook_namespaces_are_disjoint(self):
        from ingress_identity import (
            _HUMAN_PEER_PREFIXES,
            _WEBHOOK_PEER_PREFIX,
        )

        for prefix in _HUMAN_PEER_PREFIXES:
            assert not prefix.startswith(_WEBHOOK_PEER_PREFIX)
            assert not _WEBHOOK_PEER_PREFIX.startswith(prefix)

    def test_a_trigger_named_like_a_sender_is_still_not_that_sender(self):
        # A trigger literally named "telegram:42" composes to
        # "webhook:telegram:42" — outside the human namespace, so it cannot
        # claim sender 42's memories.
        from ingress_identity import ingress_identity

        trigger = ingress_identity(
            "webhook_trigger", webhook_name="telegram:42").user_peer
        sender = ingress_identity("telegram", sender_id="42").user_peer
        assert trigger != sender

    def test_an_emptied_prefix_fails_boot(self, monkeypatch):
        import ingress_identity as ii

        monkeypatch.setattr(ii, "_WEBHOOK_PEER_PREFIX", "")
        with pytest.raises(ii.IngressIdentityError, match="namespace"):
            ii.validate_ingress_identity_table()

    def test_overlapping_namespaces_fail_boot(self, monkeypatch):
        # The disjointness check itself: a webhook prefix that is a prefix of
        # the human one would let some trigger name compose into it.
        import ingress_identity as ii

        monkeypatch.setattr(ii, "_WEBHOOK_PEER_PREFIX", "tele")
        with pytest.raises(ii.IngressIdentityError, match="not disjoint"):
            ii.validate_ingress_identity_table()
