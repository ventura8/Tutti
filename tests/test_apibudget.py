"""The disabled local API budget ledger never calls a provider or uses real credentials."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tutti import apibudget

POLICY = {'model': 'example-model', 'limit_micros': 20, 'input_micros_per_million': 1000000,
          'output_micros_per_million': 1000000}


@unittest.skipUnless(apibudget.fcntl and getattr(os, 'O_NOFOLLOW', 0), 'requires safe Unix file locking')
class APIBudget(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = self.folder.name

    def test_explicit_initialization_no_reset_and_conservative_rounding(self):
        with self.assertRaises(apibudget.BudgetError):
            apibudget.reserve(self.path, POLICY, '1' * 32, 4, 6)
        apibudget.initialize(self.path, POLICY)
        with self.assertRaises(apibudget.BudgetError):
            apibudget.initialize(self.path, POLICY)
        self.assertEqual(apibudget.reserve(self.path, POLICY, '1' * 32, 4, 6), 10)
        self.assertEqual(apibudget.ceiling({**POLICY, 'input_micros_per_million': 1, 'output_micros_per_million': 1}, 1, 1), 2)
        with self.assertRaises(apibudget.BudgetError):
            apibudget.reserve(self.path, POLICY, '1' * 32, 4, 6)
        with self.assertRaises(apibudget.BudgetError):
            apibudget.reserve(self.path, {**POLICY, 'limit_micros': 100}, '2' * 32, 4, 6)

    def test_recording_cost_never_refunds_unknown_or_lower_actual(self):
        apibudget.initialize(self.path, POLICY)
        apibudget.reserve(self.path, POLICY, '1' * 32, 4, 6)
        apibudget.record_result(self.path, '1' * 32, 1)
        self.assertEqual(apibudget.reserve(self.path, POLICY, '2' * 32, 4, 6), 10)
        with self.assertRaises(apibudget.BudgetError):
            apibudget.reserve(self.path, POLICY, '3' * 32, 1, 1)
        with self.assertRaises(apibudget.BudgetError):
            apibudget.record_result(self.path, '1' * 32, 2)

    def test_overrun_blocks_future_calls(self):
        apibudget.initialize(self.path, POLICY)
        apibudget.reserve(self.path, POLICY, '1' * 32, 4, 6)
        apibudget.record_result(self.path, '1' * 32, 21)
        with self.assertRaises(apibudget.BudgetError):
            apibudget.reserve(self.path, POLICY, '2' * 32, 1, 1)

    def test_malformed_missing_or_linked_state_fails_closed(self):
        apibudget.initialize(self.path, POLICY)
        path = os.path.join(self.path, 'budget.json')
        with open(path, 'w', encoding='utf8') as f:
            f.write('{bad')
        with self.assertRaises(apibudget.BudgetError):
            apibudget.reserve(self.path, POLICY, '1' * 32, 1, 1)
        os.remove(path)
        with self.assertRaises(apibudget.BudgetError):
            apibudget.reserve(self.path, POLICY, '1' * 32, 1, 1)
        os.symlink('missing', path)
        with self.assertRaises(apibudget.BudgetError):
            apibudget.reserve(self.path, POLICY, '1' * 32, 1, 1)

    def test_structurally_understated_liability_is_rejected(self):
        apibudget.initialize(self.path, POLICY)
        apibudget.reserve(self.path, POLICY, '1' * 32, 4, 6)
        path = os.path.join(self.path, 'budget.json')
        with open(path, encoding='utf8') as f:
            data = json.load(f)
        data['entries']['1' * 32]['liability_micros'] = 1
        with open(path, 'w', encoding='utf8') as f:
            json.dump(data, f)
        with self.assertRaises(apibudget.BudgetError):
            apibudget.reserve(self.path, POLICY, '2' * 32, 1, 1)

    def test_symlink_folder_is_refused(self):
        link = os.path.join(self.path, 'link')
        target = os.path.join(self.path, 'target')
        os.mkdir(target)
        os.symlink(target, link)
        with self.assertRaises(apibudget.BudgetError):
            apibudget.initialize(link, POLICY)

    def test_shared_budget_directory_is_refused(self):
        os.chmod(self.path, 0o777)
        try:
            with self.assertRaises(apibudget.BudgetError):
                apibudget.initialize(self.path, POLICY)
        finally:
            os.chmod(self.path, 0o700)

    def test_replaced_lock_inode_is_refused(self):
        real_flock = apibudget.fcntl.flock
        def replace_lock(fd, operation):
            real_flock(fd, operation)
            path = os.path.join(self.path, '.budget.lock')
            os.unlink(path)
            with open(path, 'w', encoding='utf8'):
                pass
        with mock.patch.object(apibudget.fcntl, 'flock', side_effect=replace_lock):
            with self.assertRaises(apibudget.BudgetError):
                apibudget.initialize(self.path, POLICY)

    def test_duplicate_json_keys_are_rejected(self):
        apibudget.initialize(self.path, POLICY)
        path = os.path.join(self.path, 'budget.json')
        with open(path, encoding='utf8') as f:
            text = f.read()
        text = text.replace('"entries": {}', '"entries": {}, "entries": {}')
        with open(path, 'w', encoding='utf8') as f:
            f.write(text)
        with self.assertRaises(apibudget.BudgetError):
            apibudget.reserve(self.path, POLICY, '1' * 32, 1, 1)

    def test_lock_symlink_is_refused(self):
        os.symlink('missing', os.path.join(self.path, '.budget.lock'))
        with self.assertRaises(apibudget.BudgetError):
            apibudget.initialize(self.path, POLICY)

    def test_crash_after_reserve_keeps_liability(self):
        apibudget.initialize(self.path, POLICY)
        code = ('import json,os,sys; from tutti import apibudget; '
                "apibudget.reserve(sys.argv[1],json.loads(sys.argv[2]),'1'*32,9,11); os._exit(0)")
        subprocess.run([sys.executable, '-c', code, self.path, json.dumps(POLICY)], check=True, timeout=5)
        with self.assertRaises(apibudget.BudgetError):
            apibudget.reserve(self.path, POLICY, '2' * 32, 1, 1)

    def test_concurrent_processes_cannot_reserve_over_limit(self):
        apibudget.initialize(self.path, POLICY)
        code = ("import json,sys; from tutti import apibudget; sys.stdin.readline(); "
                "exec('try:\\n apibudget.reserve(sys.argv[1],json.loads(sys.argv[2]),sys.argv[3],4,6)\\n print(\\\"ok\\\")"
                "\\nexcept apibudget.BudgetError:\\n print(\\\"denied\\\")')")
        children = [subprocess.Popen([sys.executable, '-c', code, self.path, json.dumps(POLICY), f'{i:032x}'],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                    for i in range(8)]
        try:
            for child in children:
                child.stdin.write('\n')
                child.stdin.close()
            results = []
            for child in children:
                child.wait(timeout=10)
                results.append(child.stdout.read().strip())
                self.assertEqual(child.returncode, 0, child.stderr.read())
            self.assertEqual(results.count('ok'), 2)
            self.assertEqual(results.count('denied'), 6)
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=5)
                child.stdout.close()
                child.stderr.close()
