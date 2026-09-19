import subprocess
import os
from PIL import Image, ImageDraw, ImageFont

def render_text_to_image(text, filename):
    lines = text.split('\n')
    # Use a default font, or just default PIL font
    # Estimate size
    char_width = 7
    line_height = 15
    width = max((len(line) for line in lines), default=0) * char_width + 40
    width = max(width, 800)
    height = len(lines) * line_height + 40
    
    img = Image.new('RGB', (width, height), color=(30, 30, 30))
    d = ImageDraw.Draw(img)
    
    try:
        font = ImageFont.truetype("consola.ttf", 12)
    except:
        font = ImageFont.load_default()
        
    y = 20
    for line in lines:
        d.text((20, y), line, fill=(200, 200, 200), font=font)
        y += line_height
        
    img.save(os.path.join('Screenshots', filename))

os.makedirs('Screenshots', exist_ok=True)

print("Running demo...")
result = subprocess.run(['python', 'run_demo.py'], capture_output=True, text=True, encoding='utf-8', errors='replace')
output = result.stdout + "\n" + result.stderr

# simple heuristic to split scenarios
scenarios = output.split('========================================================================')

s1 = [s for s in scenarios if 'SCENARIO 1' in s]
s2 = [s for s in scenarios if 'SCENARIO 2' in s]
s3 = [s for s in scenarios if 'SCENARIO 3' in s]
s4 = [s for s in scenarios if 'SCENARIO 4' in s]

if s1: render_text_to_image(s1[0].strip(), '01_grounded_run.png')
if s2: render_text_to_image(s2[0].strip(), '02_deterministic_decline.png')
if s3: render_text_to_image(s3[0].strip(), '03_prompt_injection_defense.png')
if s4: render_text_to_image(s4[0].strip(), '04_irrelevant_kb_decline.png')

print("Running tests...")
result = subprocess.run(['python', '-m', 'pytest', 'tests/', '-v'], capture_output=True, text=True, encoding='utf-8', errors='replace')
output = result.stdout + "\n" + result.stderr

# grab last 50 lines of pytest output to keep it small
test_lines = output.split('\n')[-50:]
render_text_to_image('\n'.join(test_lines), '05_pytest_results.png')
print("Done!")
