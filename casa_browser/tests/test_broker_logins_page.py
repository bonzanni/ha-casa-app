"""The saved-logins page as the broker's process serves it: on its own UNIX socket, beside
the broker's RPC socket, every request answered on the broker's event loop.

Each test runs `main.run` on a real broker with the app's own store and the driver double,
asks the page over its socket the way the front proxy does, and asks the broker over its
RPC socket the way the controller does.

What they protect: a login a person saves on the page is never half of an interleaved
write with one of the broker's own; the page is answered while a sign-in is under way; a
stop does not hang on a request already taken in; and a page that cannot start, or an
install that keeps its logins in 1Password, leaves the broker serving exactly as before.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import re
import socket
import threading
import time
from urllib.parse import urlencode

from brokerfakes import LOGIN, BuiltinRig, Rig, Step, canary, item_value
from casa_browser import vault_page, vault_page_server
from casa_browser.broker import BrokerError, main
from casa_browser.broker.main import BrokerServer
from casa_browser.credential_rpc import call as rpc_call

PREFIX = '/api/hassio_ingress/abc'
# What the front proxy adds to a request that came through Home Assistant.
FORWARDED = {'X-Ingress-Path': PREFIX, 'X-Forwarded-Proto': 'https',
             'X-Forwarded-Host': 'ha.example', 'Origin': 'https://ha.example'}
LOGGED_IN = {'status': 'logged_in', 'reason': 'completed'}


def exchange(socket_path, path, form=None):
    """One request to the page, a POST of `form` when one is given, and its whole answer:
    the status and the body's text. Blocks until the page has answered."""
    body = urlencode(form).encode('utf-8') if form is not None else b''
    lines = [f"{'GET' if form is None else 'POST'} {path} HTTP/1.0",
             *(f'{name}: {value}' for name, value in FORWARDED.items())]
    if form is not None:
        lines += ['Content-Type: application/x-www-form-urlencoded',
                  f'Content-Length: {len(body)}']
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(30)
        connection.connect(str(socket_path))
        connection.sendall('\r\n'.join(lines).encode('ascii') + b'\r\n\r\n' + body)
        answer = b''
        while chunk := connection.recv(65536):
            answer += chunk
    head, _, body = answer.partition(b'\r\n\r\n')
    return int(head.split()[1]), body.decode('utf-8')


def hidden_inputs(body, action):
    """The hidden inputs (code, stamp) of the form on the page that posts to `action`."""
    [form] = re.findall(r'<form[^>]*action="' + re.escape(f'{PREFIX}/logins{action}')
                        + r'"[^>]*>(.*?)</form>', body, re.DOTALL)
    return dict(re.findall(r'<input type="hidden" name="([a-z0-9]+)" value="([^"]*)">', form))


async def rpc(server, op, **args):
    return await rpc_call(server.path, 30, op, **args)


@contextlib.asynccontextmanager
async def running(r, *, directory=True):
    """`main.run` on the rig's broker, until the block is left: its RPC server, the path of
    the page's socket and the task. The socket's directory is made by the service's start
    script, not by the broker, so here it is made by the test, or with `directory` False
    left missing."""
    page_socket = r.root / 'logins' / 'page.sock'
    if directory:
        page_socket.parent.mkdir()
    server = BrokerServer(r.broker, r.root / 'rpc.sock', allowed_uid=os.getuid())
    task = asyncio.create_task(main.run(r.broker, server, page_socket))
    try:
        async with asyncio.timeout(10):
            while not (server.path.exists() or task.done()):
                await asyncio.sleep(0)
        if task.done():
            task.result()
        # `run` has started everything it starts by the time its RPC answers.
        await rpc(server, 'status')
        yield server, page_socket, task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def linked_site(r, server):
    """A login in the store and a site linked to it, in a run that has begun."""
    login = await r.vault.create_login('Synthetic login', canary(), canary(), LOGIN)
    await rpc(server, 'register_item', site_id='site', item_id=login.id,
              login_origins=list(LOGIN), operator_ref='operator')
    await rpc(server, 'begin_run', run_id='run', account_site_ids=['site'])
    return login


async def test_a_page_request_is_answered_while_a_sign_in_waits_on_the_driver(tmp_path):
    r = BuiltinRig(tmp_path)
    async with running(r) as (server, page_socket, _):
        login = await linked_site(r, server)
        r.driver.steps = [Step(('username', 'password')), Step()]
        parked, release, submit = asyncio.Event(), asyncio.Event(), r.driver.submit

        async def held(**args):
            parked.set()
            await release.wait()
            return await submit(**args)
        r.driver.submit = held
        signing_in = asyncio.create_task(
            rpc(server, 'login', run_id='run', site_id='site', driver_session='driver'))
        await asyncio.wait_for(parked.wait(), 10)

        status, body = await asyncio.to_thread(exchange, page_socket, '/')
        assert status == 200 and 'Synthetic login' in body
        status, body = await asyncio.to_thread(exchange, page_socket, f'/s/{login.id}')
        assert status == 200
        assert not signing_in.done()
        release.set()
        assert await signing_in == LOGGED_IN


async def test_a_page_write_and_a_broker_write_never_interleave(tmp_path):
    """The broker checks a login's version and then writes; the page checks its form's stamp
    and then writes. Each pair must run with nothing of the other between its two halves.

    Here the broker's write, from the version the page's form was made at, is started at the
    very moment the page has made its check and is about to write. On the broker's loop that
    write cannot run until the page's is done, and it then finds the newer version. Were the
    page writing from its connection's thread, the loop would be free at that moment, the
    broker's write would go through in between, and both would report success with one of
    the two lost."""
    r = BuiltinRig(tmp_path)
    async with running(r) as (_, page_socket, _task):
        stored, brokers = canary(), canary()
        login = await r.vault.create_login('Synthetic login', 'me', stored, LOGIN)
        edit = f'/s/{login.id}/edit'
        status, body = await asyncio.to_thread(exchange, page_socket, edit)
        assert status == 200
        form = hidden_inputs(body, edit)

        async def broker_write():
            try:
                await r.vault.update_fields(login.id, 1, password=brokers)
            except BrokerError as error:
                return error.code
            return 'saved'

        loop, loop_thread = asyncio.get_running_loop(), threading.current_thread()
        store, started = r.vault.store, []
        replace = store.replace

        def page_about_to_write(ref, **given):
            if given['writer'] == vault_page.WRITER:
                write = asyncio.run_coroutine_threadsafe(broker_write(), loop)
                started.append(write)
                if threading.current_thread() is not loop_thread:
                    write.result(30)    # the loop is free: the broker's write ends first
            return replace(ref, **given)
        store.replace = page_about_to_write

        status, _ = await asyncio.to_thread(exchange, page_socket, edit,
                                            {**form, 'value0': 'you'})
        [write] = started
        outcome = await asyncio.wrap_future(write)
        assert (status, outcome) == (303, 'conflict')
        saved = await r.vault.get(login.id)
        assert saved.version == 2
        assert [(role, item_value(saved, role)) for role in ('username', 'password')] == [
            ('username', 'you'), ('password', stored)]


def test_stopping_with_a_request_admitted_ends(tmp_path, monkeypatch):
    """`run` is cancelled while the page has taken a request in that the loop has not
    answered yet. Closing the page waits for that request, and the request waits for the
    loop: `run` has to leave the loop free while it closes, or neither ever ends.

    The scenario has a loop and a thread of its own, so that a stop that never ends fails
    this test instead of holding the whole run."""
    admitted = threading.Event()
    answering = vault_page_server._PageServer.answering

    @contextlib.contextmanager
    def announced(server):
        with answering(server):
            admitted.set()
            yield
    monkeypatch.setattr(vault_page_server._PageServer, 'answering', announced)
    seen = {}

    async def scenario():
        r = BuiltinRig(tmp_path)
        async with running(r) as (_, page_socket, task):
            await r.vault.create_login('Synthetic login', canary(), canary(), LOGIN)
            answers = []
            client = threading.Thread(
                target=lambda: answers.append(exchange(page_socket, '/')), daemon=True)
            client.start()
            # Waiting without giving the loop away: the request is taken in, and cannot
            # be answered before `run` is cancelled.
            assert admitted.wait(30)
            began = time.monotonic()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            seen['took'] = time.monotonic() - began
            client.join(30)
            seen['answers'] = answers

    def body():
        try:
            asyncio.run(scenario())
        except BaseException as error:  # noqa: BLE001 - shown by the test's own thread
            seen['error'] = error
    thread = threading.Thread(target=body, daemon=True)
    thread.start()
    thread.join(30)
    assert not thread.is_alive(), 'the stop never ended'
    if 'error' in seen:
        raise seen['error']
    assert seen['took'] < 2
    [(status, text)] = seen['answers']
    assert status == 200 and 'Synthetic login' in text


async def test_a_page_that_cannot_start_leaves_the_broker_serving(tmp_path, capsys):
    r = BuiltinRig(tmp_path)
    capsys.readouterr()
    async with running(r, directory=False) as (server, page_socket, task):
        assert not task.done()
        printed = capsys.readouterr()
        assert printed.err == main.PAGE_FAILED + '\n' and printed.out == ''
        assert not page_socket.exists()
        # The broker goes on as if there were no page: it links a site and signs in on it.
        await linked_site(r, server)
        assert (await rpc(server, 'status'))['available'] is True
        r.driver.steps = [Step(('username', 'password')), Step()]
        assert await rpc(server, 'login', run_id='run', site_id='site',
                         driver_session='driver') == LOGGED_IN
    assert capsys.readouterr().err == ''


async def test_with_1password_no_page_is_served(tmp_path, capsys):
    """An install with a 1Password token has no saved-logins page: nothing listens where
    the page would, though the directory is there, and the broker says nothing about it."""
    r = Rig(tmp_path)
    capsys.readouterr()
    async with running(r) as (server, page_socket, task):
        assert not task.done()
        assert list(page_socket.parent.iterdir()) == []
        reply = await rpc(server, 'status')
        assert reply['sdk'] == 'ok' and 'links' not in reply and 'store' not in reply
    assert capsys.readouterr().err == ''
