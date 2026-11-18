"""A replacement fence isolates two distinct gateway sockets in an isolated process."""
import os
import socket
import threading
import unittest
from unittest import mock

from tutti import chatrun, gatewayfence, owneripc


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
        self.fence = gatewayfence.GatewayFence({'m': job})
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

    def test_duplicate_descriptor_cannot_be_replacement(self):
        duplicate = self.old_owner.dup()
        self.addCleanup(duplicate.close)
        with self.assertRaises(gatewayfence.FenceError):
            self.fence.replace(self.old_owner, duplicate)
