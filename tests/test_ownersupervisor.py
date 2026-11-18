"""Descriptor replacement in an isolated owner process; these tests do not start the dashboard."""
import os
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from tutti import auth, chat, owneripc, ownersupervisor

SUPPORTED = os.name == 'posix' and all(hasattr(socket, name) for name in ('AF_UNIX', 'SOCK_SEQPACKET', 'SCM_RIGHTS'))


@unittest.skipUnless(SUPPORTED, 'requires Unix seqpacket sockets and descriptor passing')
class SupervisorTests(unittest.TestCase):
    def test_owner_survives_replacement_and_fences_old_gateway(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        users = auth.Users(folder.name)
        users.sign_in({'sub': 'alice', 'email': 'alice@example.com', 'name': 'Alice'})
        users.sign_in({'sub': 'bob', 'email': 'bob@example.com', 'name': 'Bob'})
        users.set_role('bob', 'viewer')
        sessions = auth.Sessions(folder.name, 1)
        alice, _ = sessions.create('alice')
        bob, _ = sessions.create('bob')
        store = chat.Store(folder.name)
        conversation = store.create('alice')
        message = {'id': 'm', 'role': 'assistant', 'status': 'streaming', 'text': ''}
        store.put_message('alice', conversation['id'], message)
        supervisor, control = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        supervisor.settimeout(5)
        code = ("import socket,sys; from tutti import auth,chat,chatrun,owneripc,ownersupervisor; "
                "s=socket.socket(fileno=int(sys.argv[1])); "
                "j=chatrun.Job('alice',sys.argv[3],{'id':'m'},replay_enabled=True); "
                "j.emit('delta',text='private'); print(j.replay.generation,flush=True); "
                "sessions=auth.Sessions(sys.argv[2],1); users=auth.Users(sys.argv[2]); store=chat.Store(sys.argv[2]); "
                "ownersupervisor.serve(s,{'m':j},authorize=lambda jobs,req: owneripc.session_follow(jobs,req,sessions,users,store))")
        process = subprocess.Popen([sys.executable, '-c', code, str(control.fileno()), folder.name, conversation['id']],
                                   pass_fds=(control.fileno(),), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        control.close()
        sockets = []
        try:
            generation = process.stdout.readline().strip()
            self.assertTrue(generation)
            self.assertEqual(ownersupervisor.reconcile(supervisor, 0, 'a' * 32), 'not_applied')
            old_owner, old_gateway = socket.socketpair()
            sockets.extend([old_owner, old_gateway])
            first_token = '1' * 32
            ownersupervisor.begin_attach(supervisor, 0, old_owner, first_token)
            self.assertTrue(supervisor.recv(128).startswith(b'active:'))   # ACK is consumed and lost
            self.assertEqual(ownersupervisor.reconcile(supervisor, 0, first_token), 'applied')
            old_owner.close()  # supervisor must not retain copies of a gateway endpoint
            request = {'action': 'follow', 'subject': 'alice', 'message': 'm', 'generation': generation,
                       'after': 0, 'request_id': 'one', 'session': alice}
            owneripc.send(old_gateway, request)
            self.assertEqual(owneripc.receive(old_gateway)['events'][0][1]['text'], 'private')
            # An old gateway cannot indefinitely delay replacement by trickling a partial frame.
            started, stop = threading.Event(), threading.Event()
            def trickle():
                try:
                    old_gateway.sendall(struct.pack('!I', 1024) + b'{')
                    started.set()
                    while not stop.wait(0.15):
                        old_gateway.sendall(b' ')
                except OSError:
                    pass
            sender = threading.Thread(target=trickle, daemon=True)
            sender.start()
            self.assertTrue(started.wait(2))
            threading.Event().wait(0.2)   # let the owner enter its frame read before asking to replace
            new_owner, new_gateway = socket.socketpair()
            sockets.extend([new_owner, new_gateway])
            began = time.monotonic()
            try:
                token = '2' * 32
                ownersupervisor.begin_attach(supervisor, 1, new_owner, token)
                # The ACK is left unread: status must skip it and reconcile against owner state.
                self.assertEqual(ownersupervisor.reconcile(supervisor, 1, token), 'applied')
                self.assertEqual(ownersupervisor.reconcile(supervisor, 1, 'a' * 32), 'conflict')
                self.assertLess(time.monotonic() - began, 3)
            finally:
                stop.set()
                sender.join(3)

            new_owner.close()
            old_gateway.settimeout(2)
            self.assertEqual(old_gateway.recv(1), b'')
            owneripc.send(new_gateway, {**request, 'subject': 'alice', 'session': bob, 'request_id': 'two'})
            self.assertEqual(owneripc.receive(new_gateway), {'ok': False, 'error': 'Answer unavailable.', 'request_id': 'two'})
            stale_owner, stale_gateway = socket.socketpair()
            sockets.extend([stale_owner, stale_gateway])
            self.assertEqual(ownersupervisor.attach(supervisor, 1, stale_owner), 'stale')
            stale_owner.close()
            stale_gateway.settimeout(2)
            self.assertEqual(stale_gateway.recv(1), b'')
            owneripc.send(new_gateway, {**request, 'request_id': 'three'})
            self.assertEqual(owneripc.receive(new_gateway)['events'][0][1]['text'], 'private')
            store.delete('alice', conversation['id'])
            owneripc.send(new_gateway, {**request, 'request_id': 'deleted'})
            self.assertEqual(owneripc.receive(new_gateway), {'ok': False, 'error': 'Answer unavailable.', 'request_id': 'deleted'})
            sessions.drop(alice)
            owneripc.send(new_gateway, {**request, 'request_id': 'revoked'})
            self.assertEqual(owneripc.receive(new_gateway), {'ok': False, 'error': 'Answer unavailable.', 'request_id': 'revoked'})
            self.assertIsNone(process.poll())
        finally:
            for sock in sockets:
                sock.close()
            supervisor.close()
            process.wait(timeout=5)
            process.stdout.close()
            process.stderr.close()

    def test_lost_control_returns_unknown_instead_of_guessing(self):
        supervisor, owner = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        try:
            owner.close()
            self.assertEqual(ownersupervisor.reconcile(supervisor, 0, 'a' * 32, timeout=0.1), 'unknown')
            for invalid in (0, -1, float('inf'), float('nan'), 31, True, '1'):
                self.assertEqual(ownersupervisor.reconcile(supervisor, 0, 'a' * 32, timeout=invalid), 'unknown')
        finally:
            supervisor.close()

    def test_rejects_non_socket_descriptor(self):
        sender, receiver = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        read_fd, write_fd = os.pipe()
        try:
            import array
            sender.sendmsg([b'attach:0'], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array('i', [read_fd]))])
            with self.assertRaises(ownersupervisor.SupervisorError):
                ownersupervisor._control_packet(receiver)
        finally:
            sender.close()
            receiver.close()
            os.close(read_fd)
            os.close(write_fd)
