#!/usr/bin/env python3
import os
import csv
import re
import html
import html.parser
import urllib.request
import urllib.parse

# ---------- paths (use the directory you run it from) ----------
BASEDIR = os.getcwd()
URLFILE = os.path.join(BASEDIR, "guardian_urls.txt")
OUTDIR  = os.path.join(BASEDIR, "guardian_images")
LOGCSV  = os.path.join(BASEDIR, "guardian_log.csv")
os.makedirs(OUTDIR, exist_ok=True)

print(f"[pwd] {BASEDIR}\n[in ] {URLFILE}\n[out] {OUTDIR}\n[log] {LOGCSV}")

# ---------- HTTP ----------
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome Safari"
)

def fetch(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        charset = r.headers.get_content_charset() or "utf-8"
        return r.read().decode(charset, errors="ignore")

# ---------- helpers ----------
IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
SRCSET_SPLIT = re.compile(r"\s*,\s*")
CANDIDATE    = re.compile(r"^\s*(\S+)\s+(\d+)(w|x)?\s*$")

def best_from_srcset(srcset, base_url):
    """Return the largest candidate URL (by width/x) from a srcset string."""
    if not srcset:
        return None
    best_url, best_size = None, -1
    for part in SRCSET_SPLIT.split(srcset.strip()):
        m = CANDIDATE.match(part)
        if m:
            u, size = m.group(1), int(m.group(2))
            if size > best_size:
                best_size  = size
                best_url   = urllib.parse.urljoin(base_url, u)
        else:
            p = part.strip()
            if p:
                return urllib.parse.urljoin(base_url, p)
    return best_url

def safe_ext(u: str) -> str:
    ext = os.path.splitext(urllib.parse.urlparse(u).path)[1].lower()
    return ext if ext in IMG_EXTS else ".jpg"

def keep_guardian_cdn(u: str) -> bool:
    try:
        return urllib.parse.urlparse(u).netloc.lower().endswith("i.guim.co.uk")
    except:
        return False

def upgrade_guardian_url(url: str, width: int = 2000) -> str:
    """
    If the Guardian image URL is SIGNED (contains &s=...), DO NOT modify it.
    Otherwise (rare, unsigned), it's safe to tweak width/dpr.
    """
    if "i.guim.co.uk" not in url:
        return url
    parsed = urllib.parse.urlparse(url)
    if "s=" in (parsed.query or "").lower():
        return url  # signed; leave untouched
    # unsigned fallback (rare)
    q = urllib.parse.parse_qs(parsed.query)
    q["width"] = [str(width)]
    q["dpr"]   = ["2"]
    new_q = urllib.parse.urlencode({k: v[0] for k, v in q.items()})
    return urllib.parse.urlunparse(parsed._replace(query=new_q))

# ---------- HTML parser (article + lightbox, with noscript fallback) ----------
class GuardianParser(html.parser.HTMLParser):
    """
    Collects image URLs from:
      - <main><article>… (figures/pictures/imgs inside the article)
      - the lightbox dialog (id="gu-lightbox", role="dialog")
    Prefers largest candidate from srcset; filters to i.guim.co.uk; preserves signed URLs.
    """
    def __init__(self, base_url: str):
        super().__init__()
        self.base = base_url
        self.images = []
        self._seen  = set()

        # region state
        self.in_main = 0
        self.in_article = 0
        self.in_lightbox = 0
        self.in_figure = 0
        self.in_picture = 0
        self.in_noscript = 0

        # track best candidate inside a <picture>
        self.picture_best = None  # (url, size_hint)

    def _consider(self, url: str, size_hint: int = -1):
        if not url:
            return
        url = urllib.parse.urljoin(self.base, url)
        if not url.startswith(("http://", "https://")):
            return
        if not keep_guardian_cdn(url):
            return
        # never break signatures
        url = upgrade_guardian_url(url)
        if url not in self._seen:
            self._seen.add(url)
            self.images.append(url)

    def _pick_from_attrs(self, attrs: dict):
        # prefer srcset/data-srcset for largest
        for k in ("srcset", "data-srcset"):
            if attrs.get(k):
                u = best_from_srcset(attrs[k], self.base)
                if u:
                    return u, 1
        # then src/data-src
        for k in ("src", "data-src"):
            if attrs.get(k):
                return attrs[k], 0
        return None, -1

    def _commit_picture_best(self):
        if self.picture_best:
            self._consider(self.picture_best[0], self.picture_best[1])
            self.picture_best = None

    def handle_starttag(self, tag, attrs_list):
        attrs = dict(attrs_list)

        if tag == "main":
            self.in_main += 1
        elif tag == "article" and self.in_main:
            self.in_article += 1
        elif tag == "div" and (attrs.get("id") == "gu-lightbox" or attrs.get("role") == "dialog"):
            self.in_lightbox += 1

        if tag == "figure" and (self.in_article or self.in_lightbox):
            self.in_figure += 1

        if tag == "picture" and (self.in_figure or self.in_lightbox):
            self.in_picture += 1
            self.picture_best = None

        if tag == "noscript" and (self.in_figure or self.in_lightbox):
            self.in_noscript += 1

        # capture <img>/<source> only inside article figures or lightbox
        if (self.in_figure or self.in_lightbox) and tag in ("img", "source"):
            u, sz = self._pick_from_attrs(attrs)
            if not u:
                return
            # if inside <picture>, keep only the best for that picture
            if self.in_picture:
                # crude size hint: prefer srcset over src
                size_hint = 2 if ("srcset" in attrs or "data-srcset" in attrs) else 1
                if not self.picture_best or size_hint > self.picture_best[1]:
                    self.picture_best = (u, size_hint)
            else:
                self._consider(u)

        # also handle inline styles occasionally used for background-image
        if (self.in_figure or self.in_lightbox) and "style" in attrs:
            m = re.search(r'url\((["\']?)(https?://i\.guim\.co\.uk/[^)]+)\1\)', attrs["style"])
            if m:
                self._consider(m.group(2))

    def handle_endtag(self, tag):
        if tag == "picture" and self.in_picture:
            self._commit_picture_best()
            self.in_picture -= 1

        if tag == "noscript" and self.in_noscript:
            self.in_noscript -= 1

        if tag == "figure" and self.in_figure:
            self.in_figure -= 1

        if tag == "article" and self.in_article:
            self.in_article -= 1

        if tag == "main" and self.in_main:
            self.in_main -= 1

        if tag == "div" and self.in_lightbox:
            self.in_lightbox -= 1

    def handle_data(self, data):
        # noscript <img src="…"> fallbacks inside figures/lightbox
        if (self.in_figure or self.in_lightbox) and self.in_noscript and data.strip():
            txt = html.unescape(data)
            for m in re.finditer(r'<img[^>]+src=["\']([^"\']+)["\']', txt, re.I):
                self._consider(m.group(1))

def extract_guardian_images(page_url: str, html_text: str):
    p = GuardianParser(page_url)
    p.feed(html_text)
    return p.images

# ---------- IO ----------
def save_image(url: str, idx: int) -> str:
    name = f"guardian_{idx}{safe_ext(url)}"
    path = os.path.join(OUTDIR, name)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=30) as r, open(path, "wb") as f:
            f.write(r.read())
        print(f"[OK] {name} <- {url}")
    except Exception as e:
        print(f"[x]  {url} :: {e}")
    return name

def load_urls(path: str):
    if not os.path.isfile(path):
        print(f"[!] URL file not found: {path}")
        return []
    with open(path, "r", encoding="utf-8") as f:
        return [u.strip() for u in f if u.strip() and "theguardian.com" in u]

# ---------- main ----------
def main():
    urls = load_urls(URLFILE)
    if not urls:
        print("[!] No Guardian URLs found in file."); return

    global_idx = 0
    rows = []
    max_imgs_any = 0

    for u in urls:
        print(f"\n[fetch] {u}")
        try:
            html_text = fetch(u)
        except Exception as e:
            print(f"[!] fetch failed: {e}")
            rows.append([u, 0])
            continue

        img_urls = extract_guardian_images(u, html_text)
        print(f"[info] images found: {len(img_urls)}")

        names = []
        for img_u in img_urls:
            global_idx += 1
            names.append(save_image(img_u, global_idx))

        max_imgs_any = max(max_imgs_any, len(names))
        rows.append([u, len(names), *names])

    # write CSV log (pad columns)
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
