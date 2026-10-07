"""Private, content-free receipts. Claim precedes any work; no automatic replay."""
import fcntl
import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager


def route_key(home, bot, connection, chat):
    return hashlib.sha256(json.dumps([str(home.resolve()), bot, connection, chat],
                                    separators=(",", ":")).encode()).hexdigest()


class State:
    def __init__(self, directory):
        self.directory = directory
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        path = directory / "receipts.sqlite3"
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        os.chmod(path, 0o600)
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS receipts (
                    route TEXT, message INTEGER, status TEXT NOT NULL,
                    PRIMARY KEY(route, message));
                CREATE TABLE IF NOT EXISTS routes (
                    route TEXT PRIMARY KEY, bot INTEGER NOT NULL,
                    connection TEXT NOT NULL, chat INTEGER NOT NULL,
                    owner INTEGER NOT NULL, peer_label TEXT NOT NULL DEFAULT '');
                CREATE TABLE IF NOT EXISTS connections (
                    bot INTEGER, id TEXT, fingerprint TEXT, revision INTEGER NOT NULL,
                    PRIMARY KEY(bot, id));
                CREATE TABLE IF NOT EXISTS story_effects (
                    identity TEXT PRIMARY KEY, origin TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS stories (
                    bot INTEGER NOT NULL, connection TEXT NOT NULL, owner INTEGER NOT NULL,
                    story INTEGER NOT NULL, media TEXT NOT NULL, kind TEXT NOT NULL,
                    caption TEXT NOT NULL DEFAULT '',
                    deleted INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(bot, connection, owner, story));
            ''')
            if 'caption' not in {row[1] for row in db.execute('PRAGMA table_info(stories)')}:
                db.execute("ALTER TABLE stories ADD COLUMN caption TEXT NOT NULL DEFAULT ''")

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.directory / "receipts.sqlite3", timeout=5)
        try:
            with db:
                yield db
        finally:
            db.close()

    def remember_route(self, route, bot, connection, chat, owner, peer_label=''):
        """Persist authoritative routing, never permissions or replay authority."""
        if (any(type(v) is not int or v <= 0 for v in (bot, chat, owner))
                or not isinstance(connection, str) or not connection
                or route != route_key(self.directory.parent, bot, connection, chat)):
            raise ValueError('Invalid route identity')
        label = peer_label[:200] if isinstance(peer_label, str) else ''
        with self.db() as db:
            db.execute('INSERT OR IGNORE INTO routes VALUES (?, ?, ?, ?, ?, ?)',
                       (route, bot, connection, chat, owner, label))
            row = db.execute('SELECT bot, connection, chat, owner FROM routes WHERE route=?',
                             (route,)).fetchone()
            if row != (bot, connection, chat, owner):
                raise ValueError('Conflicting route identity')
            db.execute('UPDATE routes SET peer_label=? WHERE route=?', (label, route))

    def lookup_route(self, route, bot, owner):
        """Read for owner diagnostics; callers must freshly authorize any send."""
        with self.db() as db:
            row = db.execute('SELECT route, bot, connection, chat, owner, peer_label '
                             'FROM routes WHERE route=? AND bot=? AND owner=?',
                             (route, bot, owner)).fetchone()
        if row is None or row[0] != route_key(self.directory.parent, row[1], row[2], row[3]):
            return None
        return dict(zip(('route', 'bot', 'connection', 'chat', 'owner', 'peer_label'), row))

    def claim(self, route, message):
        with self.db() as db:
            return db.execute("INSERT OR IGNORE INTO receipts VALUES (?, ?, 'claimed')",
                              (route, message)).rowcount == 1

    def finish(self, route, message, status):
        with self.db() as db:
            db.execute("UPDATE receipts SET status=? WHERE route=? AND message=?",
                       (status, route, message))

    def revision(self, bot, cid):
        with self.db() as db:
            row = db.execute("SELECT revision FROM connections WHERE bot=? AND id=?", (bot, cid)).fetchone()
            return row[0] if row else 0

    def story_candidates(self, bot, owner):
        with self.db() as db:
            rows = db.execute("SELECT id, fingerprint FROM connections WHERE bot=?", (bot,)).fetchall()
        found = []
        for cid, fingerprint in rows:
            try:
                if json.loads(fingerprint)[0] == owner:
                    found.append(cid)
            except (ValueError, TypeError, IndexError):
                pass
        return found

    def claim_story_effect(self, identity, origin):
        with self.db() as db:
            return db.execute("INSERT OR IGNORE INTO story_effects VALUES (?, ?, 'claimed')",
                              (identity, origin)).rowcount == 1

    def finish_story_effect(self, identity, status):
        with self.db() as db:
            db.execute("UPDATE story_effects SET status=? WHERE identity=?", (status, identity))

    def save_story(self, bot, cid, owner, story, media, kind, caption=''):
        with self.db() as db:
            db.execute("INSERT INTO stories (bot,connection,owner,story,media,kind,caption,deleted) VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
                       (bot, cid, owner, story, str(media), kind, caption))

    def own_story(self, bot, cid, owner, story):
        with self.db() as db:
            return db.execute("SELECT media, kind, caption FROM stories WHERE bot=? AND connection=? AND owner=? AND story=? AND deleted=0",
                              (bot, cid, owner, story)).fetchone()

    def list_stories(self, bot, cid, owner):
        with self.db() as db:
            return [r[0] for r in db.execute("SELECT story FROM stories WHERE bot=? AND connection=? AND owner=? AND deleted=0 ORDER BY story DESC",
                                             (bot, cid, owner)).fetchall()]

    def delete_story_record(self, bot, cid, owner, story):
        with self.db() as db:
            db.execute("UPDATE stories SET deleted=1 WHERE bot=? AND connection=? AND owner=? AND story=?",
                       (bot, cid, owner, story))

    def update_story_media(self, bot, cid, owner, story, media, kind, caption=''):
        with self.db() as db:
            db.execute("UPDATE stories SET media=?, kind=?, caption=? WHERE bot=? AND connection=? AND owner=? AND story=? AND deleted=0",
                       (str(media), kind, caption, bot, cid, owner, story))

    def connection(self, bot, conn):
        rights = getattr(conn, "rights", None)
        enabled = conn.is_enabled is True
        reply = (getattr(rights, "can_reply", None) if rights is not None
                 else getattr(conn, "can_reply", None)) is True
        # Keep the existing Business handler's revision format compatible during hot-load.
        # Story permission is freshly checked by the story service before every mutation.
        fingerprint = json.dumps([conn.user.id, enabled, reply])
        with self.db() as db:
            db.execute('''INSERT INTO connections VALUES (?, ?, ?, 1)
                ON CONFLICT(bot, id) DO UPDATE SET fingerprint=excluded.fingerprint,
                revision=revision + (fingerprint != excluded.fingerprint)''',
                       (bot, conn.id, fingerprint))
        return self.revision(bot, conn.id)

    @contextmanager
    def route_lock(self, route):
        fd = os.open(self.directory / (route + ".lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        acquired = False
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except BlockingIOError:
                pass
            yield acquired
        finally:
            os.close(fd)
