"""The restart guard reads both Chat store layouts (the owner's dashboard could not restart: its per-conversation store has
no inline messages, and the guard raised KeyError on every restart). A streaming answer still blocks; a broken file says so."""
import json
import os
import tempfile
import unittest

from tutti import chat, chatrun


class Guard(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.index = os.path.join(d.name, 'abc.json')
        os.makedirs(os.path.join(d.name, 'abc'))

    def write(self, path, data):
        with open(path, 'w', encoding='utf8') as f:
            json.dump(data, f)

    def test_the_per_conversation_store_is_read_and_a_streaming_answer_blocks(self):
        self.write(self.index, {'conversations': [{'id': 'c1', 'title': 'x'}, {'id': 'c2', 'title': 'y'}]})
        self.write(self.index[:-5] + '/c1.json', {'messages': [{'role': 'user', 'status': 'done'}]})
        self.assertFalse(chatrun.store_has_streaming(self.index))   # c2 has no history file yet: nothing streams
        self.write(self.index[:-5] + '/c2.json', {'messages': [{'role': 'assistant', 'status': 'streaming'}]})
        self.assertTrue(chatrun.store_has_streaming(self.index))

    def test_the_old_inline_store_still_works_and_a_bad_file_says_so(self):
        self.write(self.index, {'conversations': [{'id': 'c1', 'messages': [{'status': 'streaming'}]}]})
        self.assertTrue(chatrun.store_has_streaming(self.index))
        self.write(self.index, {'conversations': [{'id': '../x', 'title': 'x'}]})
        with self.assertRaisesRegex(chat.ChatError, 'cannot be checked'):
            chatrun.store_has_streaming(self.index)
        self.write(self.index, {'nothing': 1})
        with self.assertRaisesRegex(chat.ChatError, 'cannot be checked'):
            chatrun.store_has_streaming(self.index)


if __name__ == '__main__':
    unittest.main()
