"""An isolated owner can retain a real Chat Job while a trusted gateway process is replaced."""
import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tutti import auth, chat, chatrun, owneripc


class BytesSocket:
    def __init__(self, value=b''):
        self.value, self.sent = value, b''

    def recv(self, count):
        part, self.value = self.value[:count], self.value[count:]
        return part

    def sendall(self, value):
        self.sent += value


class Frames(unittest.TestCase):
    def test_round_trip_and_invalid_frames(self):
        target = BytesSocket()
        owneripc.send(target, {'kind': 'delta', 'text': 'é'})
        self.assertEqual(owneripc.receive(BytesSocket(target.sent)), {'kind': 'delta', 'text': 'é'})
        self.assertIsNone(owneripc.receive(BytesSocket()))
        for value in (b'\0', struct.pack('!I', 0), struct.pack('!I', owneripc.MAX_FRAME + 1),
                      struct.pack('!I', 2) + b'{', struct.pack('!I', 2) + b'[]'):
            with self.assertRaises(owneripc.ChannelError):
                owneripc.receive(BytesSocket(value))
        with self.assertRaises(owneripc.ChannelError):
            owneripc.send(target, {'x': 'a' * owneripc.MAX_FRAME})

    def test_oversized_batch_fails_closed_without_leaking_partial_content(self):
        job = chatrun.Job('alice', 'c', {'id': 'm'}, replay_enabled=True)
        job.emit('delta', text='x' * 40000)
        job.emit('delta', text='y' * 40000)
        request = {'action': 'follow', 'subject': 'alice', 'message': 'm', 'generation': job.replay.generation, 'after': 0}
        client = BytesSocket()
        owneripc.send(client, request)
        channel = BytesSocket(client.sent)
        owneripc.serve(channel, {'m': job}, authorize=owneripc.follow)
        result = owneripc.receive(BytesSocket(channel.sent))
        self.assertFalse(result['ok'])
        self.assertNotIn('events', result)

    def test_owner_resolves_session_ignores_claimed_subject_and_honors_revocation(self):
        with tempfile.TemporaryDirectory() as folder:
            users, sessions = auth.Users(folder), auth.Sessions(folder, 1)
            users.sign_in({'sub': 'alice', 'email': 'alice@example.com', 'name': 'Alice'})
            users.sign_in({'sub': 'bob', 'email': 'bob@example.com', 'name': 'Bob'})
            users.set_role('bob', 'viewer')
            alice, _ = sessions.create('alice')
            bob, _ = sessions.create('bob')
            store = chat.Store(folder)
            conversation = store.create('alice')
            message = {'id': 'm', 'role': 'assistant', 'status': 'streaming', 'text': ''}
            store.put_message('alice', conversation['id'], message)
            job = chatrun.Job('alice', conversation['id'], message, replay_enabled=True)
            job.emit('delta', text='private')
            request = {'subject': 'alice', 'message': 'm', 'generation': job.replay.generation, 'after': 0}
            self.assertFalse(owneripc.session_follow({'m': job}, {**request, 'session': bob}, sessions, users, store)['ok'])
            allowed = owneripc.session_follow({'m': job}, {**request, 'subject': 'bob', 'session': alice}, sessions, users, store)
            self.assertTrue(allowed['ok'])
            sessions.drop(alice)
            denied = owneripc.session_follow({'m': job}, {**request, 'session': alice}, sessions, users, store)
            self.assertEqual(denied, {'ok': False, 'error': 'Answer unavailable.'})
            self.assertNotIn(alice, repr(denied))

    def test_replay_batch_fails_closed_if_revoked_or_deleted_during_preparation(self):
        for action in ('revoke', 'delete'):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as folder:
                users, sessions, store = auth.Users(folder), auth.Sessions(folder, 1), chat.Store(folder)
                users.sign_in({'sub': 'alice', 'email': 'alice@example.com', 'name': 'Alice'})
                session, _ = sessions.create('alice')
                conversation = store.create('alice')
                message = {'id': 'm', 'role': 'assistant', 'status': 'streaming', 'text': ''}
                store.put_message('alice', conversation['id'], message)
                job = chatrun.Job('alice', conversation['id'], message, replay_enabled=True)
                job.emit('delta', text='one')
                job.emit('delta', text='two')
                request = {'session': session, 'message': 'm', 'generation': job.replay.generation, 'after': 0}
                original, calls = sessions.get, []
                def changed(sid, calls=calls, action=action, sessions=sessions, session=session, store=store, original=original):
                    calls.append(sid)
                    if len(calls) == 2:
                        if action == 'revoke':
                            sessions.drop(session)
                        else:
                            store.clear('alice')
                    return original(sid)
                with mock.patch.object(sessions, 'get', side_effect=changed):
                    response = owneripc.session_follow({'m': job}, request, sessions, users, store)
                self.assertEqual(response, {'ok': False, 'error': 'Answer unavailable.'})
                self.assertGreaterEqual(len(calls), 2)

    @unittest.skipUnless(os.name == 'posix', 'requires inherited Unix socket descriptors')
    def test_job_survives_gateway_replacement_and_other_subject_is_denied(self):
        owner_socket, gateway_socket = socket.socketpair()
        owner_code = ("import socket,sys; from tutti import chatrun,owneripc; "
                      "s=socket.socket(fileno=int(sys.argv[1])); "
                      "j=chatrun.Job('alice','c',{'id':'m'},replay_enabled=True); "
                      "j.emit('delta',text='private'); print(j.replay.generation,flush=True); "
                      "owneripc.serve(s,{'m':j},authorize=owneripc.follow)")
        gateway_code = ("import json,socket,sys; from tutti import owneripc; "
                        "s=socket.socket(fileno=int(sys.argv[1])); "
                        "owneripc.send(s,json.loads(sys.argv[2])); print(json.dumps(owneripc.receive(s)))")
        owner = subprocess.Popen([sys.executable, '-c', owner_code, str(owner_socket.fileno())],
                                 pass_fds=(owner_socket.fileno(),), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        owner_socket.close()
        try:
            generation = owner.stdout.readline().strip()
            self.assertTrue(generation)
            def request(subject):
                body = {'action': 'follow', 'subject': subject, 'message': 'm', 'generation': generation, 'after': 0}
                return json.loads(subprocess.check_output([sys.executable, '-c', gateway_code, str(gateway_socket.fileno()),
                                                          json.dumps(body)], pass_fds=(gateway_socket.fileno(),), text=True, timeout=5))
            self.assertEqual(request('bob'), {'ok': False, 'error': 'Answer unavailable.'})
            first = request('alice')
            second = request('alice')   # a new gateway process; the same owner process still owns the job
            self.assertEqual(first, second)
            self.assertEqual(first['events'][0][1]['text'], 'private')
            self.assertIsNone(owner.poll())
        finally:
            gateway_socket.close()
            owner.wait(timeout=5)
            owner.stdout.close()
            owner.stderr.close()
