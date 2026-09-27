import os

text = open('take_screenshots.py', 'r', encoding='utf-8').read()
text = text.replace("encoding='utf-8'", "encoding='utf-8', errors='replace'")
open('take_screenshots.py', 'w', encoding='utf-8').write(text)
