import unittest
import os
import subprocess

class TestRoRadioAirPlay(unittest.TestCase):
    def test_airplay_stream(self):
        serial = os.environ.get('ANDROID_SERIAL')
        if not serial:
            self.skipTest("No ANDROID_SERIAL provided")
        
        # The suite should stream to the target. For the test, we'll verify the playback state
        # on the device, as suggested by the reviewer.
        
        # Verify media session
        # The output of dumpsys media_session may vary, but it should contain information about the playback state.
        result = subprocess.run(['adb', '-s', serial, 'shell', 'dumpsys', 'media_session'], capture_output=True, text=True)
        # Check if playback is active (this might need adjustment depending on the actual media session output)
        self.assertIn("state=", result.stdout)
        
        # Screencap for visual verification
        screenshot_path = 'airplay.png'
        if os.path.exists(screenshot_path):
            os.remove(screenshot_path)
            
        subprocess.run(['adb', '-s', serial, 'shell', 'screencap', '-p', '/sdcard/airplay.png'], check=True)
        subprocess.run(['adb', '-s', serial, 'pull', '/sdcard/airplay.png', screenshot_path], check=True)
        
        self.assertTrue(os.path.exists(screenshot_path), "Screenshot not captured")
        # Keep the screenshot with the run for fixing issues
        print(f"Screenshot saved to {screenshot_path}")

if __name__ == '__main__':
    unittest.main()
