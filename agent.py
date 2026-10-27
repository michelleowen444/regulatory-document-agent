"""CLI + dedicated IMAP mailbox worker. No email is sent without --send."""
import argparse
import email
import hashlib
import imaplib
import json
import logging
import os
import smtplib
import sqlite3
import ssl
import time
from datetime import datetime, timezone
from email import policy
from email.utils import parseaddr
from html import unescape
from pathlib import Path
import re
from core import TYPES, parse_request, package, summary, compose

LOG = logging.getLogger('agent')
SUBJECT_PREFIX = '[Regulatory Agent]'

def subject_prefix():
    return os.getenv('SUBJECT_PREFIX', SUBJECT_PREFIX).strip()

def is_agent_subject(subject):
    return str(subject).strip().casefold().startswith(subject_prefix().casefold())

def load_env(path=Path('.env')):
    if path.exists():
        for line in path.read_text(encoding='utf-8-sig').splitlines():
            line = line.strip()
            if line and not line.startswith('#'):
                key, value = line.split('=', 1)
                os.environ.setdefault(key.strip(), value.strip().strip('\"\''))

def required(key):
    value = os.getenv(key, '')
    if not value:
        raise ValueError(f'Missing {key}; configure .env first.')
    return value

def fixture(request, folder):
    files = folder / 'files'
    files.mkdir(parents=True, exist_ok=True)
    result = dict(matter=request.matter, category=request.category, fixture=True,
        source='synthetic offline fixture', metadata={'Title / Description': 'Synthetic demonstration matter'},
        counts={x: 12 if x == request.category else 0 for x in TYPES}, documents=[], errors=[])
    for i in range(1, 11):
        name = f'demo-{i:02}.txt'
        (files / name).write_text(f'SYNTHETIC DEMO DOCUMENT {i}\nNot an actual regulatory filing.\n', encoding='utf-8')
        result['documents'].append(dict(id=f'DEMO{i}', title=f'Synthetic example {i}', date='', filename=name))
    return result

def create_reply(text, recipient, folder, demo=False, ai=False, headed=False, reply_id=''):
    folder.mkdir(parents=True, exist_ok=True)
    sender = os.getenv('AGENT_EMAIL', 'agent@example.invalid')
    try:
        request = parse_request(text, use_ai=ai)
    except ValueError as exc:
        msg = compose(sender, recipient, 'Please clarify your document request', str(exc), in_reply_to=reply_id)
    else:
        if demo:
            result = fixture(request, folder)
        else:
            from browser import collect
            result = collect(request, folder, headed)
        archive = package(result, folder)
        body = summary(result)
        msg = compose(sender, recipient, f'{request.matter}: {request.category}', body, archive, reply_id)
        # Check the encoded MIME message, not only ZIP size (base64 adds overhead).
        if len(msg.as_bytes()) > int(os.getenv('MAX_MESSAGE_BYTES', '20000000')):
            body = body.replace('The ZIP contains the downloaded files and a source/hash manifest.',
                'The ZIP was created but exceeds the configured email size limit. No attachment is included. '
                'Please request a smaller document category; the operator can retrieve the saved files.')
            msg = compose(sender, recipient, f'{request.matter}: attachment too large', body, in_reply_to=reply_id)
    (folder / 'reply.eml').write_bytes(msg.as_bytes(policy=policy.SMTP))
    (folder / 'reply.txt').write_text(msg.get_body(preferencelist=('plain',)).get_content(), encoding='utf-8')
    return msg

def send_message(msg):
    host, user, password = required('SMTP_HOST'), required('MAIL_USERNAME'), required('MAIL_PASSWORD')
    port = int(os.getenv('SMTP_PORT', '465'))
    context = ssl.create_default_context()
    if port == 465:
        connection = smtplib.SMTP_SSL(host, port, context=context, timeout=45)
    else:
        connection = smtplib.SMTP(host, port, timeout=45)
        connection.starttls(context=context)
    with connection as smtp:
        smtp.login(user, password)
        smtp.send_message(msg)

def message_text(msg):
    body = msg.get_body(preferencelist=('plain', 'html'))
    content = body.get_content() if body else ''
    if body and body.get_content_type() == 'text/html':
        content = unescape(re.sub('<[^>]+>', ' ', content))
    # Avoid quoted threads introducing a second matter number.
    content = re.split(r'(?m)^On .+wrote:\s*$|^-{2,}\s*Original Message\s*-{2,}', content)[0]
    content = '\n'.join(line for line in content.splitlines() if not line.lstrip().startswith('>'))
    return str(msg.get('Subject', '')) + '\n' + content

class Journal:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.execute('CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, status TEXT, folder TEXT)')
    def status(self, key):
        row = self.db.execute('SELECT status FROM jobs WHERE id=?', (key,)).fetchone()
        return row[0] if row else None
    def set(self, key, status, folder):
        self.db.execute('INSERT OR REPLACE INTO jobs VALUES (?,?,?)', (key, status, str(folder)))
        self.db.commit()

def poll(root, send, ai, headed=False):
    required('AGENT_EMAIL')
    journal = Journal(root / 'state.sqlite3')
    try:
        with imaplib.IMAP4_SSL(required('IMAP_HOST'), int(os.getenv('IMAP_PORT', '993')), timeout=45) as inbox:
            inbox.login(required('MAIL_USERNAME'), required('MAIL_PASSWORD'))
            status, _ = inbox.select('INBOX')
            if status != 'OK':
                raise RuntimeError('Cannot select INBOX')
            criteria = ['UNSEEN']
            if subject_prefix():
                quoted = subject_prefix().replace('\\', '\\\\').replace('"', '\"')
                criteria += ['SUBJECT', '"' + quoted + '"']
            status, data = inbox.uid('search', None, *criteria)
            if status != 'OK':
                raise RuntimeError('Cannot search INBOX')
            for uid in data[0].split()[:20]:
                # IMAP SUBJECT is a substring search. Check the exact prefix using
                # headers before retrieving a body or creating any reply.
                status, headers = inbox.uid('fetch', uid, '(BODY.PEEK[HEADER.FIELDS (SUBJECT)])')
                if status != 'OK':
                    continue
                header_raw = next((part[1] for part in headers if isinstance(part, tuple)), b'')
                header = email.message_from_bytes(header_raw, policy=policy.default)
                if not is_agent_subject(header.get('Subject', '')):
                    continue
                status, data = inbox.uid('fetch', uid, '(BODY.PEEK[])')
                if status != 'OK':
                    continue
                raw = next((part[1] for part in data if isinstance(part, tuple)), b'')
                if not raw or len(raw) > 2000000:
                    continue
                msg = email.message_from_bytes(raw, policy=policy.default)
                if not is_agent_subject(msg.get('Subject', '')):
                    continue
                sender = parseaddr(str(msg.get('From', '')))[1]
                if not sender or sender.lower() == required('AGENT_EMAIL').lower():
                    continue
                if str(msg.get('Auto-Submitted', 'no')).lower() != 'no' or msg.get('List-Id'):
                    continue
                allow = {x.strip().lower() for x in os.getenv('ALLOWED_SENDERS', '').split(',') if x.strip()}
                if allow and sender.lower() not in allow:
                    continue
                # Message-ID + sender deduplicates redeliveries. Hash raw if no ID.
                identity = sender.lower() + '\n' + str(msg.get('Message-ID', hashlib.sha256(raw).hexdigest()))
                key = hashlib.sha256(identity.encode()).hexdigest()
                state = journal.status(key)
                if state in ('sent', 'sending', 'failed') or (state == 'preview' and not send):
                    continue
                folder = root / key[:16]
                LOG.info('Processing job %s', key[:16])
                try:
                    if state == 'preview' and (folder / 'reply.eml').exists():
                        reply = email.message_from_bytes((folder / 'reply.eml').read_bytes(), policy=policy.default)
                    else:
                        reply = create_reply(message_text(msg), sender, folder, ai=ai, headed=headed,
                                             reply_id=str(msg.get('Message-ID', '')))
                    if send:
                        # SMTP has no exactly-once guarantee: never retry an uncertain send automatically.
                        journal.set(key, 'sending', folder)
                        send_message(reply)
                        journal.set(key, 'sent', folder)
                        inbox.uid('store', uid, '+FLAGS', '(\\Seen)')
                    else:
                        journal.set(key, 'preview', folder)
                    LOG.info('Job %s: %s', key[:16], journal.status(key))
                except Exception:
                    if journal.status(key) != 'sending':
                        journal.set(key, 'failed', folder)
                    LOG.exception('Job %s needs operator attention; no automatic resend', key[:16])
    finally:
        journal.db.close()

def main():
    load_env()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    run = sub.add_parser('run', help='Create local results; optionally email them')
    run.add_argument('--text', default='Please send Other Documents from M12205')
    run.add_argument('--to', default='reviewer@example.invalid')
    run.add_argument('--out', default='runs')
    run.add_argument('--demo', action='store_true', help='Synthetic offline data, clearly labelled')
    run.add_argument('--headed', action='store_true')
    run.add_argument('--ai', action='store_true')
    run.add_argument('--send', action='store_true')
    watch = sub.add_parser('watch', help='Poll a dedicated mailbox')
    watch.add_argument('--out', default='runs/mail')
    watch.add_argument('--send', action='store_true')
    watch.add_argument('--ai', action='store_true')
    watch.add_argument('--once', action='store_true')
    watch.add_argument('--headed', action='store_true', help='Show the browser while processing email')
    args = parser.parse_args()
    if args.command == 'run':
        if args.send and args.demo:
            parser.error('Demo fixture mode cannot send email.')
        if args.send:
            required('AGENT_EMAIL')
            if args.to.endswith('.invalid'):
                parser.error('Set --to to a real test recipient before using --send.')
        folder = Path(args.out) / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        msg = create_reply(args.text, args.to, folder, args.demo, args.ai, args.headed)
        if args.send:
            send_message(msg)
        print(msg.get_body(preferencelist=('plain',)).get_content())
        print(f'Artifacts: {folder.resolve()}')
        print('Email sent.' if args.send else 'Local preview only. No email sent.')
    else:
        root = Path(args.out)
        root.mkdir(parents=True, exist_ok=True)
        # A lock prevents two worker processes from racing over the same mailbox journal.
        lock = root / '.worker.lock'
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            parser.error('Worker lock exists. Stop the other worker, or remove stale lock after checking its PID.')
        try:
            with os.fdopen(fd, 'w') as handle:
                handle.write(str(os.getpid()))
            while True:
                try:
                    poll(root, args.send, args.ai, args.headed)
                except Exception:
                    LOG.exception('Mailbox poll failed')
                    if args.once:
                        raise
                if args.once:
                    break
                time.sleep(max(15, int(os.getenv('POLL_SECONDS', '30'))))
        finally:
            lock.unlink(missing_ok=True)

if __name__ == '__main__':
    main()
