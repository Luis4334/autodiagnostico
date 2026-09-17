import os

with open('plc_tag_reader.py', 'r', encoding='utf-8') as f:
    content = f.read()

content = content.replace('"[SIMEFLUM]', '"')

with open('plc_tag_reader.py', 'w', encoding='utf-8') as f:
    f.write(content)

print("plc_tag_reader.py updated successfully.")
