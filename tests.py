import unittest
import json
from main import get_data

class TestGeneral(unittest.TestCase):
   
    def test_valid_json(self):
        data_str = get_data()

        try:
            json_data = json.loads(data_str)
        
        except json.JSONDecodeError as message:
            self.fail(f"Invalid JSON: {message}")