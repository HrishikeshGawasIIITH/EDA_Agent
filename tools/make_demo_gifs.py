"""
make_demo_gifs.py — Render the README demo animations.

Two generators, both driven by real captured material:

  terminal_gif()   Animates a real agent transcript (captured verbatim from
                   `python -m eda_agent.cli`) as a scrolling terminal.
  frames_gif()     Animates a sequence of Virtuoso screenshots captured at each
                   step of a schematic build.

Colour emoji have no glyph in the available monospace fonts, so the terminal
renderer substitutes monochrome equivalents (✅ → ✔, 📋 → ▸, …). Only the glyph
changes; the transcript text is otherwise byte-for-byte what the agent printed.

Usage:
    python tools/make_demo_gifs.py --terminal <transcript.txt> --out docs/media/x.gif
    python tools/make_demo_gifs.py --frames "shots/*.png" --out docs/media/y.gif
"""

import argparse
import glob as globmod
import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FONT_REG = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"

# Emoji the agent prints -> monochrome glyphs the mono font actually has.
GLYPH_FALLBACK = {
    "🤖": "»", "📋": "▸", "⚙️": "⚙", "⚙": "⚙", "✅": "✔", "❌": "✗",
    "📊": "≡", "🔄": "↻", "📚": "≣", "📐": "◹", "🛑": "■", "💬": "▪",
    "🔍": "◎", "⚠️": "!", "⚠": "!", "✨": "*", "🎯": "◉",
}

BG = (13, 17, 23)
FG = (201, 209, 217)
DIM = (110, 118, 129)
GREEN = (63, 185, 80)
CYAN = (86, 182, 194)
YELLOW = (210, 168, 58)
RED = (248, 81, 73)
MAGENTA = (188, 140, 255)


def _subst(text: str) -> str:
    for k, val in GLYPH_FALLBACK.items():
        text = text.replace(k, val)
    return text


def _strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)


def _colour_for(line: str):
    """Pick a colour for a transcript line from its leading marker."""
    s = line.strip()
    if s.startswith(">"):
        return GREEN
    if s.startswith("▸") or s.startswith("Plan:"):
        return CYAN
    if s.startswith("✔"):
        return GREEN
    if s.startswith("✗") or "Error" in s or "Traceback" in s:
        return RED
    if s.startswith("↻") or s.startswith("⚙"):
        return YELLOW
    if s.startswith("≡") or s.startswith("≣") or s.startswith("◹"):
        return MAGENTA
    if s.startswith("=") or s.startswith("-"):
        return DIM
    return FG


def _wrap(line: str, cols: int) -> list:
    """Hard-wrap a line to the terminal width, preserving indentation."""
    if len(line) <= cols:
        return [line]
    indent = len(line) - len(line.lstrip())
    pad = " " * min(indent + 2, cols - 10)
    out, cur = [], line
    first = True
    while len(cur) > cols:
        cut = cur.rfind(" ", 0, cols)
        if cut <= indent:
            cut = cols
        out.append(cur[:cut])
        cur = (pad if not first else pad) + cur[cut:].lstrip()
        first = False
    out.append(cur)
    return out


def _render_screen(lines, cols, rows, font, cw, ch, pad, title=None):
    """Draw one terminal screen (already wrapped and trimmed) to an image."""
    head = 30 if title else 0
    w = cols * cw + pad * 2
    h = rows * ch + pad * 2 + head
    img = Image.new("RGB", (w, h), BG)
    d = ImageDraw.Draw(img)

    if title:
        d.rectangle([0, 0, w, head], fill=(22, 27, 34))
        for i, c in enumerate([(255, 95, 86), (255, 189, 46), (39, 201, 63)]):
            d.ellipse([pad + i * 18, 10, pad + i * 18 + 10, 20], fill=c)
        d.text((pad + 70, 8), title, font=font, fill=DIM)

    y = pad + head
    for ln in lines[-rows:]:
        d.text((pad, y), ln, font=font, fill=_colour_for(ln))
        y += ch
    return img


def terminal_gif(transcript: str, out_path: Path, cols=98, rows=30,
                 font_size=15, title="EDA Agent", hold_ms=1400):
    """Animate a captured transcript as a scrolling terminal session."""
    font = ImageFont.truetype(FONT_REG, font_size)
    bbox = font.getbbox("M")
    cw = font.getlength("M")
    ch = int((bbox[3] - bbox[1]) * 1.9)
    cw = int(cw)
    pad = 14

    text = _subst(_strip_ansi(transcript))
    raw_lines = [ln.rstrip() for ln in text.splitlines()]

    wrapped = []
    for ln in raw_lines:
        wrapped.extend(_wrap(ln, cols))

    # Don't leave a slab of dead space under a short transcript.
    rows = min(rows, len(wrapped) + 1)

    frames, durations = [], []
    shown = []
    for ln in wrapped:
        if ln.strip().startswith(">") and len(ln.strip()) > 2:
            # Type the user's prompt out character by character.
            prefix = ln[:ln.index(">") + 2]
            body = ln[len(prefix):]
            step = max(1, len(body) // 18)
            for i in range(0, len(body) + 1, step):
                frames.append(_render_screen(shown + [prefix + body[:i] + "█"],
                                             cols, rows, font, cw, ch, pad, title))
                durations.append(45)
            shown.append(ln)
            frames.append(_render_screen(shown, cols, rows, font, cw, ch, pad, title))
            durations.append(380)
        else:
            shown.append(ln)
            frames.append(_render_screen(shown, cols, rows, font, cw, ch, pad, title))
            # Blank lines flick past; content lines get a readable beat.
            durations.append(90 if not ln.strip() else 200)

    if frames:
        durations[-1] = hold_ms
        out_path.parent.mkdir(parents=True, exist_ok=True)
        # A terminal uses ~10 colours; quantising to a shared 16-colour palette
        # cuts the file several-fold with no visible change.
        pal = frames[0].convert("P", palette=Image.ADAPTIVE, colors=16)
        frames = [f.quantize(palette=pal, dither=Image.NONE) for f in frames]
        frames[0].save(out_path, save_all=True, append_images=frames[1:],
                       duration=durations, loop=0, optimize=True)
    return out_path, len(frames)


def frames_gif(paths, out_path: Path, ms=1100, last_ms=2200, max_width=1100,
               captions=None):
    """Animate a sequence of screenshots, optionally captioned."""
    font = ImageFont.truetype(FONT_BOLD, 20)
    imgs = []
    for i, p in enumerate(paths):
        im = Image.open(p).convert("RGB")
        if im.width > max_width:
            im = im.resize((max_width, int(im.height * max_width / im.width)),
                           Image.LANCZOS)
        if captions and i < len(captions) and captions[i]:
            bar = 38
            canvas = Image.new("RGB", (im.width, im.height + bar), (22, 27, 34))
            canvas.paste(im, (0, bar))
            d = ImageDraw.Draw(canvas)
            d.text((12, 9), f"{i + 1}/{len(paths)}  {captions[i]}",
                   font=font, fill=(201, 209, 217))
            im = canvas
        imgs.append(im)

    if not imgs:
        return out_path, 0

    # Pad every frame to the largest size so the GIF canvas stays stable.
    w = max(i.width for i in imgs)
    h = max(i.height for i in imgs)
    padded = []
    for im in imgs:
        c = Image.new("RGB", (w, h), (22, 27, 34))
        c.paste(im, ((w - im.width) // 2, (h - im.height) // 2))
        padded.append(c)

    durations = [ms] * len(padded)
    durations[-1] = last_ms
    out_path.parent.mkdir(parents=True, exist_ok=True)
    padded[0].save(out_path, save_all=True, append_images=padded[1:],
                   duration=durations, loop=0, optimize=True)
    return out_path, len(padded)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--terminal", help="path to a captured transcript .txt")
    ap.add_argument("--frames", help="glob of screenshot frames, in order")
    ap.add_argument("--captions", help="'|'-separated captions for --frames")
    ap.add_argument("--title", default="EDA Agent")
    ap.add_argument("--cols", type=int, default=98)
    ap.add_argument("--rows", type=int, default=30)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    out = Path(args.out)
    if args.terminal:
        text = Path(args.terminal).read_text(encoding="utf-8", errors="replace")
        p, n = terminal_gif(text, out, cols=args.cols, rows=args.rows,
                            title=args.title)
    elif args.frames:
        paths = sorted(globmod.glob(args.frames))
        caps = args.captions.split("|") if args.captions else None
        p, n = frames_gif(paths, out, captions=caps)
    else:
        ap.error("need --terminal or --frames")

    print(f"wrote {p} ({n} frames, {p.stat().st_size / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
