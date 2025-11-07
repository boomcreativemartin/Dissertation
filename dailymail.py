#!/usr/bin/env python3
"""
Daily Mail scraper (image-wrap only)

- Input: dailymail_urls.txt (one URL per line, # for comments)
- Scope: ONLY images that are descendants of <div class="image-wrap"> ... </div>
- Variant: choose the largest from srcset/data-srcset (including <picture><source>), else fallback
- Save: dailymail_1.jpg, dailymail_2.webp, ... (sequential across all URLs, extensions preserved; default .jpg)
- Log: dailymail_log.csv with columns: URL, Number of Images, Image 1 Name, Image 2 Name, ...

Usage:
  python3 dailymail_imagewrap.py
"""

import os, sys, csv, re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen, urlretrieve

# ---------- paths (current working directory) ----------
BASEDIR = os.getcwd()
URLFILE = os.path.join(BASEDIR, "dailymail_urls.txt")
OUTDIR  = os.path.join(BASEDIR, "dailymail_images")
LOGCSV  = os.path.join(BASEDIR, "dailymail_log.csv")
os.makedirs(OUTDIR, exist_ok=True)

print(f"[pwd] {BASEDIR}\n[in ] {URLFILE}\n[out] {OUTDIR}\n[log] {LOGCSV}")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome Safari")

IMG_EXT_RE = re.compile(r'\.(jpg|jpeg|png|webp)(?:$|\?)', re.IGNORECASE)
SIZE_IN_NAME_RE = re.compile(r'(?<!\d)(\d{3,5})[xX](\d{3,5})(?!\d)')  # e.g., 1200x800

def _parse_srcset(srcset: str):
    """Return list of (url, width, density) from a srcset string."""
    out = []
    if not srcset:
        return out
    for part in srcset.split(','):
        token = part.strip()
        if not token:
            continue
        bits = token.split()
        url = bits[0]
        width = None
        density = None
        if len(bits) > 1:
            desc = bits[1].lower()
            if desc.endswith('w'):
                try:
                    width = int(desc[:-1])
                except ValueError:
                    width = None
            elif desc.endswith('x'):
                try:
                    density = float(desc[:-1])
                except ValueError:
                    density = None
        out.append((url, width, density))
    return out

def _score_url(u: str):
    """Heuristic when no descriptors: prefer bigger WxH in filename; else longer URL."""
    m = SIZE_IN_NAME_RE.search(u)
    if m:
        w = int(m.group(1)); h = int(m.group(2))
        return w * h
    return len(u)

def _pick_largest(candidates, base_url: str) -> str | None:
    """Pick the best absolute URL from candidates"""
    if not candidates:
        return None
    with_w = [c for c in candidates if c[1] is not None]
    if with_w:
        url, _, _ = max(with_w, key=lambda c: c[1])
        return urljoin(base_url, url)
    with_x = [c for c in candidates if c[2] is not None]
    if with_x:
        url, _, _ = max(with_x, key=lambda c: c[2])
        return urljoin(base_url, url)
    url = max((c[0] for c in candidates), key=_score_url)
    return urljoin(base_url, url)

def _ext_from_url(u: str) -> str:
    path = urlparse(u).path
    name = os.path.basename(path)
    if name:
        m = IMG_EXT_RE.search(name)
        if m:
            ext = m.group(1).lower()
            if ext == "jpeg":
                ext = "jpg"
            return "." + ext
    return ".jpg"

class DMImageWrapParser(HTMLParser):
    """
    Only collect images while inside <div class="image-wrap"> (nested allowed).
    Capture <picture><source> srcsets to inform the subsequent <img> choice.
    """
    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.imgs: list[str] = []
        self.stack_in_wrap: list[bool] = []
        self.stack_in_picture: list[bool] = []
        self.current_picture_srcsets: list[tuple[str, int|None, float|None]] = []

    def handle_starttag(self, tag, attrs):
        tl = tag.lower()
        at = dict(attrs)

        if tl == 'div':
            cls = at.get('class', '') or ''
            # class may contain multiple; match token
            classes = cls.split()
            in_wrap = 'image-wrap' in classes
            self.stack_in_wrap.append(in_wrap or (self.stack_in_wrap[-1] if self.stack_in_wrap else False))

        elif tl == 'picture':
            self.stack_in_picture.append(True)
            self.current_picture_srcsets = []

        elif tl == 'source' and (self.stack_in_picture[-1] if self.stack_in_picture else False):
            ss = at.get('srcset') or at.get('data-srcset')
            if ss:
                self.current_picture_srcsets.extend(_parse_srcset(ss))

        elif tl == 'img':
            in_wrap_now = self.stack_in_wrap[-1] if self.stack_in_wrap else False
            if not in_wrap_now:
                return

            candidates = []
            # from surrounding <picture><source>
            if self.stack_in_picture and self.current_picture_srcsets:
                candidates.extend(self.current_picture_srcsets)

            # from img srcset attributes
            for key in ('srcset', 'data-srcset'):
                if at.get(key):
                    candidates.extend(_parse_srcset(at[key]))

            # fallback single URLs
            for key in ('data-src', 'data-original', 'data-image', 'src'):
                if at.get(key):
                    candidates.append((at[key], None, None))

            if not candidates:
                return

            best = _pick_largest(candidates, self.base_url)
            if not best:
                return

            self.imgs.append(best)

    def handle_endtag(self, tag):
        tl = tag.lower()
        if tl == 'div':
            if self.stack_in_wrap:
                self.stack_in_wrap.pop()
        elif tl == 'picture':
            if self.stack_in_picture:
                self.stack_in_picture.pop()
            self.current_picture_srcsets = []

def fetch_html(url: str) -> str:
    req = Request(url, headers={'User-Agent': UA})
    with urlopen(req, timeout=60) as resp:
        return resp.read().decode('utf-8', errors='replace')

def extract_image_urls(url: str) -> list[str]:
    html_text = fetch_html(url)
    parser = DMImageWrapParser(url)
    parser.feed(html_text)
    # de-duplicate preserving order
    seen = set()
    out = []
    for u in parser.imgs:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out

def download_images_sequential(urls: list[str], outdir: str, start_index: int):
    saved = []
    idx = start_index
    for u in urls:
        ext = _ext_from_url(u)
        fname = f"dailymail_{idx}{ext}"
        full = os.path.join(outdir, fname)
        try:
            urlretrieve(u, full)
            saved.append(fname)
            idx += 1
        except Exception as e:
            print(f"[warn] failed {u}: {e}")
    return saved, idx

def main():
    if not os.path.exists(URLFILE):
        print(f"[error] URLs file not found: {URLFILE}")
        sys.exit(1)

    with open(URLFILE, "r", encoding="utf-8") as f:
        urls = [line.strip() for line in f if line.strip() and not line.strip().startswith('#')]

    rows = []
    max_imgs_any = 0
    global_index = 1

    for u in urls:
        print(f"\n[fetch] {u}")
        try:
            img_urls = extract_image_urls(u)
        except Exception as e:
            print(f"[error] {u}: {e}")
            img_urls = []

        print(f"[found] {len(img_urls)} images in image-wrap")
        saved, global_index = download_images_sequential(img_urls, OUTDIR, global_index)
        print(f"[saved] {len(saved)} images")

        max_imgs_any = max(max_imgs_any, len(saved))
        rows.append([u, len(saved), *saved])

    # write CSV log
    header = ["URL", "Number of Images"] + [f"Image {i} Name" for i in range(1, max_imgs_any + 1)]
    with open(LOGCSV, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        for r in rows:
            r = r + [""] * (2 + max_imgs_any - len(r))
            w.writerow(r)

    print(f"\n[done] wrote log: {LOGCSV}")

if __name__ == "__main__":
    main()
