from pathlib import Path
from PIL import Image, ImageDraw


root = Path(__file__).resolve().parent
files = sorted((root / "review_png").glob("*.png"))
thumb_w, thumb_h = 320, 180
gap, label_h = 18, 28
canvas = Image.new("RGB", (4 * thumb_w + 5 * gap, 4 * (thumb_h + label_h) + 5 * gap), "#DDE5EC")
draw = ImageDraw.Draw(canvas)
for index, source in enumerate(files):
    row, col = divmod(index, 4)
    x = gap + col * (thumb_w + gap)
    y = gap + row * (thumb_h + label_h + gap)
    with Image.open(source) as image:
        canvas.paste(image.convert("RGB").resize((thumb_w, thumb_h)), (x, y))
    draw.text((x + 8, y + thumb_h + 5), source.stem, fill="#17212B")
canvas.save(root / "deck_montage.png")
