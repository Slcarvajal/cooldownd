# /// script
# requires-python = ">=3.10"
# dependencies = ["pillow", "numpy", "scipy"]
# ///
"""Cut the avatar's 4×4 sprite sheets into the strips the game animates (admin/life/sprites/<outfit>.png).

  uv run admin/life/make_sprites.py casual=<sheet.png> gym=<sheet.png> volleyball=<sheet.png>

Each sheet is 4 rows (front, left, right, back) × 4 columns (stand, step, step, action); the last
cell of the back row is a celebration pose. A black background is cut away: the figure is everything brighter than
the background plus what it encloses, grown by the outline's width (the outline, glasses and pupils
are as black as the background), minus thick pockets of pure black between the arms and legs.
Scraps of a neighboring cell that cross the cell's edge are dropped.
Every frame is placed on the same canvas with the feet on one baseline and the head centered, so the
animation doesn't jitter. Runs on the desktop only; the panel serves the PNGs it writes."""
import os
import sys

import numpy as np
from PIL import Image
from scipy import ndimage

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sprites")
BLACK = 12      # max channel of the background; the outfit is ~35
PURE = 3        # an enclosed pocket this dark and thicker than an outline is background
OUTLINE = 6     # pixels the figure is grown by to get its black outline back
SCALE = 0.5
SPECK = 40      # leftover blobs smaller than this are noise
SCRAP = 1500    # a dark blob apart from the figure and smaller than this is a scrap


def cut(path):
    im = np.array(Image.open(path).convert("RGBA"))
    if im[..., 3].min() == 255:  # no alpha yet: remove the black background
        bright = im[..., :3].max(axis=2) > BLACK
        filled = ndimage.binary_fill_holes(bright)
        pockets = ndimage.binary_opening(filled & (im[..., :3].max(axis=2) <= PURE), _disk(OUTLINE + 1))
        im[..., 3] = 255 * ndimage.binary_dilation(filled & ~pockets, _disk(OUTLINE))
    return im


def _disk(r):
    y, x = np.ogrid[-r:r + 1, -r:r + 1]
    return x * x + y * y <= r * r


def frames(im):
    h, w = im.shape[:2]
    out = []
    for r in range(4):
        for c in range(4):
            cell = im[r * h // 4:(r + 1) * h // 4, c * w // 4:(c + 1) * w // 4].copy()
            solid = cell[..., 3] > 40
            # Parts are told apart on a slimmed mask: growing the outline back can glue a neighbor's
            # shoe onto the hair. A scrap is tiny, crosses the cell's edge, or is a small black bit;
            # the ball, motion lines and sparkles are bright and stay.
            core = ndimage.binary_erosion(solid, _disk(OUTLINE), border_value=1)  # what crosses the edge still does
            labels, n = ndimage.label(core)
            sizes = ndimage.sum(core, labels, range(n + 1))
            edge = set(np.unique(np.concatenate([labels[0], labels[-1], labels[:, 0], labels[:, -1]])))
            main = int(np.argmax(sizes[1:])) + 1
            shade = ndimage.mean(cell[..., :3].max(axis=2), labels, range(n + 1))
            scrap = [i for i in range(1, n + 1) if i != main and (sizes[i] < SPECK or i in edge or (sizes[i] < SCRAP and shade[i] < 60))]
            keep = ndimage.binary_dilation(core & ~np.isin(labels, scrap), _disk(OUTLINE + 1)) & solid
            thin, m = ndimage.label(solid & ~ndimage.binary_dilation(core, _disk(OUTLINE + 1)))  # too thin to survive slimming
            thin_edge = set(np.unique(np.concatenate([thin[0], thin[-1], thin[:, 0], thin[:, -1]])))
            thin_shade = ndimage.mean(cell[..., :3].max(axis=2), thin, range(m + 1))
            keep |= np.isin(thin, [i for i in range(1, m + 1) if i not in thin_edge and thin_shade[i] >= 60])
            cell[~keep, 3] = 0
            ys, xs = np.nonzero(cell[..., 3] > 40)
            top, bottom, left, right = ys.min(), ys.max(), xs.min(), xs.max()
            head = xs[ys < top + (bottom - top) * 0.3]  # the hair and face: steady between poses
            out.append((cell, int(head.mean()), int(bottom), (top, left, right)))
    return out


def main(args):
    sheets = dict(a.split("=", 1) for a in args)
    cut_frames = {name: frames(cut(path)) for name, path in sheets.items()}
    every = [f for fs in cut_frames.values() for f in fs]
    left = max(cx - l for _, cx, _, (t, l, r) in every)
    right = max(r - cx for _, cx, _, (t, l, r) in every)
    up = max(b - t for _, _, b, (t, l, r) in every)
    W, H = left + right + 9, up + 9
    W, H = W + W % 2, H + H % 2  # whole pixels after SCALE
    os.makedirs(OUT, exist_ok=True)
    for name, fs in cut_frames.items():
        strip = Image.new("RGBA", (W * len(fs), H))
        for i, (cell, cx, b, _) in enumerate(fs):
            strip.alpha_composite(Image.fromarray(cell), (i * W + left + 4 - cx, H - 4 - b))
        size = (round(strip.width * SCALE), round(H * SCALE))
        strip.resize(size, Image.BOX).save(os.path.join(OUT, f"{name}.png"), optimize=True)
        print(f"{name}: {len(fs)} frames of {size[0] // len(fs)}×{size[1]}")


if __name__ == "__main__":
    main(sys.argv[1:])
