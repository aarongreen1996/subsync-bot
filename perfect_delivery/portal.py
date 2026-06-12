# portal.py — Perfect Delivery portal: login, dashboard, API routes
import os
import json
import uuid
import hashlib
import secrets
from datetime import datetime, timezone, timedelta
from functools import wraps

from flask import (
    Blueprint, request, render_template, jsonify,
    redirect, url_for, make_response, abort
)
from supabase import create_client, Client
try:
    from .email_utils import send_welcome_email, send_site_manager_assignment_email
except Exception:
    def send_welcome_email(*a, **k): pass
    def send_site_manager_assignment_email(*a, **k): pass

SUPABASE_URL  = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY  = os.environ.get("SUPABASE_KEY", "")
ADMIN_KEY     = os.environ.get("PD_ADMIN_KEY", "change-me")
SESSION_DAYS  = 30

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

portal_bp = Blueprint("portal", __name__, template_folder="templates", url_prefix="/pd/portal")


def _hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


def _get_tenant() -> dict:
    try:
        res = supabase.table("pd_tenants").select("*").limit(1).execute()
        return res.data[0] if res.data else {}
    except Exception:
        return {}


def get_effective_checklist(tenant_id: str, dwelling_category: str = "house") -> dict:
    """Return STAGES dict filtered/customised for this tenant + dwelling type.
    Applies pd_stage_visibility (hidden_globally, hidden_for, name_override,
    applies_to_override) and pd_checklist_overrides (text, enabled, photo_required,
    hidden_for, example_photo_url/guidance, custom items) for the given dwelling
    category. Stages or items hidden for this dwelling type are removed entirely.
    Use this everywhere a stage checklist is rendered or validated for a plot."""
    from .checklist_data import STAGES as _DEFAULT_STAGES
    import copy as _copy

    result = _copy.deepcopy(_DEFAULT_STAGES)
    if not tenant_id:
        return result

    try:
        vis_res = supabase.table("pd_stage_visibility").select("*").eq("tenant_id", tenant_id).execute()
        vis_map = {v["stage_number"]: v for v in (vis_res.data or [])}
    except Exception:
        vis_map = {}

    try:
        ov_res = supabase.table("pd_checklist_overrides").select("*").eq("tenant_id", tenant_id).execute()
        overrides = ov_res.data or []
    except Exception:
        overrides = []

    ov_by_stage = {}
    for o in overrides:
        ov_by_stage.setdefault(o["stage_number"], {})[o["item_id"]] = o

    final = {}
    for n, stage in result.items():
        v = vis_map.get(n, {})
        if v.get("hidden_globally"):
            continue
        hidden_for = v.get("hidden_for") or []
        if dwelling_category in hidden_for:
            continue

        stage = _copy.deepcopy(stage)
        if v.get("name_override"):
            stage["name"] = v["name_override"]
        if v.get("applies_to_override"):
            stage["applies_to"] = v["applies_to_override"]

        item_overrides = ov_by_stage.get(n, {})
        default_ids = {i["id"] for i in stage.get("items", [])}

        new_items = []
        for item in stage.get("items", []):
            ov = item_overrides.get(item["id"], {})
            if ov.get("enabled") is False:
                continue
            ih = ov.get("hidden_for") or []
            if dwelling_category in ih:
                continue
            item = _copy.deepcopy(item)
            if "text" in ov: item["text"] = ov["text"]
            if "photo_required" in ov: item["photo"] = ov["photo_required"]
            if ov.get("example_photo_url"): item["example_photo_url"] = ov["example_photo_url"]
            if ov.get("example_guidance"):  item["example_guidance"]  = ov["example_guidance"]
            new_items.append(item)

        # Custom items not in defaults
        for item_id, ov in item_overrides.items():
            if item_id in default_ids:
                continue
            if not ov.get("is_custom"):
                continue
            if ov.get("enabled") is False:
                continue
            ih = ov.get("hidden_for") or []
            if dwelling_category in ih:
                continue
            new_items.append({
                "id": item_id,
                "text": ov.get("text",""),
                "photo": ov.get("photo_required", True),
                "example_photo_url": ov.get("example_photo_url",""),
                "example_guidance": ov.get("example_guidance",""),
            })

        stage["items"] = new_items
        final[n] = stage

    # Custom stages (stage_number >= 1000) stored only via visibility rows with name_override
    for n, v in vis_map.items():
        if n < 1000 or v.get("hidden_globally"):
            continue
        hidden_for = v.get("hidden_for") or []
        if dwelling_category in hidden_for:
            continue
        item_overrides = ov_by_stage.get(n, {})
        new_items = []
        for item_id, ov in item_overrides.items():
            if ov.get("enabled") is False:
                continue
            ih = ov.get("hidden_for") or []
            if dwelling_category in ih:
                continue
            new_items.append({
                "id": item_id,
                "text": ov.get("text",""),
                "photo": ov.get("photo_required", True),
                "example_photo_url": ov.get("example_photo_url",""),
                "example_guidance": ov.get("example_guidance",""),
            })
        final[n] = {
            "name": v.get("name_override", f"Custom Stage {n}"),
            "applies_to": v.get("applies_to_override",""),
            "items": new_items,
        }

    return final


def _get_current_user():
    token = request.cookies.get("pd_session")
    if not token:
        return None
    try:
        now = datetime.now(timezone.utc).isoformat()
        res = supabase.table("pd_sessions").select(
            "*, pd_users(*)"
        ).eq("token", token).gt("expires_at", now).single().execute()
        if not res.data:
            return None
        user = res.data["pd_users"]
        user["session_token"] = token
        return user
    except Exception:
        return None


def _require_login(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        user = _get_current_user()
        if not user:
            return redirect("/pd/portal")
        return f(*args, user=user, **kwargs)
    return decorated


def _require_admin_role(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        user = _get_current_user()
        if not user:
            return redirect("/pd/portal")
        if user.get("role") != "tenant_admin":
            return jsonify({"ok": False, "error": "Admin access required"}), 403
        return f(*args, user=user, **kwargs)
    return decorated


def _user_site_filter(user: dict, query):
    if user.get("role") == "tenant_admin":
        return query
    site_ids = user.get("site_ids") or []
    if isinstance(site_ids, str):
        try:
            site_ids = json.loads(site_ids)
        except Exception:
            site_ids = []
    if site_ids:
        query = query.in_("site_id", site_ids)
    else:
        query = query.eq("id", "00000000-0000-0000-0000-000000000000")
    return query


# ── Auth routes ───────────────────────────────────────────────────────────────

@portal_bp.route("", methods=["GET"])
@portal_bp.route("/", methods=["GET"])
def login_page():
    user = _get_current_user()
    if user:
        return redirect("/pd/portal/dashboard")
    tenant = _get_tenant()
    return render_template("pd/portal_login.html", tenant=tenant)


@portal_bp.route("/auth", methods=["POST"])
def auth():
    data     = request.get_json() or {}
    email    = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    if not email or not password:
        return jsonify({"ok": False, "error": "Email and password are required"}), 400

    pw_hash = _hash_password(password)
    try:
        res = supabase.table("pd_users").select("*").eq("email", email).eq("password_hash", pw_hash).single().execute()
    except Exception:
        return jsonify({"ok": False, "error": "Invalid email or password"}), 401

    if not res.data:
        return jsonify({"ok": False, "error": "Invalid email or password"}), 401

    user = res.data

    token      = secrets.token_hex(32)
    expires_at = (datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)).isoformat()
    supabase.table("pd_sessions").insert({
        "user_id":    user["id"],
        "token":      token,
        "expires_at": expires_at,
    }).execute()

    supabase.table("pd_users").update({
        "last_login": datetime.now(timezone.utc).isoformat()
    }).eq("id", user["id"]).execute()

    resp = make_response(jsonify({"ok": True}))
    resp.set_cookie(
        "pd_session", token,
        httponly=True, samesite="Lax",
        max_age=SESSION_DAYS * 86400,
        secure=os.environ.get("FLASK_ENV") != "development",
    )
    return resp


@portal_bp.route("/signout", methods=["POST"])
def signout():
    token = request.cookies.get("pd_session")
    if token:
        try:
            supabase.table("pd_sessions").delete().eq("token", token).execute()
        except Exception:
            pass
    resp = make_response(jsonify({"ok": True}))
    resp.delete_cookie("pd_session")
    return resp


# ── Dashboard ─────────────────────────────────────────────────────────────────

@portal_bp.route("/dashboard")
@_require_login
def dashboard(user):
    tenant = _get_tenant()
    if user.get("role") == "tenant_admin":
        sites_res = supabase.table("pd_sites").select("id, name").order("name").execute()
    else:
        site_ids = user.get("site_ids") or []
        if isinstance(site_ids, str):
            try: site_ids = json.loads(site_ids)
            except: site_ids = []
        sites_res = supabase.table("pd_sites").select("id, name").in_("id", site_ids).execute() if site_ids else type('obj', (object,), {'data': []})()

    sites = sites_res.data or []
    sites_json = json.dumps(sites)

    from .checklist_data import STAGES
    stages_json = json.dumps({
        str(k): {"name": v["name"]}
        for k, v in STAGES.items()
    })
    stages_full_json = json.dumps({
        str(k): {
            "name": v["name"],
            "applies_to": v.get("applies_to", ""),
            "items": [{"id": i["id"], "text": i["text"], "photo": i.get("photo", True)} for i in v.get("items", [])]
        }
        for k, v in STAGES.items()
    })

    return render_template(
        "pd/portal_dashboard.html",
        user=user,
        tenant=tenant,
        sites=sites,
        sites_json=sites_json,
        stages_json=stages_json,
        stages_full_json=stages_full_json,
        admin_key=ADMIN_KEY,
    )


# ── Plot progress (portal version) ────────────────────────────────────────────

@portal_bp.route("/plot/<plot_id>/progress")
@_require_login
def plot_progress(user, plot_id):
    res = supabase.table("pd_plots").select("*, pd_sites(*)").eq("id", plot_id).single().execute()
    if not res.data:
        abort(404)
    plot = res.data
    site = plot["pd_sites"]

    if user.get("role") != "tenant_admin":
        site_ids = user.get("site_ids") or []
        if isinstance(site_ids, str):
            try: site_ids = json.loads(site_ids)
            except: site_ids = []
        if plot["site_id"] not in site_ids:
            abort(403)

    from .checklist_data import STAGES, STAGE_GROUPS
    stages_json = json.dumps({
        str(k): {"name": v["name"], "applies_to": v["applies_to"]}
        for k, v in STAGES.items()
    })
    groups_json = json.dumps([
        {"name": group_name, "stages": stage_nums}
        for group_name, stage_nums in STAGE_GROUPS.items()
    ])

    return render_template(
        "pd/plot_progress.html",
        plot=plot,
        site=site,
        stages_json=stages_json,
        groups_json=groups_json,
        admin_key=ADMIN_KEY,
        back_url="/pd/portal/dashboard",
    )


@portal_bp.route("/qr/<plot_id>")
@_require_login
def portal_qr(user, plot_id):
    res = supabase.table("pd_plots").select("*, pd_sites(name)").eq("id", plot_id).single().execute()
    if not res.data:
        abort(404)
    plot = res.data

    if user.get("role") != "tenant_admin":
        site_ids = user.get("site_ids") or []
        if isinstance(site_ids, str):
            try: site_ids = json.loads(site_ids)
            except: site_ids = []
        if plot.get("site_id") not in site_ids:
            abort(403)

    from .qr_utils import generate_qr_png
    from flask import send_file as _sf
    import io as _io
    inline = request.args.get("inline") == "1"
    png_bytes = generate_qr_png(plot["access_token"], plot["plot_number"], plot["pd_sites"]["name"])
    return _sf(
        _io.BytesIO(png_bytes), mimetype="image/png",
        as_attachment=not inline,
        download_name=f"QR_Plot_{plot['plot_number']}.png",
    )


# ── Site & Plot management (tenant admin) ────────────────────────────────────

@portal_bp.route("/api/sites", methods=["POST"])
@_require_admin_role
def api_sites_create(user):
    data = request.get_json() or {}
    tenant = _get_tenant()
    tenant_id = tenant.get("id")
    record = {
        "name":          (data.get("name") or "").strip(),
        "address":       (data.get("address") or "").strip(),
        "manager_name":  (data.get("manager_name") or "").strip(),
        "manager_email": (data.get("manager_email") or "").strip(),
        "manager_phone": (data.get("manager_phone") or "").strip(),
        "qs_name":       (data.get("qs_name") or "").strip(),
        "qs_email":      (data.get("qs_email") or "").strip(),
        "tenant_id":     tenant_id,
    }
    if not record["name"] or not record["manager_email"]:
        return jsonify({"ok": False, "error": "Site name and manager email are required"}), 400
    res = supabase.table("pd_sites").insert(record).execute()
    return jsonify({"ok": True, "site": res.data[0]})


@portal_bp.route("/api/sites/<site_id>", methods=["PUT"])
@_require_admin_role
def api_sites_update(user, site_id):
    data = request.get_json() or {}
    updates = {}
    for f in ["name","address","manager_name","manager_email","manager_phone","qs_name","qs_email"]:
        if data.get(f) is not None:
            updates[f] = data[f].strip()
    if not updates:
        return jsonify({"ok": False, "error": "Nothing to update"}), 400
    supabase.table("pd_sites").update(updates).eq("id", site_id).execute()
    return jsonify({"ok": True})


@portal_bp.route("/api/sites/<site_id>", methods=["DELETE"])
@_require_admin_role
def api_sites_delete(user, site_id):
    supabase.table("pd_sites").delete().eq("id", site_id).execute()
    return jsonify({"ok": True})


@portal_bp.route("/api/plots", methods=["POST"])
@_require_admin_role
def api_plots_create(user):
    data = request.get_json() or {}
    record = {
        "site_id":      (data.get("site_id") or "").strip(),
        "plot_number":  (data.get("plot_number") or "").strip(),
    }
    if not record["site_id"] or not record["plot_number"]:
        return jsonify({"ok": False, "error": "Site and plot number are required"}), 400
    res = supabase.table("pd_plots").insert(record).execute()
    return jsonify({"ok": True, "plot": res.data[0]})


@portal_bp.route("/api/plots/<plot_id>", methods=["DELETE"])
@_require_admin_role
def api_plots_delete(user, plot_id):
    try:
        subs = supabase.table("pd_submissions").select("id").eq("plot_id", plot_id).execute()
        for sub in (subs.data or []):
            supabase.table("pd_photos").delete().eq("submission_id", sub["id"]).execute()
        supabase.table("pd_submissions").delete().eq("plot_id", plot_id).execute()
    except Exception as e:
        print(f"Cascade delete error: {e}")
    supabase.table("pd_plots").delete().eq("id", plot_id).execute()
    return jsonify({"ok": True})


@portal_bp.route("/api/plots/<plot_id>/clear", methods=["DELETE"])
@_require_admin_role
def api_plots_clear(user, plot_id):
    try:
        subs = supabase.table("pd_submissions").select("id").eq("plot_id", plot_id).execute()
        for sub in (subs.data or []):
            supabase.table("pd_photos").delete().eq("submission_id", sub["id"]).execute()
        supabase.table("pd_submissions").delete().eq("plot_id", plot_id).execute()
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True})


# ── Plot import (AI-powered + manual) ────────────────────────────────────────

@portal_bp.route("/api/plots/import-preview", methods=["POST"])
@_require_admin_role
def api_plots_import_preview(user):
    import os, base64

    if "file" not in request.files:
        return jsonify({"ok": False, "error": "No file uploaded"}), 400

    file_obj = request.files["file"]
    site_id  = request.form.get("site_id", "")
    if not site_id:
        return jsonify({"ok": False, "error": "Site ID required"}), 400

    filename = (file_obj.filename or "").lower()
    file_bytes = file_obj.read()

    system_prompt = """You are a data extraction assistant for a UK house builder.
You will receive an accommodation schedule document. Extract every RESIDENTIAL PLOT.

OUTPUT FORMAT — return ONLY a valid JSON array, nothing else, no markdown, no explanation:
[{"plot_number":"1","house_type":"Rushbury NFR3","dwelling_type":"Detached"},...]

RULES:
- plot_number = the integer at the start of the row (column 1). Must be a number 1-999.
- house_type = the PF house type/name (e.g. "Rushbury NFR3", "Anderbury NFR1", "Flats Block A Special Flat")
- dwelling_type = the dwelling type column (Det, Semi, Detached, Semi-Detached, Terrace, End-Terrace, Flat, Bungalow, Linked-Semi-Detached etc)
- SKIP rows where plot_number is blank or not a number (communal areas, cycle stores, bin stores etc)
- INCLUDE all numbered plots including flats
- Do not include any text before or after the JSON array"""

    try:
        import anthropic as _anthropic
        client = _anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY",""))

        text_content = ""
        msg = None

        if filename.endswith(".pdf"):
            import re as _re2, json as _json2, io as _io2

            raw_text = ""
            try:
                import pypdf as _pypdf
                reader = _pypdf.PdfReader(_io2.BytesIO(file_bytes))
                raw_text = "\n".join(p.extract_text() or "" for p in reader.pages)
                print(f"pypdf extracted {len(raw_text)} chars from {len(reader.pages)} pages")
            except Exception as e:
                print(f"pypdf failed: {e}")

            DWELLING_TYPES = [
                "Linked-End-Terrace","Linked-Semi-Detached","Semi-Detached",
                "End-Terrace","Detached","Terrace","Flat","Bungalow","Det","Semi","Bung"
            ]
            STRIP_WORDS = [
                "Gold Private","Silver Private","Affordable","Aff Shared","Aff Rent",
                "Social Rent","1st Homes","Private","OPP","AS","SITE SPECIFIC",
                "NFR1","NFR2","NFR3","NFR4","Special","SPECIAL"
            ]

            def parse_schedule(text):
                plots = []
                seen = set()
                for line in text.split("\n"):
                    line = line.strip()
                    if not line:
                        continue
                    m = _re2.match(r"^(\d+)\s+(.+)", line)
                    if not m:
                        continue
                    pn   = m.group(1)
                    rest = m.group(2).strip()
                    if any(w in rest.lower() for w in ["communal","cycle","bin store","car park","substation","store -"]):
                        continue
                    if pn in seen:
                        continue
                    dwelling = ""
                    for dt in DWELLING_TYPES:
                        if _re2.search(r"\b" + _re2.escape(dt) + r"\b", rest, _re2.IGNORECASE):
                            dwelling = dt
                            break
                    ht = _re2.split(r"\s+\d{2,3}\.\d", rest)[0].strip()
                    for kw in STRIP_WORDS:
                        ht = ht.replace(kw, " ")
                    for dt in DWELLING_TYPES:
                        ht = _re2.sub(r"\b" + _re2.escape(dt) + r"\b", "", ht, flags=_re2.IGNORECASE)
                    ht = _re2.sub(r"\s+", " ", ht).strip().strip("-").strip()
                    seen.add(pn)
                    plots.append({"plot_number": pn, "house_type": ht, "dwelling_type": dwelling, "site_id": site_id})
                return plots

            all_plots = []
            if raw_text:
                all_plots = parse_schedule(raw_text)
                print(f"Regex parser found {len(all_plots)} plots")

            if len(all_plots) < 10:
                print("Regex found few plots — trying AI fallback")
                try:
                    fallback_text = raw_text[:20000] if raw_text else file_bytes.decode("utf-8", errors="ignore")[:20000]
                    fb_msg = client.messages.create(
                        model="claude-haiku-4-5", max_tokens=8096,
                        system=system_prompt,
                        messages=[{"role":"user","content":
                            f"Extract all residential plots:\n\n{fallback_text}\n\nReturn ONLY JSON array."
                        }]
                    )
                    resp = fb_msg.content[0].text.strip()
                    cleaned_r = _re2.sub(r"```[a-zA-Z]*","",resp).replace("```","").strip()
                    ai_plots = _json2.loads(cleaned_r[cleaned_r.index("["):cleaned_r.rindex("]")+1])
                    for p in ai_plots:
                        p["site_id"] = site_id
                    all_plots = ai_plots
                    print(f"AI fallback found {len(all_plots)} plots")
                except Exception as e:
                    print(f"AI fallback error: {e}")

            cleaned_plots = []
            seen2 = set()
            for p in all_plots:
                pn = str(p.get("plot_number","")).strip()
                if pn and pn not in seen2 and pn.isdigit():
                    seen2.add(pn)
                    cleaned_plots.append({
                        "plot_number":   pn,
                        "house_type":    str(p.get("house_type","")).strip(),
                        "dwelling_type": str(p.get("dwelling_type","")).strip(),
                        "site_id":       site_id,
                    })

            cleaned_plots.sort(key=lambda x: int(x["plot_number"]))
            print(f"Returning {len(cleaned_plots)} validated plots")
            return jsonify({"ok": True, "plots": cleaned_plots, "count": len(cleaned_plots)})

        elif filename.endswith((".xlsx",".xls")):
            try:
                import openpyxl as _openpyxl, io as _io
                wb = _openpyxl.load_workbook(_io.BytesIO(file_bytes), data_only=True)
                ws = wb.active
                rows = []
                for row in ws.iter_rows(values_only=True):
                    if any(v is not None for v in row):
                        rows.append("\t".join(str(v or "") for v in row))
                text_content = "\n".join(rows[:300])
            except Exception as e:
                text_content = file_bytes.decode("utf-8", errors="ignore")[:12000]
        else:
            text_content = file_bytes.decode("utf-8", errors="ignore")[:12000]

        if msg is None:
            msg = client.messages.create(
                model="claude-haiku-4-5", max_tokens=4096,
                system=system_prompt,
                messages=[{"role":"user","content":
                    f"Extract all residential plots from this accommodation schedule:\n\n{text_content}\n\nReturn ONLY the JSON array."
                }]
            )

        response_text = msg.content[0].text.strip()
        print(f"AI response preview: {response_text[:300]}")

        import re as _re, json as _json
        plots = None

        try:
            plots = _json.loads(response_text)
        except Exception:
            pass

        if plots is None:
            try:
                start = response_text.index("[")
                end   = response_text.rindex("]") + 1
                plots = _json.loads(response_text[start:end])
            except Exception:
                pass

        if plots is None:
            try:
                cleaned_r = _re.sub(r"```[a-z]*", "", response_text).replace("```", "").strip()
                start = cleaned_r.index("[")
                end   = cleaned_r.rindex("]") + 1
                plots = _json.loads(cleaned_r[start:end])
            except Exception:
                pass

        if plots is None:
            try:
                start = response_text.index("[")
                end   = response_text.rindex("]") + 1
                chunk = response_text[start:end]
                chunk = _re.sub(r',\s*\]', ']', chunk)
                plots = _json.loads(chunk)
            except Exception:
                pass

        if plots is None:
            return jsonify({
                "ok": False,
                "error": "Could not parse AI response",
                "raw": response_text[:800]
            }), 400

        cleaned = []
        for p in plots:
            pn = str(p.get("plot_number","")).strip()
            ht = str(p.get("house_type","")).strip()
            dt = str(p.get("dwelling_type","")).strip()
            if pn and pn.replace("-","").replace("_","").replace(" ","").isalnum():
                cleaned.append({
                    "plot_number":  pn,
                    "house_type":   ht,
                    "dwelling_type": dt,
                    "site_id":      site_id,
                })

        return jsonify({"ok": True, "plots": cleaned, "count": len(cleaned)})

    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@portal_bp.route("/api/plots/import-confirm", methods=["POST"])
@_require_admin_role
def api_plots_import_confirm(user):
    data    = request.get_json() or {}
    plots   = data.get("plots", [])
    site_id = data.get("site_id", "")

    if not plots or not site_id:
        return jsonify({"ok": False, "error": "No plots or site provided"}), 400

    created = 0
    skipped = 0
    errors  = []

    for p in plots:
        if not p.get("plot_number"):
            continue
        ht = p.get("house_type","").strip()
        dt = p.get("dwelling_type","").strip()

        try:
            supabase.table("pd_plots").insert({
                "site_id":          site_id,
                "plot_number":      str(p["plot_number"]).strip(),
                "house_type":       ht,
                "house_type_detail": dt,
            }).execute()
            created += 1
        except Exception as e:
            err = str(e)
            if "unique" in err.lower() or "duplicate" in err.lower():
                skipped += 1
            else:
                errors.append(f"Plot {p['plot_number']}: {err}")

    return jsonify({"ok": True, "created": created, "skipped": skipped, "errors": errors})


@portal_bp.route("/api/plots/import-range", methods=["POST"])
@_require_admin_role
def api_plots_import_range(user):
    data       = request.get_json() or {}
    site_id    = data.get("site_id","")
    range_str  = data.get("range","").strip()
    house_type = data.get("house_type","").strip()

    if not site_id or not range_str:
        return jsonify({"ok": False, "error": "Site and range required"}), 400

    plot_numbers = []
    for part in range_str.split(","):
        part = part.strip()
        if "-" in part:
            try:
                start, end = part.split("-", 1)
                prefix = "".join(c for c in start if not c.isdigit())
                s = int("".join(c for c in start if c.isdigit()))
                e = int("".join(c for c in end if c.isdigit()))
                for n in range(s, e + 1):
                    plot_numbers.append(f"{prefix}{n}")
            except Exception:
                return jsonify({"ok": False, "error": f"Invalid range: {part}"}), 400
        else:
            plot_numbers.append(part)

    created = skipped = 0
    for pn in plot_numbers:
        try:
            supabase.table("pd_plots").insert({
                "site_id":    site_id,
                "plot_number": pn,
                "house_type":  house_type,
            }).execute()
            created += 1
        except Exception as e:
            if "unique" in str(e).lower() or "duplicate" in str(e).lower():
                skipped += 1

    return jsonify({"ok": True, "created": created, "skipped": skipped})


@portal_bp.route("/api/plots/<plot_id>", methods=["PUT"])
@_require_admin_role
def api_plots_update(user, plot_id):
    data = request.get_json() or {}
    updates = {}
    if "plot_number"      in data: updates["plot_number"]      = str(data["plot_number"]).strip()
    if "house_type"       in data: updates["house_type"]       = data["house_type"].strip()
    if "house_type_detail"in data: updates["house_type_detail"]= data["house_type_detail"].strip()
    if "dwelling_category"in data: updates["dwelling_category"]= data["dwelling_category"].strip()
    if not updates:
        return jsonify({"ok": False, "error": "Nothing to update"}), 400
    supabase.table("pd_plots").update(updates).eq("id", plot_id).execute()
    return jsonify({"ok": True})


# ── Plot report (portal) ─────────────────────────────────────────────────────

@portal_bp.route("/plot/<plot_id>/report")
@_require_login
def plot_report(user, plot_id):
    res = supabase.table("pd_plots").select("*, pd_sites(*)").eq("id", plot_id).single().execute()
    if not res.data:
        abort(404)
    plot = res.data
    site = plot["pd_sites"]

    if user.get("role") != "tenant_admin":
        site_ids = user.get("site_ids") or []
        if isinstance(site_ids, str):
            try: site_ids = json.loads(site_ids)
            except: site_ids = []
        if plot["site_id"] not in site_ids:
            abort(403)

    from .checklist_data import STAGES, STAGE_GROUPS
    from datetime import date as _date

    subs_res = supabase.table("pd_submissions").select("*").eq("plot_id", plot_id).order("submitted_at", desc=True).execute()
    subs = subs_res.data or []
    stage_map = {}
    for s in reversed(subs):
        stage_map[s["stage_number"]] = s

    stage_to_group = {}
    for group_name, stage_nums in STAGE_GROUPS.items():
        for n in stage_nums:
            stage_to_group[n] = group_name

    approved_count = pending_count = rejected_count = 0
    stage_rows = []
    for n in range(1, 38):
        stage  = STAGES.get(n, {})
        sub    = stage_map.get(n)
        status = sub["status"] if sub else "not_started"
        if status == "approved":   approved_count += 1
        elif status == "pending":  pending_count  += 1
        elif status == "rejected": rejected_count += 1
        stage_rows.append({
            "stage_number": n,
            "stage_name":   stage.get("name", f"Stage {n}"),
            "applies_to":   stage.get("applies_to", ""),
            "group":        stage_to_group.get(n, ""),
            "status":       status,
            "submitted_by": sub.get("submitted_by_name", "") if sub else "",
            "company":      sub.get("submitted_by_company", "") if sub else "",
            "date":         (sub.get("submitted_at") or "")[:10] if sub else "",
            "reviewed_by":  sub.get("reviewed_by", "") if sub else "",
            "signature_url":sub.get("signature_url", "") if sub else "",
            "review_token": sub.get("review_token", "") if sub else "",
        })

    not_started = 37 - approved_count - pending_count - rejected_count
    pct = round((approved_count / 37) * 100)

    part_l_rows = []
    for sub in stage_map.values():
        if sub.get("status") != "approved":
            continue
        stage   = STAGES.get(sub["stage_number"], {})
        answers = sub.get("answers") or {}
        photos_res = supabase.table("pd_photos").select("*").eq("submission_id", sub["id"]).execute()
        photos_map = {p["item_id"]: p["public_url"] for p in (photos_res.data or [])}
        for item in stage.get("items", []):
            if "PART L" not in item.get("text", ""):
                continue
            a = answers.get(item["id"]) or {}
            part_l_rows.append({
                "stage_name": stage.get("name", ""),
                "item_text":  item["text"][:80],
                "answer":     a.get("value", "") if isinstance(a, dict) else "",
                "photo_url":  photos_map.get(item["id"], ""),
                "date":       (sub.get("submitted_at") or "")[:10],
            })

    return render_template(
        "pd/plot_report.html",
        plot=plot, site=site,
        stage_rows=stage_rows, part_l_rows=part_l_rows,
        approved_count=approved_count, pending_count=pending_count,
        rejected_count=rejected_count, not_started=not_started,
        pct=pct, admin_key=ADMIN_KEY,
        now=_date.today().strftime("%d %b %Y"),
        back_url="/pd/portal/dashboard",
        pdf_url=f"/pd/portal/plot/{plot_id}/report/pdf",
    )


@portal_bp.route("/site/<site_id>/qr-sheet")
@_require_login
def qr_sheet(user, site_id):
    site_res = supabase.table("pd_sites").select("*").eq("id", site_id).single().execute()
    if not site_res.data:
        abort(404)
    site = site_res.data

    if user.get("role") != "tenant_admin":
        site_ids = user.get("site_ids") or []
        if isinstance(site_ids, str):
            try: site_ids = json.loads(site_ids)
            except: site_ids = []
        if site_id not in site_ids:
            abort(403)

    from flask import send_file as _send_file
    import io as _io
    from .qr_utils import generate_qr_png
    from .checklist_data import STAGES
    from .blueprint import _natural_sort_plots

    plots_res = supabase.table("pd_plots").select("*").eq("site_id", site_id).execute()
    plots = _natural_sort_plots(plots_res.data or [])

    from datetime import date as _date
    from flask import render_template as _rt
    from os import environ as _env
    return _rt(
        "pd/qr_sheet.html",
        site=site, plots=plots,
        admin_key=_env.get("PD_ADMIN_KEY", ""),
        base_url=_env.get("PD_BASE_URL",""),
        now=_date.today().strftime("%d %b %Y"),
    )


@portal_bp.route("/plot/<plot_id>/report/pdf")
@_require_login
def plot_report_pdf(user, plot_id):
    res = supabase.table("pd_plots").select("*, pd_sites(*)").eq("id", plot_id).single().execute()
    if not res.data:
        abort(404)
    plot = res.data
    site = plot["pd_sites"]

    if user.get("role") != "tenant_admin":
        site_ids = user.get("site_ids") or []
        if isinstance(site_ids, str):
            try: site_ids = json.loads(site_ids)
            except: site_ids = []
        if plot["site_id"] not in site_ids:
            abort(403)

    from .checklist_data import STAGES, STAGE_GROUPS
    from .pdf_generator import generate_plot_report_pdf
    from flask import send_file as _sf
    import io as _io

    subs_res = supabase.table("pd_submissions").select("*").eq("plot_id", plot_id).order("submitted_at", desc=True).execute()
    subs = subs_res.data or []
    stage_map = {}
    for s in reversed(subs):
        stage_map[s["stage_number"]] = s

    stage_to_group = {n: gn for gn, nums in STAGE_GROUPS.items() for n in nums}
    tenant = _get_tenant()

    pdf_bytes = generate_plot_report_pdf(plot, site, stage_map, STAGES, stage_to_group, tenant, supabase)
    filename = f"PD_Report_{(site.get('name') or '').replace(' ','_')}_Plot{plot.get('plot_number','')}.pdf"
    return _sf(_io.BytesIO(pdf_bytes), mimetype="application/pdf", as_attachment=True, download_name=filename)


# ── API routes ────────────────────────────────────────────────────────────────

@portal_bp.route("/api/sites")
@_require_login
def api_sites(user):
    try:
        if user.get("role") == "tenant_admin":
            res = supabase.table("pd_sites").select("*").order("name").execute()
        else:
            site_ids = user.get("site_ids") or []
            if isinstance(site_ids, str):
                try: site_ids = json.loads(site_ids)
                except: site_ids = []
            if not site_ids:
                return jsonify([])
            res = supabase.table("pd_sites").select("*").in_("id", site_ids).execute()
        sites = res.data or []

        # FIX: Batch fetch ALL plot counts in a single query (was N+1)
        try:
            all_site_ids = [s["id"] for s in sites]
            if all_site_ids:
                plots_res = supabase.table("pd_plots").select("id, site_id").in_("site_id", all_site_ids).execute()
                counts = {}
                for p in (plots_res.data or []):
                    counts[p["site_id"]] = counts.get(p["site_id"], 0) + 1
                for site in sites:
                    site["plot_count"] = counts.get(site["id"], 0)
            else:
                for site in sites:
                    site["plot_count"] = 0
        except Exception:
            for site in sites:
                site["plot_count"] = 0

        return jsonify(sites)
    except Exception as e:
        print(f"api_sites error: {e}")
        return jsonify([])


@portal_bp.route("/api/plots")
@_require_login
def api_plots(user):
    try:
        site_id = request.args.get("site_id")
        query = supabase.table("pd_plots").select("id, plot_number, site_id, status, access_token, house_type, house_type_detail, dwelling_category")
        if site_id:
            query = query.eq("site_id", site_id)
        res = query.execute()
        plots = res.data or []

        def plot_sort_key(p):
            try: return (0, int(p.get("plot_number","0")))
            except: return (1, str(p.get("plot_number","")))
        plots.sort(key=plot_sort_key)

        # FIX: Batch fetch approved counts in one query (was N+1)
        try:
            plot_ids = [p["id"] for p in plots]
            if plot_ids:
                subs_res = supabase.table("pd_submissions").select("plot_id, stage_number").in_("plot_id", plot_ids).eq("status", "approved").execute()
                approved_map = {}
                for s in (subs_res.data or []):
                    pid = s["plot_id"]
                    if pid not in approved_map:
                        approved_map[pid] = set()
                    approved_map[pid].add(s["stage_number"])
                for plot in plots:
                    plot["approved_count"] = len(approved_map.get(plot["id"], set()))
            else:
                for plot in plots:
                    plot["approved_count"] = 0
        except Exception:
            for plot in plots:
                plot["approved_count"] = 0

        return jsonify(plots)
    except Exception as e:
        print(f"api_plots error: {e}")
        return jsonify([])


@portal_bp.route("/api/submissions")
@_require_login
def api_submissions(user):
    status = request.args.get("status")
    limit  = int(request.args.get("limit", 50))

    query = supabase.table("pd_submissions").select(
        "*, pd_plots(plot_number, site_id, pd_sites(name))"
    ).order("submitted_at", desc=True).limit(limit)

    if status:
        query = query.eq("status", status)

    res = query.execute()
    subs = res.data or []

    if user.get("role") != "tenant_admin":
        site_ids = user.get("site_ids") or []
        if isinstance(site_ids, str):
            try: site_ids = json.loads(site_ids)
            except: site_ids = []
        subs = [s for s in subs if s.get("pd_plots", {}).get("site_id") in site_ids]

    return jsonify(subs)


@portal_bp.route("/api/photos")
@_require_login
def api_photos(user):
    site_id      = request.args.get("site_id")
    stage_number = request.args.get("stage_number")
    limit        = int(request.args.get("limit", 200))

    subs_query = supabase.table("pd_submissions").select(
        "id, stage_number, stage_name, submitted_at, pd_plots(plot_number, site_id, pd_sites(name))"
    ).order("stage_number")

    if stage_number:
        subs_query = subs_query.eq("stage_number", int(stage_number))

    subs_res = subs_query.execute()
    subs = subs_res.data or []

    if user.get("role") != "tenant_admin":
        site_ids = user.get("site_ids") or []
        if isinstance(site_ids, str):
            try: site_ids = json.loads(site_ids)
            except: site_ids = []
        subs = [s for s in subs if s.get("pd_plots", {}).get("site_id") in site_ids]

    if site_id:
        subs = [s for s in subs if s.get("pd_plots", {}).get("site_id") == site_id]

    if not subs:
        return jsonify([])

    sub_ids = [s["id"] for s in subs]
    photos_res = supabase.table("pd_photos").select("*").in_("submission_id", sub_ids).limit(limit).execute()
    photos = photos_res.data or []

    sub_map = {s["id"]: s for s in subs}
    for p in photos:
        sub = sub_map.get(p["submission_id"], {})
        p["stage_name"]   = sub.get("stage_name", "")
        p["stage_number"] = sub.get("stage_number", 0)
        p["submitted_at"] = sub.get("submitted_at", "")
        p["plot_number"]  = sub.get("pd_plots", {}).get("plot_number", "")
        p["site_name"]    = sub.get("pd_plots", {}).get("pd_sites", {}).get("name", "")

    photos.sort(key=lambda p: (p.get("stage_number", 0), p.get("submitted_at", "")))
    return jsonify(photos)


# ── Stage Groups API ──────────────────────────────────────────────────────────

DEFAULT_STAGE_GROUPS = [
    {"name": "Groundworks & Structure", "stages": [1,2,3,4,5,6,7,8,9,11]},
    {"name": "Roof",                    "stages": [10,12,13,14]},
    {"name": "Internal Structure",      "stages": [15,16,17,18,19,20]},
    {"name": "1st Fix",                 "stages": [21,22,23,24]},
    {"name": "Plastering & Lining",     "stages": [25]},
    {"name": "2nd Fix & Finishes",      "stages": [26,27,28,29,30,31]},
    {"name": "Finals & Handover",       "stages": [32,33,34,35,36,37]},
]


def _get_stage_groups(tenant_id: str) -> list:
    try:
        res = supabase.table("pd_stage_groups").select("*").eq(
            "tenant_id", tenant_id).order("sort_order").execute()
        if res.data:
            return res.data
        for i, g in enumerate(DEFAULT_STAGE_GROUPS):
            supabase.table("pd_stage_groups").insert({
                "tenant_id":     tenant_id,
                "name":          g["name"],
                "stage_numbers": g["stages"],
                "sort_order":    i,
            }).execute()
        res2 = supabase.table("pd_stage_groups").select("*").eq(
            "tenant_id", tenant_id).order("sort_order").execute()
        return res2.data or []
    except Exception as e:
        print(f"_get_stage_groups error: {e}")
        return []


@portal_bp.route("/api/stage-groups", methods=["GET"])
@_require_login
def api_stage_groups_get(user):
    tenant = _get_tenant()
    tenant_id = tenant.get("id")
    if not tenant_id:
        return jsonify([])
    groups = _get_stage_groups(tenant_id)
    return jsonify(groups)


@portal_bp.route("/api/stage-groups", methods=["POST"])
@_require_admin_role
def api_stage_groups_save(user):
    tenant = _get_tenant()
    tenant_id = tenant.get("id")
    if not tenant_id:
        return jsonify({"ok": False, "error": "No tenant"}), 400

    groups = request.get_json() or []
    if not isinstance(groups, list):
        return jsonify({"ok": False, "error": "Expected array"}), 400

    try:
        supabase.table("pd_stage_groups").delete().eq("tenant_id", tenant_id).execute()
        for i, g in enumerate(groups):
            name   = (g.get("name") or "").strip()
            stages = g.get("stage_numbers") or g.get("stages") or []
            if not name: continue
            supabase.table("pd_stage_groups").insert({
                "tenant_id":     tenant_id,
                "name":          name,
                "stage_numbers": [int(s) for s in stages],
                "sort_order":    i,
            }).execute()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@portal_bp.route("/api/stage-groups/reset", methods=["DELETE"])
@_require_admin_role
def api_stage_groups_reset(user):
    tenant = _get_tenant()
    tenant_id = tenant.get("id")
    if not tenant_id:
        return jsonify({"ok": False, "error": "No tenant"}), 400
    try:
        supabase.table("pd_stage_groups").delete().eq("tenant_id", tenant_id).execute()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ── Checklist Editor (tenant admin page) ─────────────────────────────────────

@portal_bp.route("/api/checklist/visibility/all", methods=["GET"])
@_require_admin_role
def api_all_visibility(user):
    tenant = _get_tenant()
    tenant_id = tenant.get("id")
    if not tenant_id:
        return jsonify([])
    res = supabase.table("pd_stage_visibility").select("*").eq("tenant_id", tenant_id).execute()
    return jsonify(res.data or [])


@portal_bp.route("/checklist")
@_require_admin_role
def checklist_editor(user):
    """Checklist editor page for tenant admins."""
    tenant = _get_tenant()
    from .checklist_data import STAGES
    stages_json = json.dumps({
        str(k): {
            "name": v["name"],
            "applies_to": v.get("applies_to", ""),
            "items": [{"id": i["id"], "text": i["text"], "photo": i.get("photo", True)}
                      for i in v.get("items", [])]
        }
        for k, v in STAGES.items()
    })
    return render_template(
        "pd/portal_checklist.html",
        user=user,
        tenant=tenant,
        stages_json=stages_json,
        admin_key=ADMIN_KEY,
    )


# ── Checklist Editor API ──────────────────────────────────────────────────────

@portal_bp.route("/api/checklist/<int:stage_number>", methods=["GET"])
@_require_admin_role
def api_checklist_get(user, stage_number):
    tenant = _get_tenant()
    tenant_id = tenant.get("id")
    if not tenant_id:
        return jsonify({"overrides": [], "visibility": {}})
    res = supabase.table("pd_checklist_overrides").select("*").eq(
        "tenant_id", tenant_id).eq("stage_number", stage_number).execute()
    vis_res = supabase.table("pd_stage_visibility").select("*").eq(
        "tenant_id", tenant_id).eq("stage_number", stage_number).execute()
    visibility = (vis_res.data or [{}])[0] if vis_res.data else {}
    return jsonify({"overrides": res.data or [], "visibility": visibility})


@portal_bp.route("/api/checklist/<int:stage_number>", methods=["POST"])
@_require_admin_role
def api_checklist_save(user, stage_number):
    tenant = _get_tenant()
    tenant_id = tenant.get("id")
    if not tenant_id:
        return jsonify({"ok": False, "error": "No tenant"}), 400
    items = request.get_json() or []
    if not isinstance(items, list): items = [items]
    for item in items:
        item_id = item.get("item_id")
        if not item_id: continue
        record = {"tenant_id": tenant_id, "stage_number": stage_number, "item_id": item_id,
                  "updated_at": datetime.now(timezone.utc).isoformat()}
        for f in ["text","photo_required","enabled","sort_order","example_photo_url","example_guidance","hidden_for","is_custom"]:
            if f in item: record[f] = item[f]
        supabase.table("pd_checklist_overrides").upsert(
            record, on_conflict="tenant_id,stage_number,item_id").execute()
    return jsonify({"ok": True})


@portal_bp.route("/api/checklist/<int:stage_number>/visibility", methods=["GET"])
@_require_admin_role
def api_stage_visibility_get(user, stage_number):
    tenant = _get_tenant()
    tenant_id = tenant.get("id")
    if not tenant_id:
        return jsonify({})
    res = supabase.table("pd_stage_visibility").select("*").eq(
        "tenant_id", tenant_id).eq("stage_number", stage_number).execute()
    return jsonify((res.data or [{}])[0] if res.data else {})


@portal_bp.route("/api/checklist/<int:stage_number>/visibility", methods=["POST"])
@_require_admin_role
def api_stage_visibility_save(user, stage_number):
    tenant = _get_tenant()
    tenant_id = tenant.get("id")
    if not tenant_id:
        return jsonify({"ok": False, "error": "No tenant"}), 400
    data = request.get_json() or {}
    record = {"tenant_id": tenant_id, "stage_number": stage_number}
    for f in ["hidden_for","hidden_globally","name_override","applies_to_override"]:
        if f in data: record[f] = data[f]
    supabase.table("pd_stage_visibility").upsert(
        record, on_conflict="tenant_id,stage_number").execute()
    return jsonify({"ok": True})


@portal_bp.route("/api/checklist/<int:stage_number>/reset", methods=["DELETE"])
@_require_admin_role
def api_checklist_reset(user, stage_number):
    tenant = _get_tenant()
    tenant_id = tenant.get("id")
    if tenant_id:
        supabase.table("pd_checklist_overrides").delete().eq(
            "tenant_id", tenant_id).eq("stage_number", stage_number).execute()
    return jsonify({"ok": True})


@portal_bp.route("/api/checklist/upload-example", methods=["POST"])
@_require_admin_role
def api_checklist_upload_example(user):
    import uuid as _uuid
    tenant = _get_tenant()
    tenant_id = tenant.get("id")
    photo = request.files.get("photo")
    item_id = request.form.get("item_id", "")
    stage_number = request.form.get("stage_number", "0")
    if not photo or not item_id:
        return jsonify({"ok": False, "error": "Missing photo or item_id"}), 400
    try:
        ext = photo.filename.rsplit(".", 1)[-1].lower() if "." in photo.filename else "jpg"
        fname = f"examples/stage_{stage_number}/{item_id}_{_uuid.uuid4().hex[:8]}.{ext}"
        photo_bytes = photo.read()
        SUPABASE_URL_raw = os.environ.get("SUPABASE_URL", "").rstrip("/")
        SUPABASE_KEY_raw = os.environ.get("SUPABASE_KEY", "")
        import requests as _req
        r = _req.post(
            f"{SUPABASE_URL_raw}/storage/v1/object/pd-photos/{fname}",
            data=photo_bytes,
            headers={"apikey": SUPABASE_KEY_raw, "Authorization": f"Bearer {SUPABASE_KEY_raw}",
                     "Content-Type": f"image/{ext}", "x-upsert": "true"}
        )
        if r.status_code not in (200, 201):
            return jsonify({"ok": False, "error": "Storage upload failed"}), 500
        url = f"{SUPABASE_URL_raw}/storage/v1/object/public/pd-photos/{fname}"
        return jsonify({"ok": True, "url": url})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@portal_bp.route("/api/checklist/item/<item_id>/reset", methods=["DELETE"])
@_require_admin_role
def api_checklist_item_reset(user, item_id):
    tenant = _get_tenant()
    tenant_id = tenant.get("id")
    if tenant_id:
        supabase.table("pd_checklist_overrides").delete().eq(
            "tenant_id", tenant_id).eq("item_id", item_id).execute()
    return jsonify({"ok": True})


# ── User management API ───────────────────────────────────────────────────────

@portal_bp.route("/api/users", methods=["GET"])
@_require_admin_role
def api_users_get(user):
    tenant = _get_tenant()
    if not tenant.get("id"):
        return jsonify([])
    res = supabase.table("pd_users").select(
        "id, name, email, role, site_ids, created_at, last_login"
    ).eq("tenant_id", tenant["id"]).order("name").execute()
    return jsonify(res.data or [])


@portal_bp.route("/api/users", methods=["POST"])
@_require_admin_role
def api_users_create(user):
    data = request.get_json() or {}
    tenant = _get_tenant()
    if not tenant.get("id"):
        return jsonify({"ok": False, "error": "No tenant found"}), 400

    name     = (data.get("name") or "").strip()
    email    = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    role     = data.get("role") or "site_manager"
    site_ids = data.get("site_ids") or []

    if not name or not email or not password:
        return jsonify({"ok": False, "error": "Name, email and password are required"}), 400
    if len(password) < 8:
        return jsonify({"ok": False, "error": "Password must be at least 8 characters"}), 400

    try:
        res = supabase.table("pd_users").insert({
            "tenant_id":     tenant["id"],
            "name":          name,
            "email":         email,
            "password_hash": _hash_password(password),
            "role":          role,
            "site_ids":      json.dumps(site_ids),
        }).execute()

        new_user = res.data[0]

        try:
            assigned_sites = []
            if site_ids:
                sites_res = supabase.table("pd_sites").select("id, name").in_("id", site_ids).execute()
                assigned_sites = sites_res.data or []
            send_welcome_email(new_user, tenant, password, assigned_sites)
        except Exception as email_err:
            print(f"Welcome email error: {email_err}")

        return jsonify({"ok": True, "user": new_user})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@portal_bp.route("/api/users/<user_id>", methods=["PUT"])
@_require_admin_role
def api_users_update(user, user_id):
    data     = request.get_json() or {}
    updates  = {}
    if data.get("name"):     updates["name"]     = data["name"].strip()
    if data.get("email"):    updates["email"]    = data["email"].strip().lower()
    if data.get("role"):     updates["role"]     = data["role"]
    if data.get("site_ids") is not None: updates["site_ids"] = json.dumps(data["site_ids"])
    if data.get("password"):
        if len(data["password"]) < 8:
            return jsonify({"ok": False, "error": "Password must be at least 8 characters"}), 400
        updates["password_hash"] = _hash_password(data["password"])

    if not updates:
        return jsonify({"ok": False, "error": "Nothing to update"}), 400

    old_site_ids = []
    if "site_ids" in updates:
        try:
            old_res = supabase.table("pd_users").select("site_ids").eq("id", user_id).single().execute()
            raw = (old_res.data or {}).get("site_ids") or "[]"
            old_site_ids = json.loads(raw) if isinstance(raw, str) else (raw or [])
        except Exception:
            pass

    supabase.table("pd_users").update(updates).eq("id", user_id).execute()

    if "site_ids" in updates:
        try:
            new_ids_raw = updates["site_ids"]
            new_ids = json.loads(new_ids_raw) if isinstance(new_ids_raw, str) else (new_ids_raw or [])
            added = [s for s in new_ids if s not in old_site_ids]
            if added:
                added_res  = supabase.table("pd_sites").select("id, name").in_("id", added).execute()
                added_sites = added_res.data or []
                if added_sites:
                    u_res = supabase.table("pd_users").select("*").eq("id", user_id).single().execute()
                    send_site_manager_assignment_email(u_res.data or {}, _get_tenant(), added_sites)
        except Exception as e:
            print(f"Site assignment email error: {e}")

    return jsonify({"ok": True})


@portal_bp.route("/api/users/<user_id>", methods=["DELETE"])
@_require_admin_role
def api_users_delete(user, user_id):
    if user_id == user.get("id"):
        return jsonify({"ok": False, "error": "You cannot delete your own account"}), 400
    supabase.table("pd_users").delete().eq("id", user_id).execute()
    return jsonify({"ok": True})
