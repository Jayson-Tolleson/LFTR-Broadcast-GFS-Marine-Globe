# Optional runtime models

Binary GLB assets are intentionally not committed because the GitHub PR/upload path for this repository rejects binary files.

The boater layer renders SVG glyphs by default. If you host a GLB externally or provide it during deployment, set `window.GFS_BOAT_MODEL_SRC` to that URL before loading `static/js/gfs/boats.js`.
