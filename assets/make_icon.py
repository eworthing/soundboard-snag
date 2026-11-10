"""Draw the app icon with AppKit (no image libraries) and build assets/AppIcon.icns.

    .venv/bin/python assets/make_icon.py

Needs the window libraries from requirements.txt (PyObjC) and macOS's iconutil.
"""
import os
import subprocess
import tempfile

import AppKit
import Foundation

HERE = os.path.dirname(os.path.abspath(__file__))
BARS = (170, 300, 470, 360, 520, 290, 190)   # waveform bar heights on a 1024 canvas


def color(r, g, b, a=1.0):
    return AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(r / 255.0, g / 255.0, b / 255.0, a)


def draw_icon(size):
    s = size / 1024.0
    image = AppKit.NSImage.alloc().initWithSize_((size, size))
    image.lockFocus()
    tile = Foundation.NSMakeRect(100 * s, 100 * s, 824 * s, 824 * s)
    path = AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(tile, 186 * s, 186 * s)

    AppKit.NSGraphicsContext.saveGraphicsState()          # soft drop shadow under the tile
    shadow = AppKit.NSShadow.alloc().init()
    shadow.setShadowColor_(color(0, 0, 0, 0.35))
    shadow.setShadowBlurRadius_(28 * s)
    shadow.setShadowOffset_((0, -14 * s))
    shadow.set()
    color(40, 60, 140).set()
    path.fill()
    AppKit.NSGraphicsContext.restoreGraphicsState()

    gradient = AppKit.NSGradient.alloc().initWithStartingColor_endingColor_(
        color(92, 140, 255), color(110, 70, 220))
    gradient.drawInBezierPath_angle_(path, -90)

    width, gap = 58 * s, 40 * s
    total = len(BARS) * width + (len(BARS) - 1) * gap
    x = 512 * s - total / 2
    color(255, 255, 255, 0.96).set()
    for height in BARS:
        h = height * s
        bar = Foundation.NSMakeRect(x, 512 * s - h / 2, width, h)
        AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(bar, width / 2, width / 2).fill()
        x += width + gap
    image.unlockFocus()
    return image


def write_png(image, path):
    rep = AppKit.NSBitmapImageRep.imageRepWithData_(image.TIFFRepresentation())
    data = rep.representationUsingType_properties_(AppKit.NSBitmapImageFileTypePNG, {})
    data.writeToFile_atomically_(path, True)


def main():
    with tempfile.TemporaryDirectory() as tmp:
        iconset = os.path.join(tmp, "AppIcon.iconset")
        os.makedirs(iconset)
        for base in (16, 32, 128, 256, 512):
            write_png(draw_icon(base), os.path.join(iconset, "icon_%dx%d.png" % (base, base)))
            write_png(draw_icon(base * 2), os.path.join(iconset, "icon_%dx%d@2x.png" % (base, base)))
        out = os.path.join(HERE, "AppIcon.icns")
        subprocess.check_call(["iconutil", "-c", "icns", iconset, "-o", out])
    print("wrote", out)


if __name__ == "__main__":
    main()
