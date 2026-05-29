# qr_utils.py
import io
import os
import qrcode
from PIL import Image

BASE_URL = os.environ.get("PD_BASE_URL", "http://localhost:5000")


def generate_qr_png(plot_token: str, plot_number: str, site_name: str) -> bytes:
    url = f"{BASE_URL}/pd/{plot_token}"

    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_H,
        box_size=10,
        border=4,
    )
    qr.add_data(url)
    qr.make(fit=True)

    qr_img = qr.make_image(fill_color="#1a1a2e", back_color="white")

    from PIL import ImageDraw, ImageFont

    qr_width, qr_height = qr_img.size
    label_height = 80
    final_img = Image.new("RGB", (qr_width, qr_height + label_height), "white")
    final_img.paste(qr_img, (0, 0))

    draw = ImageDraw.Draw(final_img)
    try:
        font_large = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 18)
        font_small = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 14)
    except Exception:
        font_large = ImageFont.load_default()
        font_small = ImageFont.load_default()

    label_top = f"Plot {plot_number}"
    label_bot = site_name[:40]

    for font, text, y_offset in [(font_large, label_top, qr_height + 8), (font_small, label_bot, qr_height + 32)]:
        bbox = draw.textbbox((0, 0), text, font=font)
        text_width = bbox[2] - bbox[0]
        x = (qr_width - text_width) // 2
        draw.text((x, y_offset), text, fill="#1a1a2e", font=font)

    draw.rectangle([(0, 0), (qr_width, 6)], fill="#C5962A")

    buf = io.BytesIO()
    final_img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
