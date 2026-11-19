import unittest

from tutti import prep


class PerceptualBrightnessTest(unittest.TestCase):
    """The requested percentage uses the Android slider's perceptual scale."""

    def test_thirty_percent_on_the_slider_is_a_low_linear_value(self):
        self.assertLess(prep.linear_brightness(30), 20)

    def test_the_ends_of_the_slider(self):
        self.assertEqual(prep.linear_brightness(0), 1)
        self.assertEqual(prep.linear_brightness(100), 255)

    def test_the_phone_default_uses_it(self):
        item = [x for x in prep.DEFAULTS['phone'] if x.get('setting') == 'system screen_brightness'][0]
        self.assertEqual(item['value'], prep.linear_brightness(30))

    def test_a_watch_gets_the_same_brightness(self):
        item = [x for x in prep.DEFAULTS['watch'] if x.get('setting') == 'system screen_brightness'][0]
        self.assertEqual(item['value'], prep.linear_brightness(30))

    def test_a_tv_gets_the_same_brightness(self):
        item = [x for x in prep.DEFAULTS['tv'] if x.get('setting') == 'system screen_brightness'][0]
        self.assertEqual(item['value'], prep.linear_brightness(30))

    def test_a_tv_keeps_its_brightness_items_after_the_deny_list(self):
        names = [x['name'] for x in prep.items({'name': 'tv', 'kind': 'tv'}, False)]
        self.assertIn('screen_brightness', names)


if __name__ == '__main__':
    unittest.main()
