#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 PWA 图标: 圆角蓝底 + 白色哑铃"""
import os
from PIL import Image, ImageDraw

OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")


def icon(S):
    im = Image.new("RGBA", (S * 4, S * 4), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    W = S * 4
    d.rounded_rectangle([0, 0, W - 1, W - 1], radius=int(W * 0.22), fill=(37, 99, 235, 255))
    # 顶部渐变高光
    for i in range(int(W * 0.5)):
        a = int(38 * (1 - i / (W * 0.5)))
        d.line([(0, i), (W, i)], fill=(255, 255, 255, a))
    white = (255, 255, 255, 255)
    r = W * 0.028
    # 横杠
    d.rounded_rectangle([W * .30, W * .465, W * .70, W * .535], radius=r, fill=white)
    # 左右各两片配重
    for x0, x1, y0, y1 in ((.185, .265, .325, .675), (.265, .345, .395, .605),
                           (.655, .735, .395, .605), (.735, .815, .325, .675)):
        d.rounded_rectangle([W * x0, W * y0, W * x1, W * y1], radius=r, fill=white)
    return im.resize((S, S), Image.LANCZOS)


os.makedirs(OUT_DIR, exist_ok=True)
for s in (192, 512):
    icon(s).save(os.path.join(OUT_DIR, f"icon-{s}.png"))
    print("wrote", f"web/icon-{s}.png")
