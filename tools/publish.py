"""Export a KiCad project and build a phone-friendly static page per board revision.

Plain Python 3.8+, standard library only, so it runs both on this machine (system python)
and inside KiCad's Docker image in CI. The QR on the board is placed by pcb_qr.py (KiCad's
python.exe); this script makes what the QR points at.

usage:
  publish.py build  <project.kicad_pro> --site <dir> --base-url URL [--rev X] [--slug S]
                    [--kicanvas] [--no-render] [--no-fab] [--no-design] [--title T]
      Export into <site>/<slug>/<rev>/ and (re)write the revision page, the board page
      <site>/<slug>/index.html (all revisions found there) and <site>/index.html (all boards).
      Older revision folders are never touched. --no-fab leaves out gerbers and pick-and-place,
      --no-design the KiCad files (and with them KiCanvas).
  publish.py url    <project.kicad_pro> --base-url URL [--rev X] [--slug S]
      Print the stable page URL that goes into the QR: <base-url>/<slug>/<rev>/
  publish.py verify-qr <image.png> --expect URL
      Decode the QR in a render (needs `pip install zxing-cpp pillow`; use a venv), check it
      equals URL and that it is not mirrored as seen in that image. Exit 1 on failure.
  publish.py init-repo <repo-dir> [--kicad-image kicad/kicad:10.0-full]
      Write the GitHub Actions workflow, .nojekyll, a README and copy this script to tools/.

Revision: --rev, else the board's title block (rev "..."), else "dev". The URL uses a slug
of it ("Rev A" -> "rev-a", "A" -> "a"); keep revisions short, a shorter URL is a smaller QR.

kicad-cli: $KICAD_CLI, else on Windows the C:\\Program Files\\KiCad\\<ver> install matching
the board's (generator_version), else `kicad-cli` on PATH.

Per revision it writes: schematic.pdf, schematic SVGs, board-top.svg / board-bottom.svg
(composite), one SVG per copper and silk layer, render-top/bottom/iso.png, bom.csv,
positions.csv (pick and place, both sides), gerbers.zip (gerbers + drill), the design files (for download
and KiCanvas), meta.json and index.html.
"""
import argparse
import datetime as dt
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from urllib.parse import quote
from pathlib import Path

DESIGN_EXT = (".kicad_pro", ".kicad_sch", ".kicad_pcb", ".kicad_dru")
KICANVAS_JS = "https://kicanvas.org/kicanvas/kicanvas.js"


# ---------------------------------------------------------------- helpers
def slugify(s):
    s = re.sub(r"[^a-z0-9.]+", "-", s.lower()).strip("-.")
    return s or "dev"


def sexpr_field(path, name):
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")[:20000]
    except OSError:
        return None
    m = re.search(r"\(%s\s+\"([^\"]*)\"" % re.escape(name), text)
    return m.group(1) if m else None


def project_files(pro):
    pro = Path(pro).resolve()
    if pro.suffix != ".kicad_pro":
        raise SystemExit(f"give the .kicad_pro file, not {pro.name}")
    pcb, sch = pro.with_suffix(".kicad_pcb"), pro.with_suffix(".kicad_sch")
    for f in (pcb, sch):
        if not f.exists():
            raise SystemExit(f"missing {f}")
    return pro, sch, pcb


def find_cli(pcb):
    if os.environ.get("KICAD_CLI"):
        return os.environ["KICAD_CLI"]
    gen = sexpr_field(pcb, "generator_version") or ""
    if os.name == "nt":
        root = Path(r"C:\Program Files\KiCad")
        cands = sorted((p for p in root.glob("*/bin/kicad-cli.exe")), key=lambda p: p.parts[-3], reverse=True)
        for c in cands:
            if gen and c.parts[-3] == gen:
                return str(c)
        if cands:
            return str(cands[0])
    exe = shutil.which("kicad-cli")
    if exe:
        return exe
    raise SystemExit("kicad-cli not found; set KICAD_CLI")


def run(cli, *args, optional=False):
    cmd = [cli, *map(str, args)]
    if optional and os.name != "nt" and not os.environ.get("DISPLAY") and shutil.which("xvfb-run"):
        cmd = ["xvfb-run", "-a", *cmd]                          # 3D render in a headless container
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        msg = f"kicad-cli {' '.join(map(str, args[:3]))} failed ({r.returncode}): {(r.stderr or r.stdout).strip()[-400:]}"
        if optional:
            print("warning: " + msg, file=sys.stderr)
            return False
        raise SystemExit(msg)
    return True


def git_sha(folder):
    try:
        r = subprocess.run(["git", "-C", str(folder), "rev-parse", "--short", "HEAD"], capture_output=True, text=True)
        return r.stdout.strip() or None
    except OSError:
        return None


def resolve(args, need_pcb=True):
    pro, sch, pcb = project_files(args.project)
    rev = args.rev or sexpr_field(pcb, "rev") or "dev"
    slug = args.slug or slugify(pro.stem)
    return pro, sch, pcb, rev, slug, slugify(rev)


def page_url(base, slug, rev_slug):
    return f"{base.rstrip('/')}/{slug}/{rev_slug}/"


# ---------------------------------------------------------------- export
def export(cli, pro, sch, pcb, out, render=True, fab=True, design_files=True):
    out.mkdir(parents=True, exist_ok=True)
    files = {}
    run(cli, "sch", "export", "pdf", "-o", out / "schematic.pdf", sch)
    files["schematic_pdf"] = "schematic.pdf"
    sdir = out / "schematic-svg"
    if sdir.exists():
        shutil.rmtree(sdir)
    sdir.mkdir()
    run(cli, "sch", "export", "svg", "-o", sdir, sch)
    files["schematic_svg"] = sorted(f"schematic-svg/{p.name}" for p in sdir.glob("*.svg"))

    common = ["--page-size-mode", "2", "--exclude-drawing-sheet"]
    run(cli, "pcb", "export", "svg", "--mode-single", "-l", "F.Cu,F.Mask,F.SilkS,Edge.Cuts", *common,
        "-o", out / "board-top.svg", pcb)
    run(cli, "pcb", "export", "svg", "--mode-single", "--mirror", "-l", "B.Cu,B.Mask,B.SilkS,Edge.Cuts", *common,
        "-o", out / "board-bottom.svg", pcb)
    files["board_svg"] = ["board-top.svg", "board-bottom.svg"]
    ldir = out / "layers"
    ldir.mkdir(exist_ok=True)
    layers = []
    names = re.findall(r'\(\d+\s+"([^"]+)"\s+(?:signal|power|mixed|jumper|user)', Path(pcb).read_text(encoding="utf-8", errors="replace")[:20000])
    cu = [n for n in names if n.endswith(".Cu")] or ["F.Cu", "B.Cu"]
    for lay in cu + ["F.SilkS", "B.SilkS"]:
        fn = f"layers/{lay.replace('.', '_')}.svg"
        mirror = ["--mirror"] if lay.startswith("B.") else []
        run(cli, "pcb", "export", "svg", "--mode-single", *mirror, "-l", f"{lay},Edge.Cuts", *common, "-o", out / fn, pcb)
        layers.append(fn)
    files["layer_svg"] = layers
    run(cli, "pcb", "export", "pdf", "--mode-single", "-l", ",".join(cu + ["F.SilkS", "B.SilkS", "Edge.Cuts"]),
        "--cl", "Edge.Cuts", "-o", out / "board.pdf", pcb, optional=True) and files.setdefault("board_pdf", "board.pdf")

    if render:
        r = []
        for name, extra in (("render-top.png", ["--side", "top"]), ("render-bottom.png", ["--side", "bottom"]),
                            ("render-iso.png", ["--rotate", "305,0,30"])):
            if run(cli, "pcb", "render", *extra, "--zoom", "1", "-w", "1600", "-h", "1200", "--quality", "high",
                   "--background", "opaque", "-o", out / name, pcb, optional=True):
                r.append(name)
        files["renders"] = r

    run(cli, "sch", "export", "bom", "--fields", "Reference,Value,Footprint,${QUANTITY},${DNP}",
        "--labels", "Refs,Value,Footprint,Qty,DNP", "--group-by", "Value,Footprint", "-o", out / "bom.csv", sch)
    files["bom"] = "bom.csv"
    if fab:
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            run(cli, "pcb", "export", "gerbers", "-o", str(td) + os.sep, pcb)
            run(cli, "pcb", "export", "drill", "-o", str(td) + os.sep, pcb)
            with zipfile.ZipFile(out / "gerbers.zip", "w", zipfile.ZIP_DEFLATED) as z:
                for f in sorted(td.iterdir()):
                    z.write(f, f.name)
        files["gerbers"] = "gerbers.zip"
        run(cli, "pcb", "export", "pos", "--format", "csv", "--units", "mm", "--side", "both",
            "-o", out / "positions.csv", pcb)
        files["positions"] = ["positions.csv"]

    ddir = out / "design"
    if ddir.exists():
        shutil.rmtree(ddir)
    design = []
    if design_files:
        ddir.mkdir()
    for f in sorted(pro.parent.iterdir()) if design_files else []:
        if f.suffix in DESIGN_EXT and not f.name.startswith(("~", "_autosave")):
            shutil.copy2(f, ddir / f.name)
            design.append(f"design/{f.name}")
    files["design"] = design
    return files


# ---------------------------------------------------------------- pages
CSS = """
:root{--bg:#fafaf8;--fg:#1d1d1b;--mute:#6b6b66;--line:#e2e1dc;--card:#fff;--acc:#0b6e4f}
@media (prefers-color-scheme:dark){:root{--bg:#141413;--fg:#ecebe6;--mute:#a19f97;--line:#2e2d2a;--card:#1c1c1a;--acc:#5cc79b}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:960px;margin:0 auto;padding:16px}h1{font-size:1.5rem;margin:.2em 0}h2{font-size:1.1rem;margin:1.6em 0 .5em}
.meta{color:var(--mute);font-size:.9rem}a{color:var(--acc)}
.grid{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(260px,1fr))}
figure{margin:0;background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden}
figure img{display:block;width:100%;height:auto;background:#fff}figcaption{padding:6px 10px;font-size:.85rem;color:var(--mute)}
ul.links{list-style:none;padding:0;display:flex;flex-wrap:wrap;gap:8px}
ul.links a{display:inline-block;padding:8px 12px;border:1px solid var(--line);border-radius:8px;background:var(--card);text-decoration:none}
table{border-collapse:collapse;width:100%}td,th{border-bottom:1px solid var(--line);padding:6px 4px;text-align:left}
kicanvas-embed{display:block;height:70vh;border:1px solid var(--line);border-radius:10px;overflow:hidden}
.note{color:var(--mute);font-size:.85rem}
"""


def shell(title, body, extra_head=""):
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{html.escape(title)}</title><style>{CSS}</style>{extra_head}</head>'
            f'<body><main>{body}</main></body></html>\n')


def u(path):
    """href/src for a relative file path (spaces in KiCad file names)."""
    return html.escape(quote(path))


def fig(src, cap, link=None):
    link = link or src
    return (f'<figure><a href="{u(link)}"><img loading="lazy" src="{u(src)}" alt="{html.escape(cap)}"></a>'
            f'<figcaption>{html.escape(cap)}</figcaption></figure>')


def rev_page(meta, kicanvas):
    f = meta["files"]
    e = html.escape
    body = [f'<p class="meta"><a href="../">{e(meta["title"])} — all revisions</a></p>',
            f'<h1>{e(meta["title"])}</h1>',
            f'<p class="meta">Revision <b>{e(meta["rev"])}</b> · built {e(meta["built"])}'
            + (f' · commit {e(meta["commit"])}' if meta.get("commit") else "") + "</p>"]
    if f.get("renders"):
        caps = {"render-top.png": "Top", "render-bottom.png": "Bottom", "render-iso.png": "3D view"}
        body.append('<div class="grid">' + "".join(fig(r, caps.get(r, r)) for r in f["renders"]) + "</div>")
    body.append("<h2>Schematic</h2>")
    body.append('<div class="grid">' + "".join(fig(s, f"Sheet {i + 1}") for i, s in enumerate(f["schematic_svg"])) + "</div>")
    body.append(f'<ul class="links"><li><a href="{f["schematic_pdf"]}">Schematic PDF</a></li></ul>')
    body.append("<h2>Layout</h2>")
    body.append('<div class="grid">' + fig("board-top.svg", "Top (copper, mask, silk)")
                + fig("board-bottom.svg", "Bottom, seen from below") + "</div>")
    body.append('<ul class="links">' + "".join(
        f'<li><a href="{u(p)}">{e(Path(p).stem.replace("_", "."))}</a></li>' for p in f["layer_svg"])
        + (f'<li><a href="{f["board_pdf"]}">Board PDF</a></li>' if f.get("board_pdf") else "") + "</ul>")
    if kicanvas:
        sch = next((d for d in f["design"] if d.endswith(".kicad_sch")), None)
        pcb = next((d for d in f["design"] if d.endswith(".kicad_pcb")), None)
        body.append("<h2>Interactive viewer</h2>")
        body.append('<p class="note">KiCanvas (alpha, third-party) — pinch to zoom; if it stays blank, use the images above.</p>')
        for src in (sch, pcb):
            if src:
                body.append(f'<kicanvas-embed src="{u(src)}" controls="basic"></kicanvas-embed><p></p>')
    body.append("<h2>Files</h2>")
    links = [(f["bom"], "BOM (CSV)")]
    links += [(p, "Pick and place (CSV)") for p in f.get("positions", [])]
    if f.get("gerbers"):
        links.append((f["gerbers"], "Gerbers + drill (zip)"))
    links += [(d, Path(d).name) for d in f["design"]]
    body.append('<ul class="links">' + "".join(f'<li><a href="{u(h)}">{e(t)}</a></li>' for h, t in links) + "</ul>")
    head = f'<script type="module" src="{KICANVAS_JS}"></script>' if kicanvas else ""
    return shell(f'{meta["title"]} rev {meta["rev"]}', "".join(body), head)


def read_metas(board_dir):
    metas = []
    for m in board_dir.glob("*/meta.json"):
        try:
            metas.append(json.loads(m.read_text(encoding="utf-8")))
        except ValueError:
            pass
    return sorted(metas, key=lambda m: m.get("built", ""), reverse=True)


def board_page(board_dir):
    metas = read_metas(board_dir)
    title = metas[0]["title"] if metas else board_dir.name
    e = html.escape
    rows = "".join(f'<tr><td><a href="{e(m["rev_slug"])}/">{e(m["rev"])}</a></td><td>{e(m["built"])}</td>'
                   f'<td>{e(m.get("commit") or "")}</td></tr>' for m in metas)
    body = (f'<p class="meta"><a href="../">All boards</a></p><h1>{e(title)}</h1>'
            + (fig(f'{metas[0]["rev_slug"]}/render-iso.png', f'Latest: rev {metas[0]["rev"]}', f'{metas[0]["rev_slug"]}/')
               if metas and "render-iso.png" in metas[0]["files"].get("renders", []) else "")
            + f"<h2>Revisions</h2><table><tr><th>Rev</th><th>Built</th><th>Commit</th></tr>{rows}</table>")
    (board_dir / "index.html").write_text(shell(title, body), encoding="utf-8")


def site_page(site):
    e = html.escape
    items = []
    for d in sorted(p for p in site.iterdir() if p.is_dir() and (p / "index.html").exists()):
        metas = read_metas(d)
        if metas:
            items.append(f'<tr><td><a href="{e(d.name)}/">{e(metas[0]["title"])}</a></td>'
                         f'<td>{e(metas[0]["rev"])}</td><td>{len(metas)}</td></tr>')
    body = ("<h1>Boards</h1><table><tr><th>Board</th><th>Latest rev</th><th>Revisions</th></tr>"
            + "".join(items) + "</table>")
    (site / "index.html").write_text(shell("Boards", body), encoding="utf-8")
    (site / ".nojekyll").write_text("", encoding="utf-8")


def cmd_build(a):
    pro, sch, pcb, rev, slug, rev_slug = resolve(a)
    cli = find_cli(pcb)
    site = Path(a.site).resolve()
    out = site / slug / rev_slug
    files = export(cli, pro, sch, pcb, out, render=not a.no_render, fab=not a.no_fab, design_files=not a.no_design)
    meta = {"title": a.title or sexpr_field(pcb, "title") or pro.stem, "board": pro.stem, "slug": slug,
            "rev": rev, "rev_slug": rev_slug, "url": page_url(a.base_url, slug, rev_slug) if a.base_url else None,
            "built": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "commit": git_sha(pro.parent), "kicad": sexpr_field(pcb, "generator_version"), "files": files}
    (out / "meta.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
    (out / "index.html").write_text(rev_page(meta, a.kicanvas and not a.no_design), encoding="utf-8")
    board_page(site / slug)
    site_page(site)
    print(json.dumps({"page": str(out / "index.html"), "url": meta["url"], "files": sum(
        len(v) if isinstance(v, list) else 1 for v in files.values())}, indent=1))


def cmd_url(a):
    _, _, _, _, slug, rev_slug = resolve(a)
    print(page_url(a.base_url, slug, rev_slug))


# ---------------------------------------------------------------- QR verification
def _format_dist(m):
    def bch(d):
        v = d << 10
        for i in range(14, 9, -1):
            if v >> i & 1:
                v ^= 0x537 << (i - 10)
        return ((d << 10) | v) ^ 0x5412
    pos = [(8, 0), (8, 1), (8, 2), (8, 3), (8, 4), (8, 5), (8, 7), (8, 8), (7, 8), (5, 8), (4, 8), (3, 8), (2, 8), (1, 8), (0, 8)]
    v = 0
    for r, c in pos:
        v = v << 1 | int(m[r][c])
    return min(bin(v ^ bch(d)).count("1") for d in range(32))


def cmd_verify_qr(a):
    try:
        import zxingcpp
        from PIL import Image
    except ImportError:
        raise SystemExit("needs zxing-cpp and pillow: python -m venv .venv && .venv/Scripts/pip install zxing-cpp pillow")
    im = Image.open(a.image).convert("L")
    res = zxingcpp.read_barcodes(im)
    qrs = [r for r in res if "QR" in r.format.name]
    if not qrs:
        raise SystemExit(f"FAIL: no QR code decoded in {a.image}")
    r = qrs[0]
    ok_text = (a.expect is None) or (r.text == a.expect)
    # mirror check: zxing reads mirrored codes too, phones may not. Sample the module grid in
    # image orientation and test the format-information bits (valid only if not mirrored).
    cs = (r.position.top_left, r.position.top_right, r.position.bottom_right, r.position.bottom_left)
    x0, x1 = min(c.x for c in cs), max(c.x for c in cs) + 1
    y0, y1 = min(c.y for c in cs), max(c.y for c in cs) + 1
    vals = sorted(im.getpixel((x, y)) for x in range(x0, x1, 3) for y in range(y0, y1, 3))
    thr = (vals[len(vals) // 10] + vals[-len(vals) // 10]) / 2
    finder = [[1 if (i in (0, 6) or j in (0, 6) or (2 <= i <= 4 and 2 <= j <= 4)) else 0 for j in range(7)] for i in range(7)]
    best = None
    for v in range(1, 41):
        n = 17 + 4 * v
        g = [[im.getpixel((int(x0 + (j + .5) * (x1 - x0) / n), int(y0 + (i + .5) * (y1 - y0) / n))) < thr
              for j in range(n)] for i in range(n)]
        sc = sum(g[i + p][j + q] == finder[i][j] for p, q in ((0, 0), (0, n - 7), (n - 7, 0)) for i in range(7) for j in range(7))
        if best is None or sc > best[0]:
            best = (sc, n, g)
    _, n, g = best
    rots = [g]
    for _ in range(3):
        rots.append([list(row) for row in zip(*rots[-1][::-1])])
    normal = min(_format_dist(x) for x in rots)
    mirrored = min(_format_dist([row[::-1] for row in x]) for x in rots)
    print(json.dumps({"decoded": r.text, "expected": a.expect, "match": ok_text, "modules": n,
                      "format_bits_normal": normal, "format_bits_mirrored": mirrored,
                      "mirrored": mirrored < normal}, indent=1))
    if not ok_text:
        raise SystemExit("FAIL: decoded text differs from --expect")
    if mirrored < normal:
        raise SystemExit("FAIL: the code is mirrored in this view; a phone may not read it")
    print("PASS")


# ---------------------------------------------------------------- repo scaffold
WORKFLOW = """\
# Rebuilds the board pages when design files change. Generated by /kicad publish (publish.py init-repo).
# Pages source: branch main, folder /docs. Every board folder under boards/ with a .kicad_pro
# is built into docs/<board-slug>/<rev>/; older revision folders already in docs/ stay.
name: kicad-publish
on:
  push:
    branches: [main]
    paths: ["boards/**", "tools/publish.py", ".github/workflows/kicad-publish.yml"]
  workflow_dispatch:
permissions:
  contents: write
concurrency:
  group: kicad-publish
  cancel-in-progress: false
jobs:
  build:
    runs-on: ubuntu-latest
    container:
      image: __IMAGE__
      options: --user root
    steps:
      - uses: actions/checkout@v4
      - name: Build pages
        shell: bash
        run: |
          git config --global --add safe.directory "$GITHUB_WORKSPACE"
          (command -v xvfb-run >/dev/null) || (apt-get update -qq && apt-get install -y -qq xvfb xauth >/dev/null) || true
          owner=$(echo "${{ github.repository_owner }}" | tr A-Z a-z)
          base="https://${owner}.github.io/${{ github.event.repository.name }}"
          shopt -s nullglob
          for pro in boards/*/*.kicad_pro; do
            python3 tools/publish.py build "$pro" --site docs --base-url "$base" $(cat "$(dirname "$pro")/publish.args" 2>/dev/null)
          done
      - name: Commit pages
        shell: bash
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
          git add docs
          git diff --cached --quiet && exit 0
          git commit -m "publish: rebuild board pages [skip ci]"
          git push
"""

README = """\
# Board pages

Each board's schematic, layout, renders, BOM and fabrication files, one page per revision,
served by GitHub Pages (Settings → Pages → Deploy from branch → main /docs). The QR code on
the bottom of each board points at `https://<owner>.github.io/<repo>/<board-slug>/<rev>/`.

- `boards/<name>/` — KiCad design files (`.kicad_pro/.kicad_sch/.kicad_pcb`, `.kicad_dru`).
  Optional `publish.args` with extra flags, e.g. `--kicanvas` or `--rev B`.
- `docs/` — the generated site; committed so older revisions keep their pages.
- `tools/publish.py` — builds it (`python tools/publish.py --help`). Locally:
  `python tools/publish.py build boards/<name>/<name>.kicad_pro --site docs --base-url https://<owner>.github.io/<repo>`
- `.github/workflows/kicad-publish.yml` — rebuilds `docs/` with KiCad's Docker image on every push
  that changes `boards/`. To start a new revision, change the title-block revision in the board
  (File → Board Setup → Title block, or `(rev "B")`) and push; the old revision's folder stays.

Everything here is public if the repository is public (free GitHub Pages requires that).
"""


def cmd_init_repo(a):
    repo = Path(a.repo).resolve()
    (repo / ".github" / "workflows").mkdir(parents=True, exist_ok=True)
    (repo / ".github" / "workflows" / "kicad-publish.yml").write_text(WORKFLOW.replace("__IMAGE__", a.kicad_image), encoding="utf-8")
    (repo / "tools").mkdir(exist_ok=True)
    shutil.copy2(Path(__file__), repo / "tools" / "publish.py")
    (repo / "docs").mkdir(exist_ok=True)
    (repo / "docs" / ".nojekyll").write_text("", encoding="utf-8")
    (repo / "boards").mkdir(exist_ok=True)
    if not (repo / "README.md").exists():
        (repo / "README.md").write_text(README, encoding="utf-8")
    gi = repo / ".gitignore"
    if not gi.exists():
        gi.write_text("*-backups/\n~*.lck\n_autosave-*\n*.kicad_prl\nfp-info-cache\nclaude-checks/\n.venv/\n", encoding="utf-8")
    print(f"scaffolded {repo}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("build", "url"):
        p = sub.add_parser(name)
        p.add_argument("project")
        p.add_argument("--base-url", required=(name == "url"))
        p.add_argument("--rev")
        p.add_argument("--slug")
        if name == "build":
            p.add_argument("--site", required=True)
            p.add_argument("--title")
            p.add_argument("--kicanvas", action="store_true")
            p.add_argument("--no-render", action="store_true")
            p.add_argument("--no-fab", action="store_true")
            p.add_argument("--no-design", action="store_true")
    p = sub.add_parser("verify-qr")
    p.add_argument("image")
    p.add_argument("--expect")
    p = sub.add_parser("init-repo")
    p.add_argument("repo")
    p.add_argument("--kicad-image", default="kicad/kicad:10.0-full")
    a = ap.parse_args()
    {"build": cmd_build, "url": cmd_url, "verify-qr": cmd_verify_qr, "init-repo": cmd_init_repo}[a.cmd](a)


if __name__ == "__main__":
    main()
