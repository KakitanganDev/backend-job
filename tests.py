import unittest

from main import decodeJson

class TestGeneral(unittest.TestCase):
    def test_placeholder(self):
        self.assertTrue(True)


    def test_decodejson(self):
        input = '{"tiam": "bibendum", "lacus": 23.5, "sellus": false}'
        data = decodeJson(input)
        self.assertDictEqual(
            data,
            {
                "tiam": "bibendum",
                "lacus": 23.5,
                "sellus": False
            }
        )

