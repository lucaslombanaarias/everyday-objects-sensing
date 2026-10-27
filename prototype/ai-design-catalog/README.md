# HomeSense Design Catalog — Local full-stack development package

**Location in your GitHub repository:** `prototype/ai-design-catalog/`. This package upgrades the previous `index.html` prototype with a working SQLite backend and curator admin. No third-party Python packages are required.

## Run locally (Windows PowerShell)

1. Install Python 3.11+ if necessary.
2. Open a terminal in this folder.
3. Set a strong temporary admin token in your terminal: `$env:ADMIN_TOKEN = "replace-with-at-least-24-random-characters"`.
4. Optional: set `$env:GEMINI_API_KEY = "your-key"` and `$env:GEMINI_MODEL = "gemini-2.5-flash"` (check current model availability/free-tier policy).
5. Run `python server.py`.
6. Open `http://127.0.0.1:8080` and `http://127.0.0.1:8080/admin.html`.

On macOS/Linux, use `export ADMIN_TOKEN='...'` instead of `$env:`.

## Real CAD catalog workflow

1. Go to `/admin.html`, paste your terminal's admin token, and select a CAD file or thumbnail (STL, STEP, STP, OBJ, PNG, JPG, WEBP; 5 MB max).
2. Click Upload; copy the returned `/uploads/...` path into Model file URL or Thumbnail URL.
3. Complete metadata and save as **draft** while unverified.
4. Set status to **active** only after approval and compatibility checks. Only active records are visible in the public catalog and AI context.
5. All design records are stored in `data/catalog.sqlite3`; files are stored in `uploads/`. Both are excluded from Git. Back up these directories separately. Do not add licensed CAD files to public GitHub without permission.

## API

- `GET /api/health` — server status
- `GET /api/designs` — active designs only
- `GET /api/admin/designs` — all records (admin token)
- `POST /api/admin/designs` — upsert design JSON (admin token)
- `POST /api/admin/upload` — JSON `{extension,base64}` (admin token)
- `DELETE /api/admin/designs/<slug>` — remove record (admin token)
- `POST /api/ai/chat` — `{message}` (Gemini API key optional)

## Security / deployment limitations

**Development-only backend. Do not expose `server.py` to the public internet as-is.** Admin uses a shared secret, no per-user identity, audit log, TLS termination, or persistent distributed rate limiter. Files are stored on local disk, not durable cloud storage. Server binds to `127.0.0.1` deliberately. Production requires proper authenticated admin accounts, HTTPS, security review, durable storage, backups, abuse controls, and an approved hosting environment.

No room photo is sent to the backend. The existing photo UI remains local-only pending the sponsor's privacy and retention decisions. AI recommendations are restricted by prompt and catalog context, but generative output still needs human review; deterministic eligibility validation should be added before deployment. The free Gemini API tier may have data-use conditions unsuitable for personal room photos; get sponsor approval before implementing photo processing.

GitHub Pages hosts static files only and **cannot run this Python backend**. Continue local testing while university hosting is being determined. The public catalog in the previous prototype will show sample designs when opened as `file://`; running via `server.py` shows only approved backend entries.

## Remaining production work

- Replace shared-token admin with university-approved authenticated accounts and audit trail.
- Confirm hosting, AI provider policy, photo consent/deletion, model license/redistribution rights, and maintenance ownership.
- Add CAD 3D preview (current implementation offers model downloads and thumbnails).
- Persist plans in a server database only if explicitly approved; browser localStorage works today.
- Validate user-supplied files more deeply than extension and size before public exposure.
- Add integration/accessibility/security tests and real design metadata.

## Verification

Run `python -m unittest -v test_server` from this directory. Tests use an isolated temporary database and localhost server; no AI credentials or external calls are needed.
