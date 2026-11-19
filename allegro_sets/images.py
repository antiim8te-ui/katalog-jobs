"""Set-Hauptbild: Produkt zweimal nebeneinander auf weißem Quadrat.

Allegro verlangt beim Hauptbild weißen Hintergrund ohne Text, Logos oder
Rahmen. Deshalb kein „x2“-Aufdruck, die Verdopplung zeigt das Set.
"""
from __future__ import annotations

import base64
import io

import requests
from PIL import Image, ImageChops

CANVAS = 2000          # Allegro empfiehlt mindestens 1200 px
MARGIN = 0.06          # Rand rundherum
OVERLAP = 0.12         # wie weit sich die beiden Produkte überlappen (0 = nebeneinander)


BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0 Safari/537.36",
    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
}


def download(url: str) -> Image.Image:
    """Bild laden. Bei 403 einmal mit Base-Panel als Herkunft erneut versuchen."""
    r = requests.get(url, timeout=60, headers=BROWSER_HEADERS)
    if r.status_code == 403:
        r = requests.get(url, timeout=60, headers={**BROWSER_HEADERS, "Referer": "https://panel-e.baselinker.com/"})
    r.raise_for_status()
    return Image.open(io.BytesIO(r.content))


def _cut_out(img: Image.Image, use_rembg: bool) -> Image.Image:
    """Hintergrund entfernen bzw. weißen Rand wegschneiden, RGBA zurückgeben."""
    img = img.convert("RGBA")
    if use_rembg:
        try:
            from rembg import remove
            img = remove(img)
        except ImportError:
            use_rembg = False
    if not use_rembg:
        # Weiß (fast weiß) transparent machen, damit sich die Kopien sauber überlappen
        rgb = img.convert("RGB")
        bg = Image.new("RGB", rgb.size, (255, 255, 255))
        diff = ImageChops.difference(rgb, bg).convert("L").point(lambda p: 255 if p > 12 else 0)
        img.putalpha(ImageChops.multiply(img.getchannel("A"), diff))
    bbox = img.getchannel("A").getbbox()
    return img.crop(bbox) if bbox else img


def make_set_image(src: Image.Image, count: int = 2, use_rembg: bool = False,
                   overlap: float = OVERLAP) -> Image.Image:
    item = _cut_out(src, use_rembg)
    usable = CANVAS * (1 - 2 * MARGIN)
    w, h = item.size
    # Gesamtbreite: count Kopien minus Überlappung
    total_w_factor = count - (count - 1) * overlap
    scale = min(usable / (w * total_w_factor), usable / h)
    item = item.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
    w, h = item.size
    total_w = int(w * total_w_factor)
    canvas = Image.new("RGBA", (CANVAS, CANVAS), (255, 255, 255, 255))
    x0 = (CANVAS - total_w) // 2
    y = (CANVAS - h) // 2
    step = int(w * (1 - overlap))
    # hinteres Exemplar zuerst, leicht nach oben versetzt für Tiefe
    for i in range(count):
        dy = -int(h * 0.03) if i < count - 1 and overlap > 0 else 0
        canvas.alpha_composite(item, (x0 + i * step, y + dy))
    return canvas.convert("RGB")


def to_base_image(img: Image.Image) -> str:
    """Format, das addInventoryProduct für Bilder erwartet: 'data:<base64>'."""
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90)
    return "data:" + base64.b64encode(buf.getvalue()).decode()


def url_to_base_image(url: str) -> str:
    """Fremde Bilder herunterladen und als base64 übergeben, statt CDN-Links
    erneut einzureichen (laut Base-Doku können die sonst gelöscht werden)."""
    img = download(url).convert("RGB")
    return to_base_image(img)
