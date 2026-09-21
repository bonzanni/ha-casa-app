"""agent_inbox (#1036): the inbound-file folder the Telegram default agent reads.

Every test here runs against a real directory. The guarantees under test are
filesystem facts — what a name resolves to, what a directory holds, what a flush
did — and a mocked filesystem would pass while every one of them was broken.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time

import httpx
import pytest

import agent_inbox as ai

PDF = b"%PDF-1.4\n" + b"x" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
TEXT = b"vendor,amount\nacme,12.50\n"


@pytest.fixture
def inbox(tmp_path):
    ib = ai.open_inbox("assistant", str(tmp_path))
    yield ib
    ib.close()


@pytest.fixture(autouse=True)
def _clean_module_state():
    ai._reset_for_tests()
    yield
    ai._reset_for_tests()


def _ready(ib):
    return sorted(os.listdir(ib.ready_dir))


def _staging(ib):
    return sorted(os.listdir(ib.staging_dir))


# ---------------------------------------------------------------------------
# Names: no operator byte reaches a path
# ---------------------------------------------------------------------------

HOSTILE = [
    "../../etc/passwd.pdf",
    "..\\..\\x.pdf",
    "a\x00b.pdf",
    "%2e%2e%2finvoice.pdf",
    "x" * 300 + ".pdf",
    "invoice‮fdp.exe.pdf",   # RTL override
    "/abs/path/statement.PDF",
]


@pytest.mark.parametrize("hostile", HOSTILE)
async def test_no_operator_byte_reaches_a_path(inbox, hostile):
    ext = ai.extension_of(hostile)
    assert ext == ".pdf"
    receipt = await inbox.publish(PDF, ext, hostile)
    assert receipt.outcome is ai.Outcome.STORED
    assert ai._NAME_RE.fullmatch(receipt.name)
    published = os.path.join(inbox.ready_dir, receipt.name)
    assert os.path.realpath(published) == os.path.join(
        os.path.realpath(inbox.ready_dir), receipt.name)
    assert _ready(inbox) == [receipt.name]


@pytest.mark.parametrize("name", ["x.zip", "x.docx", "x.exe", "noext", ".pdf",
                                  "", None, "x.pdf.", "x.PdFx"])
def test_extension_is_drawn_from_the_closed_vocabulary(name):
    assert ai.extension_of(name) is None


def test_extension_is_case_folded_and_final_suffix_only():
    assert ai.extension_of("Statement.Q3.PDF") == ".pdf"
    assert ai.extension_of("photo.JPEG") == ".jpeg"
    assert ai.extension_of("report.pdf.txt") == ".txt"


async def test_display_name_is_kept_as_metadata_only(inbox):
    receipt = await inbox.publish(PDF, ".pdf", "statement-q3.pdf")
    [f] = inbox.list_files()
    assert f.display_name == "statement-q3.pdf"
    assert "statement" not in f.name and "statement" not in f.path


# ---------------------------------------------------------------------------
# Content gates
# ---------------------------------------------------------------------------


async def test_a_mismatch_is_refused_and_leaves_nothing(inbox):
    receipt = await inbox.publish(b"not a pdf at all", ".pdf", "fake.pdf")
    assert receipt.outcome is ai.Outcome.MISMATCH
    assert _ready(inbox) == [] and _staging(inbox) == []


async def test_received_bytes_decide_not_the_declared_size(inbox):
    async def fetch(cap):
        return PDF + b"x" * (cap + 1)       # declared small, delivered large
    receipt = await inbox.receive(fetch, ext=".pdf", display_name="big.pdf",
                                  declared_size=1024)
    assert receipt.outcome is ai.Outcome.TOO_LARGE
    assert _ready(inbox) == [] and _staging(inbox) == []


async def test_a_declared_oversize_is_refused_before_download(inbox):
    called = False

    async def fetch(cap):
        nonlocal called
        called = True
        return PDF
    receipt = await inbox.receive(fetch, ext=".pdf", display_name="x.pdf",
                                  declared_size=ai.CAP_BYTES + 1)
    assert receipt.outcome is ai.Outcome.TOO_LARGE
    assert not called


@pytest.mark.parametrize("data,ext", [(PDF, ".pdf"), (PNG, ".png"),
                                      (TEXT, ".csv")])
async def test_each_allowed_kind_publishes(inbox, data, ext):
    receipt = await inbox.publish(data, ext, f"f{ext}")
    assert receipt.outcome is ai.Outcome.STORED


# ---------------------------------------------------------------------------
# Download: deadline, local path, streaming cap
# ---------------------------------------------------------------------------


async def test_a_hung_download_ends_at_the_deadline_leaving_nothing(inbox, monkeypatch):
    monkeypatch.setattr(ai, "UPLOAD_DEADLINE_S", 0.05)

    async def never(cap):
        await asyncio.Event().wait()
    receipt = await inbox.receive(never, ext=".pdf", display_name="x.pdf",
                                  declared_size=None)
    assert receipt.outcome is ai.Outcome.DOWNLOAD_FAILED
    assert _ready(inbox) == [] and _staging(inbox) == []


class _Bot:
    def __init__(self, file_path, base="https://api.telegram.org/file/botTOKEN"):
        self.base_file_url = base
        self._file_path = file_path

    async def get_file(self, file_id):
        class F:
            pass
        f = F()
        f.file_path = self._file_path
        return f


async def test_a_local_path_from_getfile_is_refused(tmp_path):
    secret = tmp_path / "options.json"
    secret.write_text('{"token": "s3cret"}')
    with pytest.raises(ai.LocalPathRefused):
        await ai.fetch_telegram_file(_Bot(str(secret)), "fid", ai.CAP_BYTES)


@pytest.mark.parametrize("url", [
    "https://evil.example/file/botTOKEN/documents/x.pdf",
    "file:///data/options.json",
    "https://api.telegram.org/file/botOTHER/documents/x.pdf",
    None,
])
async def test_only_urls_under_the_configured_endpoint_are_fetched(url):
    with pytest.raises(ai.LocalPathRefused):
        await ai.fetch_telegram_file(_Bot(url), "fid", ai.CAP_BYTES)


async def test_the_stream_is_cut_at_the_cap():
    served = 0

    def handler(request):
        nonlocal served

        async def body():
            nonlocal served
            for _ in range(100):
                served += 1
                yield b"x" * 1024
        return httpx.Response(200, content=body())
    bot = _Bot("https://api.telegram.org/file/botTOKEN/documents/x.pdf")
    with pytest.raises(ai.TooLarge):
        await ai.fetch_telegram_file(bot, "fid", 10 * 1024,
                                     transport=httpx.MockTransport(handler))
    assert served < 100          # stopped reading once over the cap


async def test_a_redirect_is_not_followed():
    """The redirect target serves a perfectly good file, so a client that DID
    follow it would succeed — only refusing to follow makes this fail. (A target
    that also redirected would loop into TooManyRedirects and pass either way.)"""
    hosts = []

    def handler(request):
        hosts.append(request.url.host)
        if request.url.host == "api.telegram.org":
            return httpx.Response(302, headers={"Location": "https://evil.example/x.pdf"})
        return httpx.Response(200, content=PDF)
    bot = _Bot("https://api.telegram.org/file/botTOKEN/documents/x.pdf")
    with pytest.raises(ai.DownloadError):
        await ai.fetch_telegram_file(bot, "fid", ai.CAP_BYTES,
                                     transport=httpx.MockTransport(handler))
    assert hosts == ["api.telegram.org"]


# ---------------------------------------------------------------------------
# Capacity: refuse when full, never evict
# ---------------------------------------------------------------------------


async def test_a_full_folder_refuses_and_evicts_nothing(inbox, monkeypatch):
    monkeypatch.setattr(ai, "MAX_FILES", 3)
    names = [(await inbox.publish(PDF, ".pdf", f"{i}.pdf")).name for i in range(3)]
    receipt = await inbox.publish(PDF, ".pdf", "fourth.pdf")
    assert receipt.outcome is ai.Outcome.FULL
    assert _ready(inbox) == sorted(names)
    assert _staging(inbox) == []


async def test_a_full_folder_is_refused_before_downloading(inbox, monkeypatch):
    monkeypatch.setattr(ai, "MAX_FILES", 1)
    await inbox.publish(PDF, ".pdf", "first.pdf")
    fetched = False

    async def fetch(cap):
        nonlocal fetched
        fetched = True
        return PDF
    receipt = await inbox.receive(fetch, ext=".pdf", display_name="x.pdf",
                                  declared_size=100)
    assert receipt.outcome is ai.Outcome.FULL
    assert not fetched


async def test_the_byte_ceiling_refuses_below_the_file_count(inbox, monkeypatch):
    """Capacity is files AND bytes: a folder under the file count but at the
    byte ceiling refuses too."""
    monkeypatch.setattr(ai, "MAX_BYTES", len(PDF) * 2)
    await inbox.publish(PDF, ".pdf", "one.pdf")
    await inbox.publish(PDF, ".pdf", "two.pdf")
    receipt = await inbox.publish(PDF, ".pdf", "three.pdf")
    assert receipt.outcome is ai.Outcome.FULL
    assert len(_ready(inbox)) == 2 < ai.MAX_FILES


async def test_a_staged_file_that_gained_a_link_is_not_published(inbox, monkeypatch):
    """The final descriptor check before the rename: a staged file with a second
    link is not the file Casa wrote alone, and is not published."""
    real_write = inbox._write_part

    def write_then_link(part, data):
        real_write(part, data)
        os.link(part, part + "-alias", src_dir_fd=inbox._staging_fd,
                dst_dir_fd=inbox._staging_fd)
    monkeypatch.setattr(inbox, "_write_part", write_then_link)
    receipt = await inbox.publish(PDF, ".pdf", "x.pdf")
    assert receipt.outcome is ai.Outcome.STORAGE_FAILED
    assert _ready(inbox) == []


async def test_a_staged_file_that_changed_size_is_not_published(inbox, monkeypatch):
    real_write = inbox._write_part

    def write_then_grow(part, data):
        real_write(part, data)
        fd = os.open(part, os.O_WRONLY | os.O_APPEND, dir_fd=inbox._staging_fd)
        os.write(fd, b"extra")
        os.close(fd)
    monkeypatch.setattr(inbox, "_write_part", write_then_grow)
    receipt = await inbox.publish(PDF, ".pdf", "x.pdf")
    assert receipt.outcome is ai.Outcome.STORAGE_FAILED
    assert _ready(inbox) == [] and _staging(inbox) == []


async def test_retention_starts_at_publication_not_before_the_flush(inbox, monkeypatch):
    """A slow write must not eat into the promised retention: the name — and the
    epoch retention is measured from — is minted at the rename."""
    monkeypatch.setattr(ai, "RETENTION_S", 0.5)
    real_write = inbox._write_part

    def slow_write(part, data):
        time.sleep(1.0)
        real_write(part, data)
    monkeypatch.setattr(inbox, "_write_part", slow_write)
    receipt = await inbox.publish(PDF, ".pdf", "x.pdf")
    assert receipt.outcome is ai.Outcome.STORED
    assert inbox.sweep()["expired"] == 0
    assert _ready(inbox) == [receipt.name]


async def test_two_uploads_cannot_both_pass_the_ceiling(inbox, monkeypatch):
    """Both uploads must pass a SOLITARY quota check for this to test anything:
    the check is slowed so that, without serialization, each would see one file
    and both would publish."""
    monkeypatch.setattr(ai, "MAX_FILES", 2)
    await inbox.publish(PDF, ".pdf", "first.pdf")
    real_check = inbox._capacity_ok

    def slow_check(incoming):
        ok = real_check(incoming)
        time.sleep(0.1)          # widen the window between check and rename
        return ok
    monkeypatch.setattr(inbox, "_capacity_ok", slow_check)
    a, b = await asyncio.gather(inbox.publish(PDF, ".pdf", "a.pdf"),
                                inbox.publish(PDF, ".pdf", "b.pdf"))
    assert sorted([a.outcome.value, b.outcome.value]) == ["full", "stored"]
    assert len(_ready(inbox)) == 2
    assert _staging(inbox) == []


async def test_the_lock_is_never_held_across_the_file_flush(inbox, monkeypatch):
    held_during_fsync = []
    real_fsync = os.fsync

    def watching_fsync(fd):
        held_during_fsync.append(inbox._lock().locked())
        return real_fsync(fd)
    monkeypatch.setattr(ai.os, "fsync", watching_fsync)
    await inbox.publish(PDF, ".pdf", "x.pdf")
    assert held_during_fsync, "no flush happened at all"
    assert not any(held_during_fsync)


# ---------------------------------------------------------------------------
# Durability: acknowledged means flushed; uncertain means said
# ---------------------------------------------------------------------------


async def test_a_failed_file_flush_publishes_nothing(inbox, monkeypatch):
    real_fsync = os.fsync

    def failing_file_fsync(fd):
        if fd != inbox._ready_fd:
            raise OSError(5, "EIO")
        return real_fsync(fd)
    monkeypatch.setattr(ai.os, "fsync", failing_file_fsync)
    receipt = await inbox.publish(PDF, ".pdf", "x.pdf")
    assert receipt.outcome is ai.Outcome.STORAGE_FAILED
    assert _ready(inbox) == [] and _staging(inbox) == []


async def test_a_failed_directory_flush_is_never_acknowledged(inbox, monkeypatch):
    real_fsync = os.fsync

    def failing_dir_fsync(fd):
        if fd == inbox._ready_fd:
            raise OSError(5, "EIO")
        return real_fsync(fd)
    monkeypatch.setattr(ai.os, "fsync", failing_dir_fsync)
    receipt = await inbox.publish(PDF, ".pdf", "x.pdf")
    assert receipt.outcome is ai.Outcome.UNCERTAIN
    assert receipt.name == ""                   # nothing acknowledged
    assert _staging(inbox) == []


# ---------------------------------------------------------------------------
# The ready/ invariant and retention
# ---------------------------------------------------------------------------


async def test_the_sweep_removes_everything_casa_did_not_write(inbox, tmp_path, caplog):
    stored = (await inbox.publish(PDF, ".pdf", "keep.pdf")).name
    outside = tmp_path / "secret.txt"
    outside.write_text("vault token")
    os.symlink(outside, os.path.join(inbox.ready_dir, "1700000000000-aaaaaaaaaaaaaaaa.pdf"))
    os.mkdir(os.path.join(inbox.ready_dir, "1700000000000-bbbbbbbbbbbbbbbb.pdf"))
    os.link(outside, os.path.join(inbox.ready_dir, "1700000000000-cccccccccccccccc.pdf"))
    with open(os.path.join(inbox.ready_dir, "not-a-casa-name.pdf"), "wb") as fh:
        fh.write(PDF)
    with caplog.at_level(logging.WARNING, logger="agent_inbox"):
        stats = inbox.sweep()
    assert stats["foreign"] == 4
    assert _ready(inbox) == [stored]
    assert outside.read_text() == "vault token"      # the link's target is untouched
    assert sum("not a Casa-written" in r.message for r in caplog.records) == 4


async def test_retention_is_seven_days_from_the_name(inbox):
    stored = (await inbox.publish(PDF, ".pdf", "x.pdf")).name
    published = int(stored.split("-")[0]) / 1000.0
    assert inbox.sweep(now=published + ai.RETENTION_S - 3600)["expired"] == 0
    assert _ready(inbox) == [stored]
    assert inbox.sweep(now=published + ai.RETENTION_S + 3600)["expired"] == 1
    assert _ready(inbox) == []
    assert os.listdir(inbox.meta_dir) == []


async def test_a_touched_mtime_does_not_extend_retention(inbox):
    stored = (await inbox.publish(PDF, ".pdf", "x.pdf")).name
    path = os.path.join(inbox.ready_dir, stored)
    future = time.time() + 30 * 86400
    os.utime(path, (future, future))
    published = int(stored.split("-")[0]) / 1000.0
    assert inbox.sweep(now=published + ai.RETENTION_S + 1)["expired"] == 1


async def test_the_sweep_never_removes_a_file_for_capacity(inbox, monkeypatch):
    monkeypatch.setattr(ai, "MAX_FILES", 1)
    stored = (await inbox.publish(PDF, ".pdf", "x.pdf")).name
    inbox.sweep()
    assert _ready(inbox) == [stored]


def test_boot_reclaims_staging(inbox):
    for name in (".part-dead", ".part-also-dead"):
        with open(os.path.join(inbox.staging_dir, name), "wb") as fh:
            fh.write(b"half a file")
    assert inbox.reclaim_staging() == 2
    assert _staging(inbox) == []


# ---------------------------------------------------------------------------
# Listing and the read grant
# ---------------------------------------------------------------------------


async def test_listing_is_newest_first_and_shows_only_casa_files(inbox):
    first = (await inbox.publish(PDF, ".pdf", "old.pdf")).name
    await asyncio.sleep(0.002)
    second = (await inbox.publish(TEXT, ".csv", "new.csv")).name
    os.symlink("/etc/hostname", os.path.join(inbox.ready_dir, "1700000000000-dddddddddddddddd.pdf"))
    names = [f.name for f in inbox.list_files()]
    assert names == [second, first]


async def test_only_the_wired_role_gets_a_read_grant(tmp_path):
    class _Sched:
        def add_job(self, *a, **k):
            self.job = k.get("id")
    sched = _Sched()
    await ai.wire(sched, str(tmp_path), role="assistant")
    assert ai.readable_prefixes("assistant") == (
        os.path.join(str(tmp_path), "assistant", "ready"),)
    assert ai.readable_prefixes("butler") == ()
    assert ai.readable_prefixes("concierge") == ()
    assert sched.job == "agent_inbox_sweep"


async def test_an_unprovisionable_inbox_grants_nothing(tmp_path):
    blocker = tmp_path / "assistant"
    blocker.write_text("a file where the directory should be")

    class _Sched:
        def add_job(self, *a, **k):
            raise AssertionError("must not register a sweep for a failed inbox")
    await ai.wire(_Sched(), str(tmp_path), role="assistant")
    assert ai.readable_prefixes("assistant") == ()
    assert ai.get_inbox("assistant") is None


@pytest.mark.parametrize("step", ["reclaim_staging", "sweep"])
async def test_a_failing_boot_step_leaves_no_inbox(tmp_path, monkeypatch, step):
    """Any step of provisioning failing leaves boot running and the role with no
    inbox — and so no read grant — never a half-installed one."""
    def boom(self, *a, **k):
        raise OSError(5, "EIO")
    monkeypatch.setattr(ai.Inbox, step, boom)
    closed = []
    real_close = ai.Inbox.close
    monkeypatch.setattr(ai.Inbox, "close", lambda self: (closed.append(1), real_close(self)))

    class _Sched:
        def add_job(self, *a, **k):
            raise AssertionError("no sweep for a failed inbox")
    await ai.wire(_Sched(), str(tmp_path), role="assistant")
    assert ai.get_inbox("assistant") is None
    assert ai.readable_prefixes("assistant") == ()
    assert closed == [1]


async def test_a_failing_sweep_registration_leaves_no_inbox(tmp_path):
    class _Sched:
        def add_job(self, *a, **k):
            raise RuntimeError("scheduler down")
    await ai.wire(_Sched(), str(tmp_path), role="assistant")
    assert ai.get_inbox("assistant") is None
    assert ai.readable_prefixes("assistant") == ()


@pytest.mark.parametrize("fail_at", [2, 3])
def test_a_provisioning_failure_partway_leaks_no_descriptor(tmp_path, monkeypatch, fail_at):
    """Provisioning opens staging/, ready/ and meta/ in turn. Failing the second
    or third open must close the ones already opened."""
    real_open = os.open
    opened: list[int] = []
    calls = 0

    def counting_open(path, flags, *a, **k):
        nonlocal calls
        if flags & os.O_DIRECTORY:
            calls += 1
            if calls == fail_at:
                raise OSError(24, "EMFILE")
            fd = real_open(path, flags, *a, **k)
            opened.append(fd)
            return fd
        return real_open(path, flags, *a, **k)
    monkeypatch.setattr(ai.os, "open", counting_open)
    with pytest.raises(OSError):
        ai.open_inbox("assistant", str(tmp_path))
    monkeypatch.undo()
    assert len(opened) == fail_at - 1
    for fd in opened:
        with pytest.raises(OSError):
            os.fstat(fd)          # closed


def test_a_symlinked_inbox_directory_is_refused(tmp_path):
    real = tmp_path / "elsewhere"
    real.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    os.symlink(real, root / "assistant")
    with pytest.raises(RuntimeError):
        ai.open_inbox("assistant", str(root))
