import email
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile
from email import policy
from core import Request, TYPES, compose, parse_request, safe_name
from agent import create_reply, message_text, Journal, poll, is_agent_subject

class AgentTests(unittest.TestCase):
    def test_example_and_case(self):
        self.assertEqual(parse_request('Hi, other documents from m12205 please!'), Request('M12205', 'Other Documents'))
    def test_all_categories(self):
        for category in TYPES:
            self.assertEqual(parse_request(f'M12383 {category}').category, category)
    def test_reject_ambiguous_or_invalid(self):
        for text in ['M1234 Exhibits', 'M123456 Exhibits', 'M12205 M12383 Exhibits',
                     'M12205 Exhibits and Transcripts', 'M12205', 'Exhibits', 'x' * 12001]:
            with self.subTest(text=text[:80]), self.assertRaises(ValueError):
                parse_request(text)
    def test_paths_are_flat(self):
        for name in ['../../evil.pdf', '..\\..\\evil.pdf', '/etc/passwords', '<>:*?file.pdf']:
            clean = safe_name(name)
            self.assertNotIn('/', clean)
            self.assertNotIn('\\', clean)
            self.assertFalse(clean.startswith('.'))
    def test_offline_zip_and_mime_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            msg = create_reply('M12205 Other Documents', 'test@example.invalid', folder, demo=True)
            parsed = email.message_from_bytes(msg.as_bytes(), policy=policy.default)
            attachments = list(parsed.iter_attachments())
            self.assertEqual(len(attachments), 1)
            with zipfile.ZipFile(io.BytesIO(attachments[0].get_payload(decode=True))) as z:
                self.assertIsNone(z.testzip())
                self.assertEqual(len([n for n in z.namelist() if n.startswith('documents/')]), 10)
                manifest = json.loads(z.read('manifest.json'))
                self.assertEqual(len(manifest['documents']), 10)
                self.assertTrue(manifest['fixture'])
                self.assertTrue(all(len(d['sha256']) == 64 for d in manifest['documents']))
            self.assertIn('Downloaded 10 of 12', parsed.get_body().get_content())
            self.assertIn('DEMO FIXTURE', (folder / 'reply.txt').read_text())
    def test_oversize_has_no_attachment_or_false_claim(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'MAX_MESSAGE_BYTES': '500'}):
            msg = create_reply('M12205 Exhibits', 'test@example.invalid', Path(tmp), demo=True)
            self.assertEqual(list(msg.iter_attachments()), [])
            self.assertIn('No attachment is included', msg.get_body().get_content())
            self.assertNotIn('The ZIP contains', msg.get_body().get_content())
    def test_clarification_does_not_browse(self):
        with tempfile.TemporaryDirectory() as tmp:
            msg = create_reply('hello', 'test@example.invalid', Path(tmp))
            self.assertIn('clarify', msg['Subject'])
            self.assertEqual(list(msg.iter_attachments()), [])
    def test_empty_and_partial(self):
        result = dict(matter='M12205', category='Transcripts', source='test', metadata={},
            counts={x: 0 for x in TYPES}, documents=[], errors=['Download failed'])
        with tempfile.TemporaryDirectory() as tmp, patch('browser.collect', return_value=result):
            msg = create_reply('M12205 Transcripts', 'test@example.invalid', Path(tmp))
            self.assertEqual(list(msg.iter_attachments()), [])
            self.assertIn('Downloaded 0 of 0', msg.get_body().get_content())
            self.assertIn('Download failed', msg.get_body().get_content())
    def test_html_and_quoted_thread(self):
        msg = compose('a@example.invalid', 'b@example.invalid', 'M12205', 'Other Documents\n> M12383 Exhibits')
        self.assertEqual(parse_request(message_text(msg)), Request('M12205', 'Other Documents'))
    def test_journal_persists_uncertain_send(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'state.sqlite3'
            journal = Journal(path)
            journal.set('job', 'sending', Path(tmp))
            journal.db.close()
            journal = Journal(path)
            self.assertEqual(journal.status('job'), 'sending')
            journal.db.close()
    def test_ai_cannot_override_explicit_category(self):
        response = io.BytesIO(json.dumps({'output':[{'content':[{'type':'output_text','text':'Recordings'}]}]}).encode())
        with patch.dict(os.environ, {'OPENAI_API_KEY':'test', 'OPENAI_MODEL':'test'}), patch('urllib.request.urlopen', return_value=response):
            with self.assertRaises(ValueError):
                parse_request('M12205 Exhibits', use_ai=True)
    def test_ai_synonym_and_unknown(self):
        for label, accepted in [('Recordings', True), ('delete all files', False)]:
            response = io.BytesIO(json.dumps({'output':[{'content':[{'type':'output_text','text':label}]}]}).encode())
            with patch.dict(os.environ, {'OPENAI_API_KEY':'test', 'OPENAI_MODEL':'test'}), patch('urllib.request.urlopen', return_value=response):
                if accepted:
                    self.assertEqual(parse_request('M12205 audio files', True).category, 'Recordings')
                else:
                    with self.assertRaises(ValueError):
                        parse_request('M12205 audio files', True)

    def test_mailbox_preview_send_and_redelivery(self):
        incoming = compose('requester@example.invalid', 'agent@example.invalid', '[Regulatory Agent] M12205', 'Transcripts')
        del incoming['Auto-Submitted']
        class Inbox:
            calls = []
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def login(self, *args): pass
            def select(self, *args): return 'OK', [b'1']
            def uid(self, command, *args):
                self.calls.append((command, args))
                if command == 'search': return 'OK', [b'1']
                if command == 'fetch': return 'OK', [(b'1', incoming.as_bytes())]
                return 'OK', []
        result = dict(matter='M12205', category='Transcripts', source='test', metadata={},
                      counts={x: 0 for x in TYPES}, documents=[], errors=[])
        env = dict(AGENT_EMAIL='agent@example.invalid', MAIL_USERNAME='test', MAIL_PASSWORD='test',
                   IMAP_HOST='example.invalid', ALLOWED_SENDERS='')
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, env), \
             patch('agent.imaplib.IMAP4_SSL', return_value=Inbox()), \
             patch('browser.collect', return_value=result) as collect, patch('agent.send_message') as send:
            poll(Path(tmp), False, False, headed=True)
            self.assertTrue(collect.call_args.args[2])
            send.assert_not_called()
            poll(Path(tmp), True, False)
            poll(Path(tmp), True, False)
            send.assert_called_once()
            self.assertEqual(send.call_args.args[0]['To'], 'requester@example.invalid')
            self.assertTrue(any(c[0] == 'store' for c in Inbox.calls))

    def test_subject_prefix(self):
        for subject in ('[Regulatory Agent] Request', ' [regulatory agent] M12205 '):
            self.assertTrue(is_agent_subject(subject))
        for subject in ('Meeting tomorrow', 'Re: [Regulatory Agent] Request', '',
                        'Mentioning [Regulatory Agent] in ordinary mail'):
            self.assertFalse(is_agent_subject(subject))

    def test_unrelated_mail_body_not_fetched_or_replied_to(self):
        class Inbox:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def login(self, *args): pass
            def select(self, *args): return 'OK', [b'1']
            def uid(self, command, *args):
                if command == 'search':
                    assert 'SUBJECT' in args
                    assert '\"[Regulatory Agent]\"' in args
                    return 'OK', [b'1']
                assert command == 'fetch'
                assert args[1] == '(BODY.PEEK[HEADER.FIELDS (SUBJECT)])'
                return 'OK', [(b'1', b'Subject: Re: [Regulatory Agent] unrelated\r\n\r\n')]
        env = dict(AGENT_EMAIL='agent@example.invalid', MAIL_USERNAME='test', MAIL_PASSWORD='test', IMAP_HOST='example.invalid')
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, env), \
             patch('agent.imaplib.IMAP4_SSL', return_value=Inbox()), \
             patch('agent.create_reply') as create, patch('agent.send_message') as send:
            poll(Path(tmp), True, False)
            create.assert_not_called()
            send.assert_not_called()

    def test_dedicated_inbox_accepts_ordinary_subject(self):
        incoming = compose('requester@example.invalid', 'agent@example.invalid', 'Document request', 'M12205 Transcripts')
        del incoming['Auto-Submitted']
        class Inbox:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def login(self, *args): pass
            def select(self, *args): return 'OK', [b'1']
            def uid(self, command, *args):
                if command == 'search':
                    assert args == (None, 'UNSEEN')
                    return 'OK', [b'1']
                if command == 'fetch': return 'OK', [(b'1', incoming.as_bytes())]
                return 'OK', []
        result = dict(matter='M12205', category='Transcripts', source='test', metadata={},
                      counts={x: 0 for x in TYPES}, documents=[], errors=[])
        env = dict(AGENT_EMAIL='agent@example.invalid', MAIL_USERNAME='test', MAIL_PASSWORD='test',
                   IMAP_HOST='example.invalid', ALLOWED_SENDERS='', SUBJECT_PREFIX='')
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, env), \
             patch('agent.imaplib.IMAP4_SSL', return_value=Inbox()), \
             patch('browser.collect', return_value=result), patch('agent.send_message') as send:
            self.assertTrue(is_agent_subject('Document request'))
            self.assertTrue(is_agent_subject(''))
            poll(Path(tmp), True, False)
            send.assert_called_once()
            self.assertEqual(send.call_args.args[0]['To'], 'requester@example.invalid')

    def test_singular_category_names(self):
        for category in TYPES:
            self.assertEqual(parse_request(f'M12205 {category[:-1]} files'), Request('M12205', category))

    def test_real_email_singular_regression(self):
        msg = compose('requester@example.invalid', 'agent@example.invalid', 'Document Request',
                      'Hi Agent, can you give me other document files from M12205? Thanks!')
        self.assertEqual(parse_request(message_text(msg)), Request('M12205', 'Other Documents'))

    def test_category_whitespace_and_ambiguity(self):
        for category in ('Other  Documents', 'Other\nDocument', 'Other\u00a0Documents'):
            self.assertEqual(parse_request(f'M12205 {category}').category, 'Other Documents')
        with self.assertRaises(ValueError):
            parse_request('M12205 other document and transcript')
        with self.assertRaises(ValueError):
            parse_request('M12205 other documentary')

if __name__ == '__main__':
    unittest.main()
