import unittest
import main
import json

class TestGeneral(unittest.TestCase):
    def test_placeholder(self):
        try:
            data = json.loads(main.get_data())
            self.assertIsInstance(data, dict)
        except json.JSONDecodeError:
            self.fail("json.JSONDecodeError was raised unexpectedly!")

if __name__ == "__main__":
    unittest.main()
