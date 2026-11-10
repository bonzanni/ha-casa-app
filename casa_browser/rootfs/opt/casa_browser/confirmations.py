"""The operator's questions (action model, design rev 10 §2.5; plan S2-9, S2-10).

A check that answers ``ask`` freezes a question: a ``confirmations`` row (migration 8) with its
view, state and the stored response of its one answer, and — in controller memory, because an
element handle cannot be stored — the frozen operation (``FrozenQuestion``). This module builds
the view and decides, mechanically, whether a confirm button may be offered:

* **F6.** A ``payment``, a ``commitment`` the check asked a screenshot for (``required``: the
  rulebook asks it exactly for money actions), or a page holding a payment-card field needs the
  app's own question screenshot; without a faithful one there is no confirm button.
* **F6a.** ``text_incomplete: true`` (the check could not read a fact the operator needs) means
  no confirm button; that includes account changes with no stated terms (ruling (a)).
* **Blind.** A reading the app could not see whole for any cause but the control cap has no final
  read to press on: no confirm button, and no plan card.
* **F2.** A ``go`` whose category is a change becomes a question with no check wording: fixed copy
  and no confirm button.

Every question offers [Open live view] and [Not now] (F5, slice 3 renders them). The view never
carries an exact destination.

Slice 5 adds the **login question** (design §5.1 step 3): which 1Password item signs in to a site.
Its view is fixed app copy filled from the browser's real host and the vault's titles; its buttons
are [Yes] [No] for one candidate, the titles and [None of these] for several (no confirm, no
live view). The item ids stay in controller memory (``LoginQuestion``); the view carries titles.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from casa_browser.masking import mask_text

QUESTION_TTL_S = 3600          # a question lives as long as its tap tokens (one hour)
MAX_REJUDGES = 2               # re-judgements per tap (ruling 8: "at most twice in all")

COPY_F2 = ("Ellen was about to {verb} “{name}” on {site}, and the check says it may change "
           "something but did not describe it. You can do it in the live view.")
COPY_F6 = "I can't show you this page reliably; you can do it in the live view."
COPY_F6A = "I can't check the amount on this page myself, so please finish it in the live view."
COPY_BLIND = "I can't re-check this page before pressing, so please do it in the live view."
# Why a Yes could not be checked again at the tap: nothing was pressed, whichever it is. A long page
# is not unreadable: only a reading that came back blind (``gate.blind``) has this copy.
COPY_INCOMPLETE = ("I can't read this page whole, so I can't check it again and didn't press "
                   "anything; you can finish it in the live view.")
COPY_TOO_MUCH = ("This page changed too much since I asked to check it again, so I didn't press "
                 "anything; you can finish it in the live view.")

VERBS = {"click": "press", "download": "press", "type": "type into", "select": "choose",
         "navigate": "open", "search": "open", "back": "go back from"}


@dataclass(eq=False)
class FrozenQuestion:
    """A question's frozen operation, in controller memory (design §2.5 "Freezing")."""

    question_id: str
    revision: int
    digest: str
    pending: Any = field(repr=False)        # the action's gate.PendingJudgement
    verdict: dict = field(repr=False)
    approved_packet: dict = field(repr=False)
    approved_reading: dict = field(repr=False)
    screenshot_id: str | None = None
    confirm_label: str = ""


@dataclass(eq=False)
class LoginQuestion:
    """A login question's frozen choice, in controller memory: the candidate item ids by index,
    never in a view, a packet or a reply."""

    question_id: str
    revision: int
    digest: str
    site_id: str
    item_ids: list = field(repr=False)
    titles: list = field(repr=False)
    pending: Any = None                      # (no action: nothing to execute or quarantine)


LOGIN_ONE = "To sign in to {host} I'd use your 1Password item \"{title}\". OK?"
LOGIN_MANY = "To sign in to {host}, which 1Password item?"
MAX_LOGIN_CANDIDATES = 5


def login_candidates(items: list, order: list) -> list:
    """The candidates the operator is offered, in the check's order: at most five, each shown
    by its whole title — with its website hosts when another candidate has the same title, and
    left out when even those are the same as an earlier one's (the operator could not tell
    which runs)."""
    out, seen = [], set()
    titles = [items[i]["title"] for i in order]
    for i in order:
        item = items[i]
        label = item["title"]
        if titles.count(label) > 1:
            hosts = ", ".join(item.get("website_hosts") or ()) or "no website"
            label = f"{label} ({hosts})"
        if label in seen:
            continue
        seen.add(label)
        out.append((i, label))
    return out[:MAX_LOGIN_CANDIDATES]


def login_view(host: str, labels: list) -> dict:
    """The login question's view (``kind: login_item``): no confirm button, no extra options,
    no screenshot; ``options`` are the titles the buttons show."""
    text = (LOGIN_ONE.format(host=host, title=labels[0]) if len(labels) == 1
            else LOGIN_MANY.format(host=host))
    return {"kind": "login_item", "host": host, "options": list(labels), "text": text,
            "confirm_label": "", "extra_options": [], "highlight": [], "screenshot_id": None,
            "confirm_offered": False, "live_only_copy": None, "category": "login"}


# -- slice 5n: the sign-in card for a site that is not set up --------------------------------

@dataclass(frozen=True)
class SignInProposal:
    """What the sign-in card shows and what its Yes registers, read once from the browser:
    the page's origin (the one spelling registration stores), its host as shown, the sign-in
    page's address without query or fragment, the label, and the app's own screenshot."""

    origin: str = field(repr=False)
    host: str
    start_page: str = field(repr=False)
    name: str
    screenshot_id: str | None = None
    site_id: str | None = None               # a registered site that is exactly this origin


@dataclass(eq=False)
class SiteQuestion:
    """A sign-in card's frozen choice, in controller memory: the proposal, and the one vault
    item the card names (its id never in a view, a packet or a reply). ``candidates``: how many
    items the check found (0, 1, or more), which decides what follows a Yes."""

    question_id: str
    revision: int
    digest: str
    proposal: SignInProposal
    item_id: str | None = field(default=None, repr=False)
    item_title: str | None = None
    candidates: int = 0
    pending: Any = None                      # (no action: nothing to execute or quarantine)


SITE_PLAIN = "Sign in to {host} ({name})?"
SITE_ONE = "Sign in to {host} ({name}) with your 1Password item \"{title}\"?"
SITE_SAVES = ("Yes, and next time I'll sign in here without asking. "
              "You can undo that in Casa Browser's Sites page.")


def site_view(proposal: SignInProposal, item_title: str | None) -> dict:
    """The sign-in card's view (``kind: site_signin``): the host from the browser, the label,
    the one fitting item's whole title or none, the app's screenshot; Yes and No only."""
    first = (SITE_ONE.format(host=proposal.host, name=proposal.name, title=item_title)
             if item_title else SITE_PLAIN.format(host=proposal.host, name=proposal.name))
    return {"kind": "site_signin", "host": proposal.host, "name": proposal.name,
            "item": item_title, "text": first + "\n" + SITE_SAVES, "options": [],
            "confirm_label": "", "extra_options": [], "highlight": [],
            "screenshot_id": proposal.screenshot_id, "confirm_offered": False,
            "live_only_copy": None, "category": "login"}


def money(category: str, question: dict | None, has_cc: bool) -> bool:
    """F6: a question that must carry the app's screenshot."""
    return (category == "payment"
            or (category == "commitment" and (question or {}).get("screenshot") == "required")
            or bool(has_cc))


def wants_screenshot(category: str, question: dict | None, has_cc: bool) -> bool:
    if money(category, question, has_cc):
        return True
    return (question or {}).get("screenshot") in ("required", "useful")


def build_view(verdict: dict, *, has_cc: bool, screenshot_id: str | None, f2: bool,
               verb: str, name: str, site: str, plan: str | None = None,
               blind: bool = False) -> dict:
    """The question view the plugin renders (design §2.5, F5–F6a), from the check's question."""
    category = verdict.get("category", "unclear")
    q = None if f2 else verdict.get("question")
    copy = None
    if q is None:
        copy = COPY_F2.format(verb=verb, name=_m(name, 80), site=_m(site, 80))
    elif blind:
        copy = COPY_BLIND
    elif q.get("text_incomplete"):
        copy = COPY_F6A
    elif money(category, q, has_cc) and screenshot_id is None:
        copy = COPY_F6
    view = {"text": copy if q is None else q["text"],
            "confirm_label": "" if q is None else q["confirm_label"],
            "extra_options": [] if q is None else list(q.get("extra_options") or []),
            "highlight": [] if q is None else list(q.get("highlight") or []),
            "screenshot_id": screenshot_id,
            "confirm_offered": copy is None,
            "live_only_copy": copy,
            "category": category}
    if plan is not None:
        view["plan"] = plan
    return view


def _m(text: str, limit: int) -> str:
    return mask_text(text or "").text.strip()[:limit]
