import unittest

from tutti import prep


class PerceptualBrightnessTest(unittest.TestCase):
    """The owner's 30 % is 30 % on the device's own slider, not 30 % of the linear range (about 80 % on a Pixel)."""

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


if __name__ == '__main__':
    unittest.main()
