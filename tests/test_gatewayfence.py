"""A replacement fence isolates two distinct gateway sockets in an isolated process."""
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

from tutti import auth, chat, chatrun, gatewayfence, owneripc


@unittest.skipUnless(os.name == 'posix', 'requires Unix socket pairs')
class GatewayFenceTests(unittest.TestCase):
    def setUp(self):
        self.old_owner, self.old_gateway = socket.socketpair()
        self.new_owner, self.new_gateway = socket.socketpair()
        self.addCleanup(self.old_owner.close)
        self.addCleanup(self.old_gateway.close)
        self.addCleanup(self.new_owner.close)
        self.addCleanup(self.new_gateway.close)
        job = chatrun.Job('alice', 'c', {'id': 'm'}, replay_enabled=True)
        job.emit('delta', text='private')
        self.fence = gatewayfence.GatewayFence({'m': job}, authorize=owneripc.follow)
        self.request = {'action': 'follow', 'subject': 'alice', 'message': 'm', 'generation': job.replay.generation,
                        'after': 0, 'request_id': 'first'}
        self.assertTrue(self.fence.replace(None, self.old_owner))

    def test_replacement_refuses_stale_authority_and_foreign_reply(self):
        self.assertTrue(self.fence.dispatch(self.old_owner, self.request))
        self.assertEqual(owneripc.receive(self.old_gateway)['request_id'], 'first')
        self.assertTrue(self.fence.replace(self.old_owner, self.new_owner))
        self.assertFalse(self.fence.replace(self.old_owner, self.old_owner))
        self.assertFalse(self.fence.dispatch(self.old_owner, self.request))
        request = {**self.request, 'subject': 'bob', 'request_id': 'second'}
        self.assertTrue(self.fence.dispatch(self.new_owner, request))
        response = owneripc.receive(self.new_gateway)
        self.assertEqual(response, {'ok': False, 'error': 'Answer unavailable.', 'request_id': 'second'})
        self.old_gateway.settimeout(0.05)
        with self.assertRaises(TimeoutError):
            self.old_gateway.recv(1)

    def test_replacement_waits_for_inflight_send_before_fencing_old_gateway(self):
        entered, release, replaced = threading.Event(), threading.Event(), threading.Event()
        original = owneripc.send
        def slow_send(sock, value):
            entered.set()
            release.wait(5)
            original(sock, value)
        with mock.patch('tutti.gatewayfence.owneripc.send', side_effect=slow_send):
            dispatch = threading.Thread(target=lambda: self.fence.dispatch(self.old_owner, self.request), daemon=True)
            switch = threading.Thread(target=lambda: (self.fence.replace(self.old_owner, self.new_owner), replaced.set()), daemon=True)
            dispatch.start()
            self.assertTrue(entered.wait(5))
            switch.start()
            try:
                self.assertFalse(replaced.wait(0.1))
            finally:
                release.set()
                dispatch.join(5)
                switch.join(5)
        self.assertTrue(replaced.is_set())
        self.assertFalse(self.fence.dispatch(self.old_owner, self.request))
        self.assertEqual(owneripc.receive(self.old_gateway)['request_id'], 'first')

    def test_send_failure_fences_channel_before_any_retry(self):
        with mock.patch('tutti.gatewayfence.owneripc.send', side_effect=TimeoutError('send timed out')):
            with self.assertRaises(TimeoutError):
                self.fence.dispatch(self.old_owner, self.request)
        self.assertFalse(self.fence.dispatch(self.old_owner, self.request))
        duplicate = self.old_owner.dup()
        self.addCleanup(duplicate.close)
        for poisoned in (self.old_owner, duplicate):
            with self.assertRaises(gatewayfence.FenceError):
                self.fence.replace(None, poisoned)
        self.assertTrue(self.fence.replace(None, self.new_owner))

    def test_stalled_store_authorization_cannot_block_gateway_replacement(self):
        with tempfile.TemporaryDirectory() as folder:
            users, sessions, store = auth.Users(folder), auth.Sessions(folder, 1), chat.Store(folder)
            users.sign_in({'sub': 'alice', 'email': 'alice@example.com', 'name': 'Alice'})
            session, _ = sessions.create('alice')
            conversation = store.create('alice')
            message = {'id': 'm', 'role': 'assistant', 'status': 'streaming', 'text': ''}
            store.put_message('alice', conversation['id'], message)
            job = chatrun.Job('alice', conversation['id'], message, replay_enabled=True)
            job.emit('delta', text='private')
            code = ("import sys; from tutti import chat; "
                    "lock=chat.store_locked(sys.argv[1]); lock.__enter__(); "
                    "print('locked',flush=True); sys.stdin.read(); lock.__exit__(None,None,None)")
            child = subprocess.Popen([sys.executable, '-c', code, store.folder], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            entered, finished = threading.Event(), threading.Event()
            def authorize(jobs, request):
                entered.set()
                try:
                    return owneripc.session_follow(jobs, request, sessions, users, store)
                finally:
                    finished.set()
            fence = gatewayfence.GatewayFence({'m': job}, send_timeout=0.2, authorize=authorize)
            request = {'action': 'follow', 'session': session, 'message': 'm', 'generation': job.replay.generation,
                       'after': 0, 'request_id': 'blocked'}
            worker = threading.Thread(target=lambda: fence.dispatch(self.old_owner, request), daemon=True)
            try:
                self.assertEqual(child.stdout.readline().strip(), 'locked')
                self.assertTrue(fence.replace(None, self.old_owner))
                worker.start()
                self.assertTrue(entered.wait(3))
                self.assertTrue(fence.replace(self.old_owner, self.new_owner))
                worker.join(3)
                self.assertFalse(worker.is_alive())
                self.assertFalse(owneripc.receive(self.old_gateway)['ok'])
                self.assertTrue(fence.dispatch(self.new_owner, {**request, 'request_id': 'busy'}))
                self.assertFalse(owneripc.receive(self.new_gateway)['ok'])
                self.assertFalse(finished.is_set())   # timed-out worker remains bounded to one outstanding request
            finally:
                child.stdin.close()
                child.wait(timeout=5)
                child.stdout.close()
                child.stderr.close()
                worker.join(5)
                self.assertTrue(finished.wait(5))

    def test_duplicate_descriptor_cannot_be_replacement(self):
        duplicate = self.old_owner.dup()
        self.addCleanup(duplicate.close)
        with self.assertRaises(gatewayfence.FenceError):
            self.fence.replace(self.old_owner, duplicate)
