═══════════════════════════════════════════════════════════════════
PATCH 1 — Replace _get_stage_with_overrides entirely
═══════════════════════════════════════════════════════════════════

FIND (the whole function, near the bottom of the file):

def _get_stage_with_overrides(stage_number: int, tenant_id: str = None) -> dict:
    """Return stage items merged with any tenant overrides."""
    from .checklist_data import get_stage
    stage = get_stage(stage_number)
    if not stage:
        return {}
    if not tenant_id:
        return stage

    try:
        res = supabase.table("pd_checklist_overrides").select("*").eq(
            "tenant_id", tenant_id
        ).eq("stage_number", stage_number).execute()
        ov_map = {o["item_id"]: o for o in (res.data or [])}
    except Exception:
        ov_map = {}

    if not ov_map:
        return stage

    import copy
    stage_copy = copy.deepcopy(stage)
    merged = []
    for item in stage_copy.get("items", []):
        ov = ov_map.get(item["id"], {})
        if ov.get("enabled") is False:
            continue
        if ov.get("text"):
            item["text"] = ov["text"]
        if "photo_required" in ov and ov["photo_required"] is not None:
            item["photo"] = bool(ov["photo_required"])
        if ov.get("example_photo_url"):
            item["example_photo_url"] = ov["example_photo_url"]
        if ov.get("example_guidance"):
            item["example_guidance"] = ov["example_guidance"]
        merged.append(item)

    # Add any custom items (item_id starts with custom_)
    for item_id, ov in ov_map.items():
        if item_id.startswith("custom_") and ov.get("enabled", True):
            merged.append({
                "id":    item_id,
                "text":  ov.get("text", ""),
                "type":  "check",
                "photo": ov.get("photo_required", True),
                "example_photo_url": ov.get("example_photo_url", ""),
                "example_guidance":  ov.get("example_guidance", ""),
            })

    stage_copy["items"] = merged
    return stage_copy


REPLACE WITH:

def _get_stage_with_overrides(stage_number: int, tenant_id: str = None, dwelling_category: str = "house"):
    """Return stage merged with tenant overrides and filtered for the given
    dwelling type. Returns None if the stage is hidden entirely or hidden for
    this dwelling type — callers must check for None and skip the stage."""
    from .checklist_data import get_stage
    stage = get_stage(stage_number)
    if not stage:
        return None
    if not tenant_id:
        return stage

    import copy
    stage_copy = copy.deepcopy(stage)

    # Stage-level visibility / name overrides
    try:
        vis_res = supabase.table("pd_stage_visibility").select("*").eq(
            "tenant_id", tenant_id
        ).eq("stage_number", stage_number).single().execute()
        vis = vis_res.data or {}
    except Exception:
        vis = {}

    if vis.get("hidden_globally"):
        return None
    hidden_for = vis.get("hidden_for") or []
    if dwelling_category in hidden_for:
        return None
    if vis.get("name_override"):
        stage_copy["name"] = vis["name_override"]
    if vis.get("applies_to_override"):
        stage_copy["applies_to"] = vis["applies_to_override"]

    # Item-level overrides
    try:
        res = supabase.table("pd_checklist_overrides").select("*").eq(
            "tenant_id", tenant_id
        ).eq("stage_number", stage_number).execute()
        ov_map = {o["item_id"]: o for o in (res.data or [])}
    except Exception:
        ov_map = {}

    merged = []
    for item in stage_copy.get("items", []):
        ov = ov_map.get(item["id"], {})
        if ov.get("enabled") is False:
            continue
        item_hidden_for = ov.get("hidden_for") or []
        if dwelling_category in item_hidden_for:
            continue
        if "text" in ov and ov["text"]:
            item["text"] = ov["text"]
        if "photo_required" in ov and ov["photo_required"] is not None:
            item["photo"] = bool(ov["photo_required"])
        if ov.get("example_photo_url"):
            item["example_photo_url"] = ov["example_photo_url"]
        if ov.get("example_guidance"):
            item["example_guidance"] = ov["example_guidance"]
        merged.append(item)

    # Custom items (any item_id not in the default item set, flagged is_custom
    # or legacy custom_ prefix)
    default_ids = {i["id"] for i in stage.get("items", [])}
    for item_id, ov in ov_map.items():
        if item_id in default_ids:
            continue
        if not (ov.get("is_custom") or item_id.startswith("custom_")):
            continue
        if ov.get("enabled") is False:
            continue
        item_hidden_for = ov.get("hidden_for") or []
        if dwelling_category in item_hidden_for:
            continue
        merged.append({
            "id":    item_id,
            "text":  ov.get("text", ""),
            "type":  "check",
            "photo": ov.get("photo_required", True),
            "example_photo_url": ov.get("example_photo_url", ""),
            "example_guidance":  ov.get("example_guidance", ""),
        })

    stage_copy["items"] = merged
    return stage_copy


def _get_custom_stages(tenant_id: str, dwelling_category: str = "house") -> dict:
    """Return any tenant-created custom stages (stage_number >= 1000) that
    aren't hidden for this dwelling type, keyed by stage number."""
    if not tenant_id:
        return {}
    try:
        vis_res = supabase.table("pd_stage_visibility").select("*").eq(
            "tenant_id", tenant_id
        ).gte("stage_number", 1000).execute()
        vis_rows = vis_res.data or []
    except Exception:
        vis_rows = []

    if not vis_rows:
        return {}

    try:
        ov_res = supabase.table("pd_checklist_overrides").select("*").eq(
            "tenant_id", tenant_id
        ).execute()
        overrides = ov_res.data or []
    except Exception:
        overrides = []

    ov_by_stage = {}
    for o in overrides:
        ov_by_stage.setdefault(o["stage_number"], {})[o["item_id"]] = o

    custom = {}
    for v in vis_rows:
        n = v["stage_number"]
        if v.get("hidden_globally"):
            continue
        if dwelling_category in (v.get("hidden_for") or []):
            continue
        items = []
        for item_id, ov in ov_by_stage.get(n, {}).items():
            if ov.get("enabled") is False:
                continue
            if dwelling_category in (ov.get("hidden_for") or []):
                continue
            items.append({
                "id": item_id,
                "text": ov.get("text",""),
                "type": "check",
                "photo": ov.get("photo_required", True),
                "example_photo_url": ov.get("example_photo_url",""),
                "example_guidance":  ov.get("example_guidance",""),
            })
        custom[n] = {
            "name": v.get("name_override") or f"Custom Stage {n}",
            "applies_to": v.get("applies_to_override",""),
            "items": items,
        }
    return custom


═══════════════════════════════════════════════════════════════════
PATCH 2 — form() route: filter stages by plot's dwelling type
═══════════════════════════════════════════════════════════════════

FIND:

    plot = {"id": plot_row["id"], "plot_number": plot_row["plot_number"], "token": token}
    site = {"name": plot_row["pd_sites"]["name"], "address": plot_row["pd_sites"].get("address", "")}
    tenant = _get_tenant()
    tenant_id = tenant.get("id")

    # Build stages with overrides applied
    stages_with_overrides = {}
    for k, v in STAGES.items():
        merged = _get_stage_with_overrides(k, tenant_id)
        stages_with_overrides[k] = merged

    stages_json = json.dumps(stages_with_overrides, ensure_ascii=False)
    stages_by_group = get_stages_by_group()

REPLACE WITH:

    plot = {"id": plot_row["id"], "plot_number": plot_row["plot_number"], "token": token}
    site = {"name": plot_row["pd_sites"]["name"], "address": plot_row["pd_sites"].get("address", "")}
    tenant = _get_tenant()
    tenant_id = tenant.get("id")
    dwelling_category = plot_row.get("dwelling_category") or "house"

    # Build stages with overrides applied, dropping any hidden for this dwelling type
    stages_with_overrides = {}
    for k, v in STAGES.items():
        merged = _get_stage_with_overrides(k, tenant_id, dwelling_category)
        if merged is not None:
            stages_with_overrides[k] = merged

    # Add tenant custom stages (not hidden for this dwelling type)
    custom_stages = _get_custom_stages(tenant_id, dwelling_category)
    stages_with_overrides.update(custom_stages)

    stages_json = json.dumps(stages_with_overrides, ensure_ascii=False)

    # Filter stages_by_group to only include visible stages, and append a
    # Custom group for any tenant-added stages
    stages_by_group = []
    for group in get_stages_by_group():
        visible_nums = [n for n in group.get("stages", []) if n in stages_with_overrides]
        if visible_nums:
            stages_by_group.append({**group, "stages": visible_nums})
    if custom_stages:
        stages_by_group.append({"name": "Custom Stages", "stages": list(custom_stages.keys())})

═══════════════════════════════════════════════════════════════════
PATCH 3 — resubmit_form() route: same filtering
═══════════════════════════════════════════════════════════════════

FIND:

    tenant = _get_tenant()
    tenant_id = tenant.get("id")
    stages_with_overrides = {}
    for k, v in STAGES.items():
        merged = _get_stage_with_overrides(k, tenant_id)
        stages_with_overrides[k] = merged
    stages_json    = json.dumps(stages_with_overrides, ensure_ascii=False)
    stages_by_group = get_stages_by_group()

REPLACE WITH:

    tenant = _get_tenant()
    tenant_id = tenant.get("id")
    dwelling_category = plot_row.get("dwelling_category") or "house"

    stages_with_overrides = {}
    for k, v in STAGES.items():
        merged = _get_stage_with_overrides(k, tenant_id, dwelling_category)
        if merged is not None:
            stages_with_overrides[k] = merged

    custom_stages = _get_custom_stages(tenant_id, dwelling_category)
    stages_with_overrides.update(custom_stages)

    stages_json = json.dumps(stages_with_overrides, ensure_ascii=False)

    stages_by_group = []
    for group in get_stages_by_group():
        visible_nums = [n for n in group.get("stages", []) if n in stages_with_overrides]
        if visible_nums:
            stages_by_group.append({**group, "stages": visible_nums})
    if custom_stages:
        stages_by_group.append({"name": "Custom Stages", "stages": list(custom_stages.keys())})

═══════════════════════════════════════════════════════════════════
NOTE on /admin/checklist-editor (superadmin route)
═══════════════════════════════════════════════════════════════════
This route still uses raw STAGES — leave it alone, it's the legacy
superadmin tool and unaffected by these changes. The portal checklist
editor at /pd/portal/checklist is now the primary editor.
