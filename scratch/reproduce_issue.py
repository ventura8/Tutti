import unittest
from unittest import mock
from tutti import autopilot

class TestTakeoverRoom(unittest.TestCase):
    def test_takeover_room_respects_quota_plan(self):
        cfg = {'accounts': {'cursor': {'task_pct': 1}}}
        # Subordinate: exhausted quota plan (limit 1, used 1)
        s = {'quota_plan': {'tasks': 1}, 'used': 1, 'panic_at': 95}
        
        # Room should be 0 because quota is exhausted
        room = autopilot.takeover_room(cfg, 'cursor', s)
        print(f"DEBUG: takeover_room room={room}")
        self.assertEqual(room, 0, "Subordinate should have 0 room if quota is exhausted")

if __name__ == '__main__':
    unittest.main()
