# -*- coding: utf-8 -*-
"""生成 exe 图标：暖橙圆角方块 + 白色相机轮廓，与网页内 favicon 一致。"""
from PIL import Image, ImageDraw

def make(size):
    S = size * 8
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # 圆角底
    d.rounded_rectangle([0, 0, S - 1, S - 1], radius=int(S * 0.22), fill=(201, 123, 46, 255))
    # 相机机身
    lw = max(2, int(S * 0.052))
    x0, y0, x1, y1 = S * 0.26, S * 0.34, S * 0.74, S * 0.72
    d.rounded_rectangle([x0, y0, x1, y1], radius=int(S * 0.045), outline=(255, 255, 255, 255), width=lw)
    # 取景器凸起
    d.line([S * 0.385, S * 0.34, S * 0.425, S * 0.265, S * 0.575, S * 0.265, S * 0.615, S * 0.34],
           fill=(255, 255, 255, 255), width=lw, joint="curve")
    # 镜头
    r = S * 0.105
    cx, cy = S * 0.5, S * 0.532
    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=(255, 255, 255, 255), width=lw)
    return img.resize((size, size), Image.LANCZOS)

sizes = [256, 128, 64, 48, 32, 16]
imgs = [make(s) for s in sizes]
imgs[0].save("app.ico", format="ICO", sizes=[(s, s) for s in sizes], append_images=imgs[1:])
imgs[0].save("app_256.png")
print("icon ok:", sizes)
