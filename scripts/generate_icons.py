import sys
import os
import subprocess

# Ensure Pillow is installed
try:
    from PIL import Image
except ImportError:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "Pillow"])
    from PIL import Image

input_path = r"C:\Users\nikla\OneDrive\Desktop\opentraitor.png"
output_dir = r"d:\Development\auto-traitor\dashboard\frontend\public"

if not os.path.exists(output_dir):
    os.makedirs(output_dir)

if not os.path.exists(input_path):
    print(f"Error: Could not find '{input_path}'. Please make sure the image exists at this path.")
    sys.exit(1)

try:
    img = Image.open(input_path)
except Exception as e:
    print(f"Error opening image: {e}")
    sys.exit(1)

# Ensure the image has an alpha channel
img = img.convert('RGBA')

# Process image to make white pixels transparent
data = img.getdata()
new_data = []
# Assuming white background with some tolerance for anti-aliasing
for item in data:
    # item is (R, G, B, A)
    # Check if pixel is white or very close to white (tolerance can be adjusted)
    if item[0] > 240 and item[1] > 240 and item[2] > 240:
        # Change white pixels to completely transparent
        new_data.append((255, 255, 255, 0))
    else:
        new_data.append(item)
        
img.putdata(new_data)

# Crop the image to its bounding box (removes empty transparent space)
bbox = img.getbbox()
if bbox:
    img = img.crop(bbox)

# Make the image perfectly square again by padding with transparency 
# to ensure it's not distorted when resized for icons
width, height = img.size
new_size = max(width, height)
# Add a very small 2% padding so it doesn't touch the absolute edges
padding = int(new_size * 0.02)
new_size += padding * 2

square_img = Image.new('RGBA', (new_size, new_size), (0, 0, 0, 0))
offset = ((new_size - width) // 2, (new_size - height) // 2)
square_img.paste(img, offset)
img = square_img

# 1. Generate multi-size favicon.ico
ico_path = os.path.join(output_dir, "favicon.ico")
img.save(ico_path, format="ICO", sizes=[(16, 16), (32, 32), (48, 48), (64, 64)])
print(f"Generated: {ico_path}")

# 2. Generate standard web sizes
sizes = {
    "favicon-16x16.png": (16, 16),
    "favicon-32x32.png": (32, 32),
    "apple-touch-icon.png": (180, 180),
    "android-chrome-192x192.png": (192, 192),
    "android-chrome-512x512.png": (512, 512),
}

for filename, size in sizes.items():
    output_path = os.path.join(output_dir, filename)
    
    # Use LANCZOS for high-quality downsampling
    if hasattr(Image, 'Resampling'):
        resized_img = img.resize(size, Image.Resampling.LANCZOS)
    else:
        resized_img = img.resize(size, Image.ANTIALIAS)
        
    resized_img.save(output_path, format="PNG")
    print(f"Generated: {output_path}")

print("All transparent icons successfully generated in the public directory!")
