"""Generate assets/glide.ico - brand tile with a white 'glide' cursor."""
from PIL import Image, ImageDraw

def make(size):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    r = size // 5
    # brand rounded tile
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=r, fill=(37, 99, 235, 255))
    # white cursor arrow, centered-ish, scaled to size
    s = size / 64.0
    def P(x, y):
        return (x * s, y * s)
    cursor = [P(24, 16), P(24, 48), P(32, 40), P(38, 52), P(43, 50), P(37, 38), P(47, 38)]
    d.polygon(cursor, fill=(255, 255, 255, 255))
    # motion trail dots behind the cursor
    for i, cx in enumerate((14, 10, 6)):
        d.ellipse([P(cx, 30 - 1.5), P(cx + 3, 30 + 1.5)], fill=(255, 255, 255, 180 - i * 45))
    return img

sizes = [16, 24, 32, 48, 64, 128, 256]
imgs = [make(s) for s in sizes]
imgs[0].save("assets/glide.ico", sizes=[(s, s) for s in sizes], append_images=imgs[1:])
print("wrote assets/glide.ico", sizes)
