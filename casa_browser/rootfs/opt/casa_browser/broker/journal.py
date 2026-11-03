"""Durable credential-operation metadata interface; values are never accepted."""
from dataclasses import dataclass
from pathlib import Path
import os
import sqlite3
from datetime import datetime, timezone

from casa_browser.broker import BrokerError

SAVE_STATES = ('intent', 'attempt', 'vault_written', 'vault_failed', 'vault_unknown')
CAPTURE_STATES = ('capture_pending', 'capture_saved', 'capture_declined', 'capture_warned')

@dataclass
class Journal:
    path: Path

    def __post_init__(self):
        self.path = Path(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Create privately before SQLite opens it, including under a permissive umask.
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        os.chmod(self.path, 0o600)
        # SQLite also creates transaction/WAL/shared-memory files at connection
        # setup. Restore the process umask before returning to asynchronous work.
        previous_umask = os.umask(0o077)
        try:
            self._db = sqlite3.connect(self.path)
            self._db.row_factory = sqlite3.Row
            self._db.execute('PRAGMA synchronous=FULL')
            self._db.executescript('''
                CREATE TABLE IF NOT EXISTS operations (
                    op_id TEXT PRIMARY KEY, kind TEXT NOT NULL, site_id TEXT NOT NULL,
                    run_id TEXT NOT NULL, capture_id TEXT, item_id TEXT,
                    item_version_before INTEGER, item_version INTEGER,
                    state TEXT NOT NULL, closed INTEGER NOT NULL DEFAULT 0, consent_id TEXT);
                CREATE TABLE IF NOT EXISTS captures (
                    capture_id TEXT PRIMARY KEY, run_id TEXT NOT NULL,
                    site_id TEXT NOT NULL, state TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS notices (
                    notice_key INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_key TEXT NOT NULL UNIQUE, kind TEXT NOT NULL,
                    site_id TEXT NOT NULL, run_id TEXT NOT NULL,
                    text_masked TEXT NOT NULL, at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS controls (
                    name TEXT PRIMARY KEY, operator_ref TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS operator_actions (
                    seq INTEGER PRIMARY KEY, action TEXT NOT NULL,
                    site_id TEXT NOT NULL, operator_ref TEXT NOT NULL);
            ''')
        finally:
            os.umask(previous_umask)

    def begin(self, op_id, kind, site, run, capture_id=None, item_id=None,
              item_version_before=None, *, consent_id=None):
        if kind not in ('live', 'manual'):
            raise BrokerError('unsupported')
        with self._db:
            self._db.execute('''INSERT INTO operations
                (op_id, kind, site_id, run_id, capture_id, item_id,
                 item_version_before, state, consent_id) VALUES (?,?,?,?,?,?,?,'intent',?)''',
                (op_id, kind, site, run, capture_id, item_id, item_version_before, consent_id))

    def _notice(self, row, kind, unsaved='password'):
        """A warning, worded by what was left unsaved: ``username`` when only a username was
        (casa-test F11), else a password — also when nothing is known. A save issues none: every
        save is the automatic one, which the agent's own reply tells (slice 5)."""
        source = ('capture:' + row['capture_id'] if row['capture_id'] else 'save:' + row['op_id']) + ':' + kind
        if unsaved == 'username':
            text = 'a username you typed on {display_name} was not saved to the vault'
        else:
            text = ('a password you changed on {display_name} may not be in the vault — '
                    'enter it again in a login live view')
        self._db.execute('''INSERT OR IGNORE INTO notices
            (source_key, kind, site_id, run_id, text_masked, at) VALUES (?,?,?,?,?,?)''',
            (source, kind, row['site_id'], row['run_id'], text,
             datetime.now(timezone.utc).isoformat()))

    def transition(self, op_id, state, item_version=None, *, capture_resolved=True):
        allowed = {'intent': ('attempt',),
                   'attempt': ('vault_written', 'vault_failed', 'vault_unknown'),
                   'vault_unknown': ('vault_written',)}
        with self._db:
            row = self._db.execute('SELECT * FROM operations WHERE op_id=?', (op_id,)).fetchone()
            if row is None:
                raise BrokerError('not_found')
            if row['closed'] or state not in allowed.get(row['state'], ()):
                raise BrokerError('conflict')
            if state == 'vault_written' and type(item_version) is not int:
                raise BrokerError('unsupported')
            self._db.execute('UPDATE operations SET state=?, item_version=? WHERE op_id=?',
                             (state, item_version, op_id))
            if state == 'vault_written':
                self._db.execute('UPDATE operations SET closed=1 WHERE op_id=?', (op_id,))
                if capture_resolved:
                    changed = self._db.execute("""UPDATE captures SET state='capture_saved'
                        WHERE capture_id=? AND state='capture_pending'""",
                        (row['capture_id'],))
                    if changed.rowcount == 1:
                        # All offered roles now have a confirmed save; earlier
                        # failed or uncertain attempts no longer need recovery.
                        self._db.execute('UPDATE operations SET closed=1 WHERE capture_id=?',
                                         (row['capture_id'],))
                    return changed.rowcount == 1
            return False

    def write_failed(self, op_id, error):
        """Record a fixed-code refusal as definite; other failures are uncertain."""
        state = 'vault_failed' if error.code in ('refused', 'conflict') else 'vault_unknown'
        self.transition(op_id, state)

    def capture_pending(self, capture_id, run, site):
        with self._db:
            self._db.execute('INSERT INTO captures VALUES (?,?,?,?)',
                             (capture_id, run, site, 'capture_pending'))

    def capture_reopen(self, capture_id):
        """A manual save may follow a review with no changed fields."""
        with self._db:
            self._db.execute("UPDATE captures SET state='capture_pending' "
                             "WHERE capture_id=? AND state='capture_declined'", (capture_id,))

    def capture_finish(self, capture_id, state, unsaved='password'):
        if state not in ('capture_declined', 'capture_warned'):
            raise BrokerError('unsupported')
        with self._db:
            row = self._db.execute('SELECT * FROM captures WHERE capture_id=?', (capture_id,)).fetchone()
            if row is None:
                raise BrokerError('not_found')
            if row['state'] != 'capture_pending':
                return
            attempted = self._db.execute('''SELECT 1 FROM operations WHERE capture_id=?
                AND state != 'intent' LIMIT 1''', (capture_id,)).fetchone()
            if attempted:
                state = 'capture_warned'
            self._db.execute('UPDATE captures SET state=? WHERE capture_id=?', (state, capture_id))
            if state == 'capture_warned':
                self._notice(row, 'credential_unknown', unsaved)
            self._db.execute('UPDATE operations SET closed=1 WHERE capture_id=?', (capture_id,))

    def recover(self):
        with self._db:
            for row in self._db.execute('SELECT * FROM operations WHERE closed=0').fetchall():
                if row['state'] != 'vault_written':
                    self._notice(row, 'credential_unknown')
                self._db.execute('UPDATE operations SET closed=1 WHERE op_id=?', (row['op_id'],))
            for row in self._db.execute("SELECT * FROM captures WHERE state='capture_pending'").fetchall():
                self._notice(row, 'credential_unknown')
                self._db.execute("UPDATE captures SET state='capture_warned' WHERE capture_id=?",
                                 (row['capture_id'],))

    def rows(self):
        return [dict(row) for row in self._db.execute('SELECT * FROM operations')] + [
            dict(row) for row in self._db.execute('SELECT * FROM captures')]

    def pending_notices(self, after=0):
        return [dict(row) for row in self._db.execute('''SELECT notice_key, kind, site_id,
            run_id, text_masked, at FROM notices WHERE notice_key>? ORDER BY notice_key''', (after,))]

    def record_control(self, action, site_id, operator_ref):
        with self._db:
            self._db.execute('INSERT INTO operator_actions (action, site_id, operator_ref) VALUES (?,?,?)',
                             (action, site_id, operator_ref))

    def close(self):
        self._db.close()
