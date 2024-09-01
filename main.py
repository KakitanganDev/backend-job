import json

def get_data():
    return '{"tiam": "bibendum", "lacus": 23.5, "sellus": false}'


def decodeJson(input: str | bytes | bytearray):
    data = json.loads(input)
    return data

def main():
    print(decodeJson(get_data()))

if __name__ == '__main__':
    main()
