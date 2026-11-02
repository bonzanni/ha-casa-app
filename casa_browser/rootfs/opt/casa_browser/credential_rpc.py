"""Bounded private Unix RPC transport for the credential processes."""
import asyncio
import json
import os
import socket
import struct
from pathlib import Path

from casa_browser.broker import BrokerError, MAX_LINE


def encode(message):
    try:
        data = json.dumps(message, ensure_ascii=True, allow_nan=False).encode() + b'\n'
    except (TypeError, ValueError, RecursionError):
        raise BrokerError('unsupported') from None
    if len(data) > MAX_LINE:
        raise BrokerError('unsupported')
    return data


async def read(reader):
    try:
        line = await reader.readline()
        if not line.endswith(b'\n') or len(line) > MAX_LINE:
            raise BrokerError('unsupported')
        return json.loads(line)
    except (ValueError, UnicodeError, RecursionError):
        raise BrokerError('unsupported') from None


async def call(path, timeout, op, **args):
    data = encode({'op': op, **args})
    writer = None
    try:
        async with asyncio.timeout(timeout):
            reader, writer = await asyncio.open_unix_connection(str(path), limit=MAX_LINE)
            writer.write(data)
            await writer.drain()
            reply = await read(reader)
            if not isinstance(reply, dict):
                raise BrokerError('unsupported')
            if 'error' in reply:
                raise BrokerError(reply['error'])
            return reply
    except TimeoutError:
        raise BrokerError('timeout') from None
    except OSError:
        raise BrokerError('unavailable') from None
    except BrokerError:
        raise
    except Exception:
        raise BrokerError('internal') from None
    finally:
        if writer is not None:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), 1)
            except (OSError, TimeoutError):
                pass


class UnixServer:
    """One checked peer and one bounded request/reply per accepted connection."""
    async def start(self):
        self.path = Path(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o711)
        self.path.unlink(missing_ok=True)
        self._connections = set()
        self._server = await asyncio.start_unix_server(self.handle, path=str(self.path), limit=MAX_LINE)
        os.chmod(self.path, 0o660)

    async def close(self):
        self._server.close()
        tasks = tuple(self._connections)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self._server.wait_closed()
        self.path.unlink(missing_ok=True)

    async def serve(self, reader, writer, dispatch):
        task = asyncio.current_task()
        self._connections.add(task)
        try:
            sock = writer.get_extra_info('socket')
            credentials = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i'))
            if struct.unpack('3i', credentials)[1] != self.allowed_uid:
                reply = {'error': 'refused'}
            else:
                try:
                    async with asyncio.timeout(125):
                        request = await asyncio.wait_for(read(reader), 10)
                        reply = await dispatch(request)
                except BrokerError as error:
                    reply = {'error': error.code}
                except TimeoutError:
                    reply = {'error': 'timeout'}
                except Exception:
                    reply = {'error': 'internal'}
            try:
                data = encode(reply)
            except BrokerError:
                data = encode({'error': 'unsupported'})
            writer.write(data)
            await asyncio.wait_for(writer.drain(), 10)
        except (OSError, TimeoutError, BrokerError):
            pass
        finally:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), 1)
            except (OSError, TimeoutError):
                pass
            self._connections.discard(task)
