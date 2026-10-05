"""Web app: connect an Etsy shop, keep it synced, generate listings with Claude, push them to Etsy."""

import asyncio
import csv
import io
import json
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import etsy, rules, services
from .config import Settings, load_settings
from .db import DB
from .generator import ClaudeGenerator, GenerationError

BASE = Path(__file__).parent
ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}


def create_app(settings: Settings | None = None, generator=None) -> FastAPI:
    settings = settings or load_settings()
    db = DB(settings.database_path)
    Path(settings.upload_dir).mkdir(parents=True, exist_ok=True)

    def get_generator():
        if generator is not None:
            return generator
        if not settings.claude_configured:
            return None
        if not hasattr(app.state, "claude"):
            app.state.claude = ClaudeGenerator(settings)
        return app.state.claude

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task = None
        if settings.background_sync and settings.sync_interval_minutes > 0:
            task = asyncio.create_task(services.sync_loop(settings, db, get_generator))
        yield
        if task:
            task.cancel()

    app = FastAPI(title="Listing Generator", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
    templates = Jinja2Templates(directory=BASE / "templates")
    templates.env.filters["fromjson"] = json.loads
    templates.env.filters["ago"] = _ago
    app.state.settings = settings
    app.state.db = db

    def render(request: Request, name: str, **ctx):
        ctx.setdefault("flash", request.session.pop("flash", None))
        ctx["settings"] = settings
        return templates.TemplateResponse(request, name, ctx)

    def flash(request: Request, message: str, kind: str = "ok"):
        request.session["flash"] = {"message": message, "kind": kind}

    def require_generator():
        gen = get_generator()
        if gen is None:
            raise GenerationError("Set ANTHROPIC_API_KEY to generate listings.")
        return gen

    @app.middleware("http")
    async def password_gate(request: Request, call_next):
        open_paths = ("/login", "/static", "/healthz")
        if settings.app_password and not request.url.path.startswith(open_paths):
            if not request.session.get("authed"):
                return RedirectResponse("/login", status_code=303)
        return await call_next(request)

    # Added after the gate so it wraps it (the gate reads the session).
    app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, same_site="lax")

    # Auth ------------------------------------------------------------------
    @app.get("/login", response_class=HTMLResponse)
    async def login_form(request: Request):
        return render(request, "login.html")

    @app.post("/login")
    async def login(request: Request, password: str = Form(...)):
        if settings.app_password and secrets.compare_digest(password, settings.app_password):
            request.session["authed"] = True
            return RedirectResponse("/", status_code=303)
        flash(request, "Wrong password.", "error")
        return RedirectResponse("/login", status_code=303)

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    # Dashboard & OAuth -----------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        return render(request, "index.html", shops=db.shops())

    @app.get("/connect")
    async def connect(request: Request):
        if not settings.etsy_configured:
            flash(request, "Add your ETSY_API_KEY to .env first.", "error")
            return RedirectResponse("/", status_code=303)
        verifier, challenge = etsy.make_pkce()
        state = secrets.token_urlsafe(24)
        db.run("INSERT INTO oauth_states (state, verifier, created_at) VALUES (?, ?, ?)", (state, verifier, time.time()))
        return RedirectResponse(etsy.authorize_url(settings, state, challenge), status_code=303)

    @app.get("/oauth/callback")
    async def oauth_callback(request: Request, code: str = "", state: str = "", error: str = ""):
        row = db.one("SELECT * FROM oauth_states WHERE state=?", (state,))
        db.run("DELETE FROM oauth_states WHERE state=? OR created_at < ?", (state, time.time() - 3600))
        if error or not row or not code:
            flash(request, f"Etsy connection was cancelled or expired. {error}".strip(), "error")
            return RedirectResponse("/", status_code=303)
        tokens = await etsy.exchange_code(settings, code, row["verifier"])
        client = etsy.EtsyClient(settings, tokens["access_token"], tokens.get("refresh_token"))
        me = await client.get_me()
        shop_id = me.get("shop_id")
        if not shop_id:
            flash(request, "That Etsy account doesn't have a shop yet.", "error")
            return RedirectResponse("/", status_code=303)
        shop = await client.get_shop(shop_id)
        db.save_shop(shop_id, me["user_id"], shop.get("shop_name", ""), tokens, shop)
        try:
            await services.sync_shop(settings, db, shop_id)
            flash(request, f"Connected {shop.get('shop_name')} and imported its listings.")
        except etsy.EtsyError as e:
            flash(request, f"Connected, but the first sync failed: {e}", "error")
        return RedirectResponse(f"/shops/{shop_id}", status_code=303)

    # Shop ------------------------------------------------------------------
    def get_shop(shop_id: int):
        shop = db.shop(shop_id)
        if not shop:
            raise HTTPException(404, "Shop not connected")
        return shop

    @app.get("/shops/{shop_id}", response_class=HTMLResponse)
    async def shop_page(request: Request, shop_id: int, sort: str = "score"):
        shop = get_shop(shop_id)
        order = "score ASC" if sort == "score" else "last_modified DESC"
        listings = db.q(f"SELECT * FROM listings WHERE shop_id=? ORDER BY {order}", (shop_id,))
        changes = db.q("SELECT * FROM changes WHERE shop_id=? ORDER BY id DESC LIMIT 25", (shop_id,))
        drafts = db.q("SELECT * FROM drafts WHERE shop_id=? ORDER BY id DESC LIMIT 50", (shop_id,))
        return render(request, "shop.html", shop=shop, listings=listings, changes=changes, drafts=drafts, sort=sort)

    @app.post("/shops/{shop_id}/sync")
    async def sync(request: Request, shop_id: int):
        get_shop(shop_id)
        try:
            s = await services.sync_shop(settings, db, shop_id, get_generator())
            msg = f"Synced {s['total']} listings: {s['new']} new, {s['updated']} updated, {s['removed']} removed."
            if s["suggestions"]:
                msg += f" {s['suggestions']} improvement suggestions drafted."
            flash(request, msg)
        except etsy.EtsyError as e:
            flash(request, f"Sync failed: {e}", "error")
        return RedirectResponse(f"/shops/{shop_id}", status_code=303)

    @app.get("/shops/{shop_id}/new", response_class=HTMLResponse)
    async def new_form(request: Request, shop_id: int, template: int | None = None):
        shop = get_shop(shop_id)
        listings = db.q("SELECT listing_id, title FROM listings WHERE shop_id=? AND state='active' ORDER BY title", (shop_id,))
        return render(
            request, "new.html", shop=shop, listings=listings, template_id=template,
            defaults=services.default_fields(db, shop_id),
            shipping=db.meta(shop_id, "shipping_profiles"), returns=db.meta(shop_id, "return_policies"),
            readiness=db.meta(shop_id, "readiness_states"), who_made=etsy.WHO_MADE, when_made=etsy.WHEN_MADE,
        )

    async def _save_uploads(files: list[UploadFile]) -> list[tuple[str, str]]:
        saved = []
        for f in files or []:
            if not f.filename or f.content_type not in ALLOWED_IMAGE_TYPES:
                continue
            path = Path(settings.upload_dir) / f"{uuid.uuid4().hex}{Path(f.filename).suffix.lower()}"
            path.write_bytes(await f.read())
            saved.append((str(path), f.content_type))
        return saved

    @app.post("/shops/{shop_id}/new")
    async def new_submit(
        request: Request,
        shop_id: int,
        notes: str = Form(""),
        price: str = Form(""),
        quantity: str = Form(""),
        who_made: str = Form(""),
        when_made: str = Form(""),
        listing_type: str = Form(""),
        shipping_profile_id: str = Form(""),
        return_policy_id: str = Form(""),
        readiness_state_id: str = Form(""),
        template_id: str = Form(""),
        photos: list[UploadFile] = File(default=[]),
    ):
        get_shop(shop_id)
        template = None
        if template_id:
            row = db.one("SELECT data FROM listings WHERE listing_id=? AND shop_id=?", (int(template_id), shop_id))
            template = json.loads(row["data"]) if row else None
        images = await _save_uploads(photos)
        if not notes.strip() and not images and not template:
            flash(request, "Add a few notes, a photo, or pick a listing to base it on.", "error")
            return RedirectResponse(f"/shops/{shop_id}/new", status_code=303)
        overrides = {
            "price": price, "quantity": _int(quantity), "who_made": who_made, "when_made": when_made,
            "type": listing_type, "shipping_profile_id": _int(shipping_profile_id),
            "return_policy_id": _int(return_policy_id), "readiness_state_id": _int(readiness_state_id),
        }
        try:
            draft_id = await services.create_new_draft(
                db, require_generator(), shop_id, notes, images, overrides, template
            )
        except GenerationError as e:
            flash(request, str(e), "error")
            return RedirectResponse(f"/shops/{shop_id}/new", status_code=303)
        return RedirectResponse(f"/drafts/{draft_id}", status_code=303)

    @app.post("/shops/{shop_id}/bulk")
    async def bulk(request: Request, shop_id: int, csv_file: UploadFile = File(...)):
        """CSV with columns: notes, price, quantity (optional). One draft per row."""
        get_shop(shop_id)
        text = (await csv_file.read()).decode("utf-8-sig")
        rows = [r for r in csv.DictReader(io.StringIO(text)) if (r.get("notes") or "").strip()]
        made, failed = 0, 0
        try:
            gen = require_generator()
        except GenerationError as e:
            flash(request, str(e), "error")
            return RedirectResponse(f"/shops/{shop_id}", status_code=303)
        for r in rows[:50]:
            try:
                await services.create_new_draft(
                    db, gen, shop_id, r["notes"], [],
                    {"price": r.get("price", ""), "quantity": _int(r.get("quantity", ""))},
                )
                made += 1
            except GenerationError:
                failed += 1
        flash(request, f"Drafted {made} listings from the CSV." + (f" {failed} failed." if failed else ""))
        return RedirectResponse(f"/shops/{shop_id}", status_code=303)

    @app.post("/listings/{listing_id}/optimize")
    async def optimize(request: Request, listing_id: int, notes: str = Form("")):
        row = db.one("SELECT * FROM listings WHERE listing_id=?", (listing_id,))
        if not row:
            raise HTTPException(404)
        try:
            draft_id = await services.create_optimize_draft(
                db, require_generator(), row["shop_id"], json.loads(row["data"]), notes
            )
        except GenerationError as e:
            flash(request, str(e), "error")
            return RedirectResponse(f"/shops/{row['shop_id']}", status_code=303)
        return RedirectResponse(f"/drafts/{draft_id}", status_code=303)

    # Drafts ----------------------------------------------------------------
    def get_draft(draft_id: int) -> dict:
        draft = db.draft(draft_id)
        if not draft:
            raise HTTPException(404)
        return draft

    @app.get("/drafts/{draft_id}", response_class=HTMLResponse)
    async def draft_page(request: Request, draft_id: int):
        draft = get_draft(draft_id)
        shop_id = draft["shop_id"]
        tax_id = draft["fields"].get("taxonomy_id")
        tax = db.one("SELECT * FROM taxonomy WHERE id=?", (tax_id,)) if tax_id else None
        listing = None
        if draft["source_listing_id"]:
            listing = db.one("SELECT * FROM listings WHERE listing_id=?", (draft["source_listing_id"],))
        issues = rules.validate(draft["fields"]) if draft["kind"] == "new" else []
        return render(
            request, "draft.html", draft=draft, f=draft["fields"], issues=issues, taxonomy=tax,
            listing=listing, shop=db.shop(shop_id), limits=rules,
            shipping=db.meta(shop_id, "shipping_profiles"), returns=db.meta(shop_id, "return_policies"),
            readiness=db.meta(shop_id, "readiness_states"), who_made=etsy.WHO_MADE, when_made=etsy.WHEN_MADE,
        )

    @app.post("/drafts/{draft_id}")
    async def draft_save(request: Request, draft_id: int):
        draft = get_draft(draft_id)
        form = await request.form()
        f = dict(draft["fields"])
        f["title"] = rules.clean_title(form.get("title", f.get("title", "")))
        f["description"] = (form.get("description") or "").strip()
        f["tags"] = rules.clean_tags(form.get("tags", ""))
        f["materials"] = rules.clean_materials(form.get("materials", ""))
        for key in ("taxonomy_id", "quantity", "shipping_profile_id", "return_policy_id", "readiness_state_id"):
            if key in form:
                f[key] = _int(form.get(key))
        for key in ("price", "who_made", "when_made", "type"):
            if key in form:
                f[key] = form.get(key)
        if draft["kind"] == "new":
            f["is_supply"] = form.get("is_supply") == "on"
        db.update_draft(draft_id, fields=f)
        flash(request, "Saved.")
        return RedirectResponse(f"/drafts/{draft_id}", status_code=303)

    @app.post("/drafts/{draft_id}/regenerate")
    async def regenerate(request: Request, draft_id: int, notes: str = Form("")):
        draft = get_draft(draft_id)
        try:
            gen = require_generator()
            if draft["kind"] == "optimize":
                row = db.one("SELECT data FROM listings WHERE listing_id=?", (draft["source_listing_id"],))
                new_id = await services.create_optimize_draft(db, gen, draft["shop_id"], json.loads(row["data"]), notes)
                db.update_draft(draft_id, status="discarded")
                return RedirectResponse(f"/drafts/{new_id}", status_code=303)
            all_notes = f"{draft['notes']}\n\nChanges requested: {notes}" if notes else draft["notes"]
            ctx = services.build_context(db, draft["shop_id"], all_notes)
            images = [services.ImageInput(Path(i["path"]).read_bytes(), i["media_type"]) for i in draft["images"]]
            copy = await asyncio.to_thread(gen.generate_new, ctx, all_notes, images)
        except GenerationError as e:
            flash(request, str(e), "error")
            return RedirectResponse(f"/drafts/{draft_id}", status_code=303)
        f = dict(draft["fields"])
        f.update({k: v for k, v in copy.items() if v not in (None, "")})
        db.update_draft(draft_id, fields=f, notes=all_notes)
        flash(request, "Rewrote the listing copy.")
        return RedirectResponse(f"/drafts/{draft_id}", status_code=303)

    @app.post("/drafts/{draft_id}/push")
    async def push(request: Request, draft_id: int, action: str = Form("draft")):
        draft = get_draft(draft_id)
        try:
            await services.push_draft(settings, db, draft_id, publish=action == "publish")
        except (ValueError, etsy.EtsyError) as e:
            db.update_draft(draft_id, error=str(e))
            flash(request, f"Etsy didn't accept it: {e}", "error")
            return RedirectResponse(f"/drafts/{draft_id}", status_code=303)
        done = {"publish": "Published on Etsy.", "draft": "Saved to Etsy as a draft."}
        flash(request, "Applied to the Etsy listing." if draft["kind"] == "optimize" else done.get(action, "Done."))
        try:
            await services.sync_shop(settings, db, draft["shop_id"])
        except etsy.EtsyError:
            pass
        return RedirectResponse(f"/drafts/{draft_id}", status_code=303)

    @app.post("/drafts/{draft_id}/discard")
    async def discard(request: Request, draft_id: int):
        draft = get_draft(draft_id)
        db.update_draft(draft_id, status="discarded")
        return RedirectResponse(f"/shops/{draft['shop_id']}", status_code=303)

    @app.get("/api/taxonomy")
    async def taxonomy_search(q: str = ""):
        rows = db.q("SELECT id, path FROM taxonomy WHERE path LIKE ? ORDER BY level DESC LIMIT 20", (f"%{q}%",))
        return [dict(r) for r in rows]

    return app


def _int(value) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _ago(ts) -> str:
    if not ts:
        return "never"
    secs = int(time.time() - float(ts))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= size:
            return f"{secs // size}{unit} ago"
    return "just now"


def get_app() -> FastAPI:
    """Entry point for `uvicorn app.main:get_app --factory`."""
    return create_app()
