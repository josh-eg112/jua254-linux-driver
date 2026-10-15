#!/usr/bin/env python3
"""Render icon.svg at every macOS icon size and pack AppIcon.icns (needs rsvg-convert and Pillow).
Run after editing icon.svg: python macos/native/icon/make_icon.py"""
import io, subprocess
from pathlib import Path
from PIL import Image

here = Path(__file__).resolve().parent
sizes = [16, 32, 64, 128, 256, 512, 1024]
images = {n: Image.open(io.BytesIO(subprocess.run(["rsvg-convert", "-w", str(n), "-h", str(n), str(here / "icon.svg")],
                                                  capture_output=True, check=True).stdout)).convert("RGBA") for n in sizes}
images[1024].save(here / "AppIcon.icns", append_images=[images[n] for n in sizes[:-1]])
images[1024].save(here / "icon-1024.png")
print("wrote AppIcon.icns and icon-1024.png")
