"""#486: the plugin file handoff — capture, publish, the sweep, and the tools.

Every refusal test builds the hostile entry on a real filesystem and calls the
real ``capture``; a stubbed ``os`` would pass while the check never ran.
"""
from __future__ import annotations

import os
import threading
import time
from types import SimpleNamespace as NS

import pytest

import agent_inbox as ai
import casa_handoff as ch
import plugin_handoff as ph

PDF = b"%PDF-1.4\n" + b"x" * 64
SECRET = b"refresh_token=do-not-mail"


@pytest.fixture(autouse=True)
def _clean_module_state():
    ai._reset_for_tests()
    ph._reset_for_tests()
    yield
    ai._reset_for_tests()
    ph._reset_for_tests()


@pytest.fixture
def root(tmp_path):
    r = tmp_path / "handoff"
    ph.provision(str(r))
    return str(r)


@pytest.fixture
def secret(tmp_path):
    p = tmp_path / "token.json"
    p.write_bytes(SECRET)
    return str(p)


def _id(age_s: float = 0.0, now: float | None = None) -> str:
    return ch.new_id((time.time() if now is None else now) - age_s)


def _plant(root: str, producer: str = "gmail", ident: str | None = None) -> str:
    d = os.path.join(root, producer, ident or _id())
    os.makedirs(d)
    return d


# ---------------------------------------------------------------------------
# capture — the outcome it exists for: nothing outside the folder is taken
# ---------------------------------------------------------------------------


def test_capture_returns_the_published_bytes_and_name(root):
    out = ch.publish("gmail", "invoice-Q3.pdf", data=PDF, root=root)
    assert ch.capture(out["path"], root=root) == ("invoice-Q3.pdf", PDF)


def test_capture_refuses_a_leaf_symlink_to_a_credential(root, secret):
    d = _plant(root)
    os.symlink(secret, os.path.join(d, "invoice.pdf"))
    with pytest.raises(ch.HandoffError) as e:
        ch.capture(os.path.join(d, "invoice.pdf"), root=root)
    assert e.value.kind == "not_a_handoff_file"


def test_capture_refuses_a_producer_dir_linked_outside(root, secret, tmp_path):
    outside = tmp_path / "outside" / _id()
    outside.mkdir(parents=True)
    (outside / "invoice.pdf").write_bytes(SECRET)
    os.symlink(str(tmp_path / "outside"), os.path.join(root, "gmail"))
    path = os.path.join(root, "gmail", outside.name, "invoice.pdf")
    with pytest.raises(ch.HandoffError):
        ch.capture(path, root=root)


def test_capture_refuses_a_hard_link_to_a_credential(root, secret):
    d = _plant(root)
    os.link(secret, os.path.join(d, "invoice.pdf"))
    with pytest.raises(ch.HandoffError) as e:
        ch.capture(os.path.join(d, "invoice.pdf"), root=root)
    assert e.value.kind == "not_a_handoff_file"


def test_capture_refuses_a_fifo_without_hanging(root):
    d = _plant(root)
    os.mkfifo(os.path.join(d, "invoice.pdf"))
    result: list = []

    def run():
        try:
            ch.capture(os.path.join(d, "invoice.pdf"), root=root)
        except ch.HandoffError as exc:
            result.append(exc.kind)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(5)
    assert result == ["not_a_handoff_file"]


@pytest.mark.parametrize("make", [
    lambda root, secret: os.path.join(root, "gmail", _id(), "..", "..", "..",
                                      os.path.relpath(secret, os.path.dirname(root))),
    lambda root, secret: secret,
    lambda root, secret: root + "-x/gmail/" + _id() + "/a.pdf",
    lambda root, secret: "relative/path.pdf",
])
def test_capture_refuses_paths_that_resolve_outside(root, secret, make):
    with pytest.raises(ch.HandoffError) as e:
        ch.capture(make(root, secret), root=root)
    assert e.value.kind == "not_a_handoff_file"


def test_capture_refuses_a_lookalike_root(root, tmp_path):
    twin = str(tmp_path / "handoff-x")
    ph.provision(twin)
    out = ch.publish("gmail", "a.pdf", data=PDF, root=twin)
    with pytest.raises(ch.HandoffError):
        ch.capture(out["path"], root=root)


def test_capture_refuses_a_staging_path(root):
    d = os.path.join(root, "gmail", ch.STAGING_PREFIX + _id())
    os.makedirs(d)
    with open(os.path.join(d, "a.pdf"), "wb") as f:
        f.write(PDF)
    with pytest.raises(ch.HandoffError):
        ch.capture(os.path.join(d, "a.pdf"), root=root)


def test_capture_refuses_an_over_cap_file(root, monkeypatch):
    out = ch.publish("gmail", "a.pdf", data=PDF, root=root)
    monkeypatch.setattr(ch, "MAX_FILE_BYTES", len(PDF) - 1)
    with pytest.raises(ch.HandoffError) as e:
        ch.capture(out["path"], root=root)
    assert e.value.kind == "file_too_large"


# ---------------------------------------------------------------------------
# publish
# ---------------------------------------------------------------------------


def test_publish_lays_out_one_file_under_its_human_name(root):
    out = ch.publish("bank-feed", "export 2026.csv", data=b"a,b\n", root=root)
    producer_dir, ident, name = out["path"][len(root) + 1:].split(os.sep)
    assert (producer_dir, name) == ("bank-feed", "export 2026.csv")
    assert ch.ID_RE.match(ident)
    assert os.listdir(os.path.join(root, "bank-feed")) == [ident]
    assert out["size_bytes"] == 4
    assert out["expires_at"] == pytest.approx(time.time() + ch.RETENTION_S, abs=5)


def test_publish_from_src_writes_fresh_bytes_never_the_source_inode(root, tmp_path):
    src = tmp_path / "private.pdf"
    src.write_bytes(PDF)
    out = ch.publish("gmail", "a.pdf", src=str(src), root=root)
    assert os.stat(out["path"]).st_ino != os.stat(src).st_ino
    assert os.stat(out["path"]).st_nlink == 1
    assert os.stat(src).st_nlink == 1


def test_publish_refuses_a_symlinked_src(root, tmp_path, secret):
    link = tmp_path / "innocent.pdf"
    os.symlink(secret, link)
    with pytest.raises(ch.HandoffError):
        ch.publish("gmail", "a.pdf", src=str(link), root=root)
    assert os.listdir(root) == []


def test_publish_keeps_a_long_multibyte_name_publishable(root):
    name = "請" * 100 + ".pdf"          # 304 bytes as UTF-8
    out = ch.publish("casa", name, data=PDF, root=root)
    assert out["filename"].endswith(".pdf")
    assert len(out["filename"].encode()) <= ch.MAX_NAME_BYTES
    assert ch.capture(out["path"], root=root)[1] == PDF


def test_publish_refuses_over_the_per_file_cap(root, monkeypatch):
    monkeypatch.setattr(ch, "MAX_FILE_BYTES", 10)
    with pytest.raises(ch.HandoffError) as e:
        ch.publish("gmail", "a.pdf", data=PDF, root=root)
    assert e.value.kind == "file_too_large"


def test_publish_refuses_when_the_folder_is_full_and_evicts_nothing(root, monkeypatch):
    first = ch.publish("gmail", "a.pdf", data=PDF, root=root)
    monkeypatch.setattr(ch, "MAX_TOTAL_BYTES", len(PDF) + 10)
    with pytest.raises(ch.HandoffError) as e:
        ch.publish("gmail", "b.pdf", data=PDF, root=root)
    assert e.value.kind == "handoff_full"
    assert os.path.exists(first["path"])
    assert len(os.listdir(os.path.join(root, "gmail"))) == 1


def test_publish_refuses_when_the_disk_is_nearly_full(root, monkeypatch):
    monkeypatch.setattr(ch.os, "statvfs",
                        lambda p: NS(f_bavail=1, f_frsize=ch.FREE_RESERVE_BYTES))
    with pytest.raises(ch.HandoffError) as e:
        ch.publish("gmail", "a.pdf", data=PDF, root=root)
    assert e.value.kind == "handoff_full"


def test_publish_without_the_folder_says_so(tmp_path):
    with pytest.raises(ch.HandoffError) as e:
        ch.publish("gmail", "a.pdf", data=PDF, root=str(tmp_path / "absent"))
    assert e.value.kind == "handoff_unavailable"


@pytest.mark.parametrize("producer", ["", "Gmail", "../x", ".staging-1", "a/b"])
def test_publish_refuses_a_bad_producer_name(root, producer):
    with pytest.raises(ch.HandoffError) as e:
        ch.publish(producer, "a.pdf", data=PDF, root=root)
    assert e.value.kind == "bad_producer"


def test_a_failed_publication_leaves_no_staging(root, monkeypatch):
    def boom(*a, **k):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(ch.os, "rename", boom)
    with pytest.raises(ch.HandoffError) as e:
        ch.publish("gmail", "a.pdf", data=PDF, root=root)
    assert e.value.kind == "storage_failed"
    assert os.listdir(os.path.join(root, "gmail")) == []


@pytest.mark.parametrize("raw,expected", [
    ("invoice.pdf", "invoice.pdf"),
    ("../../etc/passwd", "_.._etc_passwd"),
    (".hidden.pdf", "hidden.pdf"),
    ("a\x00b\x07.pdf", "ab.pdf"),
    ("", "file"),
    ("...", "file"),
])
def test_clean_filename(raw, expected):
    assert ch.clean_filename(raw, "file") == expected


# ---------------------------------------------------------------------------
# the sweep
# ---------------------------------------------------------------------------


def test_sweep_removes_at_seven_days_and_not_a_second_before(root):
    now = time.time()
    young = ch.publish("gmail", "a.pdf", data=PDF, root=root,
                       now=now - ch.RETENTION_S + 1)
    old = ch.publish("gmail", "b.pdf", data=PDF, root=root,
                     now=now - ch.RETENTION_S)
    stats = ph.sweep(root, now=now)
    assert os.path.exists(young["path"]) and not os.path.exists(old["path"])
    assert (stats["expired"], stats["kept"]) == (1, 1)


def test_sweep_keeps_a_future_dated_file(root):
    now = time.time()
    out = ch.publish("gmail", "a.pdf", data=PDF, root=root, now=now + 3 * 3600)
    ph.sweep(root, now=now)
    assert os.path.exists(out["path"])


def test_sweep_removes_links_and_off_pattern_entries(root, secret):
    keep = ch.publish("gmail", "keep.pdf", data=PDF, root=root)
    os.symlink(secret, os.path.join(root, "linked"))                  # root level
    d1 = _plant(root)
    os.symlink(secret, os.path.join(d1, "a.pdf"))                     # leaf link
    d2 = _plant(root)
    os.link(secret, os.path.join(d2, "a.pdf"))                        # hard link
    os.makedirs(os.path.join(root, "gmail", "not-an-id"))            # off-pattern
    os.makedirs(os.path.join(root, "Not_A_Producer", _id()))         # bad producer
    with open(os.path.join(root, "stray.txt"), "w") as f:            # non-dir
        f.write("x")
    stats = ph.sweep(root)
    assert sorted(os.listdir(root)) == ["gmail"]
    assert os.listdir(os.path.join(root, "gmail")) == [
        os.path.basename(os.path.dirname(keep["path"]))]
    assert stats["foreign"] == 6
    assert os.path.exists(secret), "removal must never follow a link"


def test_sweep_reclaims_abandoned_staging_only_after_an_hour(root):
    now = time.time()
    old = os.path.join(root, "gmail", ch.STAGING_PREFIX + _id(3601, now))
    young = os.path.join(root, "gmail", ch.STAGING_PREFIX + _id(60, now))
    os.makedirs(old)
    os.makedirs(young)
    ph.sweep(root, now=now)
    assert not os.path.exists(old) and os.path.exists(young)


async def test_wire_provisions_the_folder_and_registers_the_sweep(tmp_path):
    jobs = []
    sched = NS(add_job=lambda *a, **k: jobs.append(k["id"]))
    target = str(tmp_path / "handoff")
    await ph.wire(sched, target)
    assert ph.root() == target
    assert os.stat(target).st_mode & 0o777 == 0o770
    assert jobs == ["plugin_handoff_sweep"]


async def test_a_failed_boot_sweep_leaves_the_folder_available(tmp_path, monkeypatch):
    jobs = []
    def boom(root, now=None):
        raise OSError("disk error")
    monkeypatch.setattr(ph, "sweep", boom)
    await ph.wire(NS(add_job=lambda *a, **k: jobs.append(k["id"])), str(tmp_path / "h"))
    assert ph.root() == str(tmp_path / "h")
    assert jobs == ["plugin_handoff_sweep"]


async def test_wire_failure_leaves_no_folder_and_does_not_raise(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    await ph.wire(NS(add_job=lambda *a, **k: None), str(blocker / "handoff"))
    assert ph.root() is None


# ---------------------------------------------------------------------------
# share_inbound_file / list_inbound_files
# ---------------------------------------------------------------------------


class _Sched:
    def add_job(self, *a, **k):
        pass


@pytest.fixture
async def inbox(tmp_path):
    await ai.wire(_Sched(), str(tmp_path / "agent-inbox"), role="assistant")
    return ai.get_inbox("assistant")


@pytest.fixture
async def handoff(tmp_path):
    await ph.wire(_Sched(), str(tmp_path / "handoff"))
    return ph.root()


async def _call(tool, args: dict, origin: dict) -> str:
    import agent as agent_mod
    token = agent_mod.origin_var.set(origin)
    try:
        out = await tool.handler(args)
    finally:
        agent_mod.origin_var.reset(token)
    return out["content"][0]["text"]


async def test_share_publishes_under_the_operators_name(inbox, handoff):
    import tools
    await inbox.publish(PDF, ".pdf", "statement-q3.pdf")
    [f] = inbox.list_files()
    text = await _call(tools.share_inbound_file, {"path": f.path}, {"role": "assistant"})
    shared = next(line for line in text.splitlines() if line.startswith(handoff))
    assert ch.capture(shared, root=handoff) == ("statement-q3.pdf", PDF)
    assert os.path.exists(f.path), "the inbox copy is untouched"


async def test_share_names_a_photo_by_its_kind(inbox, handoff):
    import tools
    png = b"\x89PNG\r\n\x1a\n" + b"x" * 32
    await inbox.publish(png, ".png", "")
    [f] = inbox.list_files()
    text = await _call(tools.share_inbound_file, {"path": f.path}, {"role": "assistant"})
    assert "photo.png" in text


async def test_share_names_a_document_with_no_display_name_by_its_kind(inbox, handoff):
    import tools
    await inbox.publish(PDF, ".pdf", "lost.pdf")
    [f] = inbox.list_files()
    for meta in os.listdir(inbox.meta_dir):               # meta is best-effort
        os.unlink(os.path.join(inbox.meta_dir, meta))
    text = await _call(tools.share_inbound_file, {"path": f.path}, {"role": "assistant"})
    assert "file.pdf" in text and "photo" not in text


async def test_share_refuses_a_path_that_is_not_an_inbound_file(inbox, handoff, secret):
    import tools
    await inbox.publish(PDF, ".pdf", "real.pdf")     # a real file is present
    text = await _call(tools.share_inbound_file, {"path": secret}, {"role": "assistant"})
    assert "not one of your inbound files" in text
    assert os.listdir(handoff) == []


async def test_a_delegate_neither_lists_nor_shares_its_callers_files(inbox, handoff):
    import tools
    await inbox.publish(PDF, ".pdf", "x.pdf")
    [f] = inbox.list_files()
    delegated = {"role": "assistant", "execution_role": "finance"}
    listed = await _call(tools.list_inbound_files, {}, delegated)
    shared = await _call(tools.share_inbound_file, {"path": f.path}, delegated)
    assert f.path not in listed and "no inbound files" in listed
    assert "no inbound files to share" in shared
    assert os.listdir(handoff) == []


async def test_share_without_the_folder_says_so(inbox):
    import tools
    await inbox.publish(PDF, ".pdf", "x.pdf")
    [f] = inbox.list_files()
    text = await _call(tools.share_inbound_file, {"path": f.path}, {"role": "assistant"})
    assert "handoff folder is not available" in text


def test_share_is_granted_to_the_assistant_only():
    import yaml
    base = os.path.join(os.path.dirname(ch.__file__), "defaults")
    granted = []
    for dirpath, _dirs, files in os.walk(base):
        for name in files:
            if name.endswith((".yaml", ".yml")):
                with open(os.path.join(dirpath, name)) as fh:
                    if "mcp__casa-framework__share_inbound_file" in fh.read():
                        granted.append(os.path.relpath(os.path.join(dirpath, name), base))
    assert sorted(granted) == ["agents/assistant/runtime.yaml",
                               "roles/resident/assistant/role.yaml"]
    with open(os.path.join(base, "roles/resident/assistant/role.yaml")) as fh:
        assert "mcp__casa-framework__share_inbound_file" in yaml.safe_load(fh)["tools"]["allowed"]
