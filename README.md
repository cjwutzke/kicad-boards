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
