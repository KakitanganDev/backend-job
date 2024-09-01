import unittest

from main import decodeJson, get_data

class TestGeneral(unittest.TestCase):
    def test_placeholder(self):
        self.assertTrue(True)


    def test_decodejson(self):
        input = get_data()
        data = decodeJson(input)
        self.assertDictEqual(
            data,
            {
                "tiam": "bibendum",
                "lacus": 23.5,
                "sellus": False
            }
        )

