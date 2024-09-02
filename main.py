import json

def get_data():
    #False changed to false: JSON and Python uses different capitalisation
    return '{"tiam": "bibendum", "lacus": 23.5, "sellus": false}'

def main():
    data = json.loads(get_data())
    print(data)

if __name__ == '__main__':
    main()