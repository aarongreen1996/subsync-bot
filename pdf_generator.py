from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable, Image
from reportlab.lib.enums import TA_LEFT, TA_RIGHT, TA_CENTER
from io import BytesIO
from datetime import datetime
import requests
import os


def fetch_logo(logo_url):
    if not logo_url:
        return None
    try:
        r = requests.get(logo_url, timeout=5)
        if r.status_code == 200 and len(r.content) > 0:
            return BytesIO(r.content)
    except Exception as e:
        print(f"Logo fetch error: {e}")
    return None


def generate_pdf(company, logs, doc_title="Variation Order", doc_ref="VO-001", site_label=None):
    """Route to the correct PDF layout based on document type."""
    project_info = company.get("project_info", {}) or {}
    if doc_title == "Purchase Order":
        return _generate_po(company, logs, doc_ref, site_label, project_info)
    elif doc_title == "Site Report":
        # Combined report — all types together, use VO layout with ALL items
        return _generate_vo_or_ds(company, logs, "Site Report", doc_ref, site_label, project_info)
    else:
        return _generate_vo_or_ds(company, logs, doc_title, doc_ref, site_label, project_info)


# ── PURCHASE ORDER ────────────────────────────────────────────────────────────
def _generate_po(company, logs, doc_ref, site_label, project_info=None):
    buffer    = BytesIO()
    brand_hex = company.get("primary_color", "#f59e0b")
    brand     = colors.HexColor(brand_hex)
    light     = colors.HexColor("#f7f7f7")
    mid       = colors.HexColor("#dddddd")
    dark      = colors.HexColor("#444444")

    doc = SimpleDocTemplate(buffer, pagesize=A4,
        topMargin=18*mm, bottomMargin=18*mm,
        leftMargin=20*mm, rightMargin=20*mm)
    story = []

    # Styles — unique per call to avoid ReportLab style name conflicts
    _uid = id(logs)
    def sty(name, **kw): return ParagraphStyle(f"{name}_{_uid}", **kw)
    co_name_sty  = sty("pcn",  fontSize=18, textColor=brand, fontName="Helvetica-Bold", leading=22)
    small_sty    = sty("psm",  fontSize=8,  textColor=dark, leading=12)
    po_sty       = sty("ppo",  fontSize=22, textColor=brand, fontName="Helvetica-Bold",
                        alignment=TA_RIGHT, leading=26)
    ref_sty      = sty("prf",  fontSize=8,  textColor=dark, alignment=TA_RIGHT, leading=12)
    label_sty    = sty("plb",  fontSize=7,  textColor=colors.grey, fontName="Helvetica-Bold",
                        leading=10, spaceAfter=1)
    # Cell styles — Paragraph wraps automatically by column width
    cell_sty     = sty("pcl",  fontSize=9,  textColor=dark, leading=13)
    cell_r_sty   = sty("pclr", fontSize=9,  textColor=dark, leading=13, alignment=TA_RIGHT)
    hdr_sty      = sty("phd",  fontSize=9,  textColor=colors.white, fontName="Helvetica-Bold",
                        leading=13)
    footer_sty   = sty("pft",  fontSize=7,  textColor=colors.grey, alignment=TA_CENTER)
    total_sty    = sty("ptt",  fontSize=9,  textColor=dark, fontName="Helvetica-Bold",
                        leading=13, alignment=TA_RIGHT)

    # Logo
    logo_img  = None
    logo_data = fetch_logo(company.get("logo_url"))
    if logo_data:
        try:
            logo_img = Image(logo_data, width=40*mm, height=20*mm, kind="proportional")
        except Exception:
            logo_img = None

    # ── HEADER ─────────────────────────────────────────────────────────────
    contact = " | ".join(filter(None, [(company.get("phone") or ""), (company.get("email") or "")]))
    vat     = f"VAT: {company['vat_number']}" if (company.get("vat_number") or "") else ""

    left_col = [
        logo_img if logo_img else Paragraph((company.get("company_name") or ""), co_name_sty),
        Spacer(1, 2*mm),
        Paragraph((company.get("company_name") or "") if logo_img else "", small_sty),
        Paragraph((company.get("address") or ""), small_sty),
        Paragraph(contact, small_sty),
        Paragraph(vat, small_sty),
    ]

    right_col = [
        Paragraph("PURCHASE ORDER", po_sty),
        Spacer(1, 2*mm),
        Paragraph(f"PO Ref: {doc_ref}", ref_sty),
        Paragraph(f"Date: {datetime.now().strftime('%d %B %Y')}", ref_sty),
        Paragraph(f"Site: {site_label or 'All Sites'}", ref_sty),
    ]

    header = Table([[left_col, right_col]], colWidths=[110*mm, 65*mm])
    header.setStyle(TableStyle([
        ("VALIGN", (0,0), (-1,-1), "TOP"),
        ("LEFTPADDING",  (0,0), (-1,-1), 0),
        ("RIGHTPADDING", (0,0), (-1,-1), 0),
    ]))
    story.append(header)
    story.append(Spacer(1, 4*mm))
    story.append(HRFlowable(width="100%", thickness=2, color=brand))
    story.append(Spacer(1, 5*mm))

    # ── SUPPLIER & DELIVERY BOXES ───────────────────────────────────────────
    suppliers   = list(set(l.get("supplier", "") or "" for l in logs if l.get("supplier")))
    supplier_name = suppliers[0] if suppliers else "___________________________"

    box_sty = sty("bx", fontSize=9, textColor=dark, leading=14)
    box_lbl = sty("bl", fontSize=7, textColor=colors.grey, fontName="Helvetica-Bold",
                   leading=10, spaceBefore=0)

    supplier_content = [
        Paragraph("SUPPLIER", box_lbl), Spacer(1, 1*mm),
        Paragraph(f"<b>{supplier_name}</b>", box_sty), Spacer(1, 2*mm),
        Paragraph("Address: ___________________________", box_sty),
        Paragraph("___________________________", box_sty), Spacer(1, 2*mm),
        Paragraph("Tel: ___________________________", box_sty),
        Paragraph("Email: ___________________________", box_sty),
    ]

    project_info = project_info or {}
    delivery_content = [
        Paragraph("DELIVER TO", box_lbl), Spacer(1, 1*mm),
        Paragraph(f"<b>{company.get('company_name', '')}</b>", box_sty), Spacer(1, 2*mm),
        Paragraph(f"Site: {site_label or '___________________________'}", box_sty), Spacer(1, 2*mm),
        Paragraph((company.get("address") or "___________________________"), box_sty), Spacer(1, 2*mm),
        Paragraph(f"Contact: {company.get('phone', '___________________________')}", box_sty),
    ]

    box_tbl = Table([[supplier_content, delivery_content]], colWidths=[87*mm, 87*mm])
    box_tbl.setStyle(TableStyle([
        ("VALIGN",        (0,0), (-1,-1), "TOP"),
        ("BOX",           (0,0), (0,0),   0.5, mid),
        ("BOX",           (1,0), (1,0),   0.5, mid),
        ("BACKGROUND",    (0,0), (-1,-1), light),
        ("LEFTPADDING",   (0,0), (-1,-1), 6),
        ("RIGHTPADDING",  (0,0), (-1,-1), 6),
        ("TOPPADDING",    (0,0), (-1,-1), 6),
        ("BOTTOMPADDING", (0,0), (-1,-1), 6),
    ]))
    story.append(box_tbl)
    story.append(Spacer(1, 6*mm))

    # ── ORDER ITEMS TABLE — use Paragraph for wrapping ──────────────────────
    # Column widths: #, Description, Notes/Location, Qty, Unit Cost, Total
    col_w = [10*mm, 70*mm, 36*mm, 14*mm, 22*mm, 18*mm]  # total 170mm

    header_row = [
        Paragraph("#",               hdr_sty),
        Paragraph("Item / Description", hdr_sty),
        Paragraph("Notes / Location",   hdr_sty),
        Paragraph("Qty",             hdr_sty),
        Paragraph("Unit Cost",       hdr_sty),
        Paragraph("Total",           hdr_sty),
    ]
    table_data = [header_row]
    total = 0.0

    for i, log in enumerate(logs, 1):
        cost  = float(log.get("cost_estimate") or 0)
        total += cost
        materials = log.get("materials") or []
        mat_str   = ", ".join(materials) if isinstance(materials, list) and materials else ""
        desc = (log.get("description") or "") or ""
        if mat_str and mat_str.lower() not in desc.lower():
            desc = mat_str or desc

        table_data.append([
            Paragraph(str(i),                                           cell_sty),
            Paragraph(desc,                                             cell_sty),
            Paragraph((log.get("location") or "") or "—",                  cell_sty),
            Paragraph("—",                                              cell_r_sty),
            Paragraph(f"£{cost:.2f}" if cost else "—",                 cell_r_sty),
            Paragraph(f"£{cost:.2f}" if cost else "—",                 cell_r_sty),
        ])

    table_data.append([
        Paragraph("", cell_sty),
        Paragraph("", cell_sty),
        Paragraph("", cell_sty),
        Paragraph("", cell_sty),
        Paragraph("TOTAL", total_sty),
        Paragraph(f"£{total:.2f}", total_sty),
    ])

    rc  = len(table_data)
    tbl = Table(table_data, colWidths=col_w, repeatRows=1)
    tbl.setStyle(TableStyle([
        ("BACKGROUND",    (0,0),  (-1,0),        brand),
        ("TOPPADDING",    (0,0),  (-1,0),        6),
        ("BOTTOMPADDING", (0,0),  (-1,0),        6),
        ("FONTSIZE",      (0,1),  (-1,-1),       9),
        ("TOPPADDING",    (0,1),  (-1,-1),       5),
        ("BOTTOMPADDING", (0,1),  (-1,-1),       5),
        ("ROWBACKGROUNDS",(0,1),  (-1,rc-2),     [colors.white, light]),
        ("BACKGROUND",    (0,-1), (-1,-1),       light),
        ("LINEABOVE",     (0,-1), (-1,-1),       1.5, brand),
        ("ALIGN",         (3,0),  (-1,-1),       "RIGHT"),
        ("VALIGN",        (0,0),  (-1,-1),       "TOP"),
        ("GRID",          (0,0),  (-1,-1),       0.4, mid),
        ("LEFTPADDING",   (0,0),  (-1,-1),       6),
        ("RIGHTPADDING",  (0,0),  (-1,-1),       6),
    ]))
    story.append(tbl)
    story.append(Spacer(1, 8*mm))

    # ── TERMS ───────────────────────────────────────────────────────────────
    terms_sty = sty("tr", fontSize=8, textColor=dark, leading=13)
    story.append(Paragraph(
        "<b>Terms:</b>  Payment on invoice · This PO constitutes an order agreement · "
        "Please confirm receipt by email · Include this PO reference on all correspondence and invoices.",
        terms_sty))
    story.append(Spacer(1, 8*mm))

    # ── ORDER CONFIRMATION ───────────────────────────────────────────────────
    conf_label_sty = sty("cl2", fontSize=8, textColor=dark, fontName="Helvetica-Bold", leading=12)
    conf_data = [
        [Paragraph("ORDER CONFIRMATION (Supplier to complete and return)", conf_label_sty), "", "", ""],
        ["Confirmed by:", "___________________________", "Expected delivery date:", "___________________________"],
        ["Supplier ref:", "___________________________", "Delivery contact:", "___________________________"],
        ["Notes:", "___________________________", "", ""],
    ]
    conf_tbl = Table(conf_data, colWidths=[35*mm, 57*mm, 42*mm, 41*mm])
    conf_tbl.setStyle(TableStyle([
        ("SPAN",          (0,0), (-1,0)),
        ("BACKGROUND",    (0,0), (-1,0),  brand),
        ("TEXTCOLOR",     (0,0), (-1,0),  colors.white),
        ("FONTSIZE",      (0,0), (-1,-1), 8),
        ("TOPPADDING",    (0,0), (-1,-1), 5),
        ("BOTTOMPADDING", (0,0), (-1,-1), 5),
        ("LEFTPADDING",   (0,0), (-1,-1), 6),
        ("RIGHTPADDING",  (0,0), (-1,-1), 6),
        ("BOX",           (0,0), (-1,-1), 0.5, mid),
        ("INNERGRID",     (0,1), (-1,-1), 0.3, mid),
        ("VALIGN",        (0,0), (-1,-1), "MIDDLE"),
    ]))
    story.append(conf_tbl)
    story.append(Spacer(1, 6*mm))

    # ── SIGNATURE ────────────────────────────────────────────────────────────
    sig_data = [
        ["Authorised by:", "___________________________", "Position:", "___________________________"],
        ["Signature:",     "___________________________", "Date:",     "___________________________"],
    ]
    sig_tbl = Table(sig_data, colWidths=[30*mm, 60*mm, 25*mm, 60*mm])
    sig_tbl.setStyle(TableStyle([
        ("FONTSIZE",      (0,0), (-1,-1), 8),
        ("TEXTCOLOR",     (0,0), (-1,-1), dark),
        ("TOPPADDING",    (0,0), (-1,-1), 4),
        ("BOTTOMPADDING", (0,0), (-1,-1), 4),
        ("FONTNAME",      (0,0), (0,-1),  "Helvetica-Bold"),
        ("FONTNAME",      (2,0), (2,-1),  "Helvetica-Bold"),
    ]))
    story.append(sig_tbl)
    story.append(Spacer(1, 6*mm))
    story.append(HRFlowable(width="100%", thickness=0.5, color=mid))
    story.append(Spacer(1, 2*mm))
    story.append(Paragraph(
        f"{doc_ref} · Generated by Note2Quote on {datetime.now().strftime('%d/%m/%Y at %H:%M')} "
        f"· Please quote PO reference on all invoices and delivery notes.",
        footer_sty))

    doc.build(story)
    buffer.seek(0)
    return buffer.getvalue()


# ── VARIATION ORDER / DAYWORK SHEET ──────────────────────────────────────────
def _generate_vo_or_ds(company, logs, doc_title, doc_ref, site_label, project_info=None):
    buffer    = BytesIO()
    brand_hex = company.get("primary_color", "#1a3a6b")
    brand     = colors.HexColor(brand_hex)
    light     = colors.HexColor("#f7f7f7")
    mid       = colors.HexColor("#dddddd")
    dark      = colors.HexColor("#555555")

    doc = SimpleDocTemplate(buffer, pagesize=A4,
        topMargin=18*mm, bottomMargin=18*mm,
        leftMargin=20*mm, rightMargin=20*mm)
    story = []

    _uid = id(logs)  # unique per call to avoid ReportLab style name conflicts
    def sty(name, **kw): return ParagraphStyle(f"{name}_{_uid}", **kw)

    name_sty    = sty("vn",  fontSize=18, textColor=brand, fontName="Helvetica-Bold", leading=22)
    sub_sty     = sty("vs",  fontSize=8,  textColor=dark, leading=13)
    ref_sty     = sty("vr",  fontSize=11, textColor=brand, fontName="Helvetica-Bold",
                       alignment=TA_RIGHT, leading=16)
    ref_sub_sty = sty("vrs", fontSize=8,  textColor=dark, alignment=TA_RIGHT, leading=13)
    footer_sty  = sty("vf",  fontSize=7,  textColor=colors.grey, alignment=TA_CENTER)
    # Cell styles — no wordWrap param needed, Paragraph wraps by column width automatically
    cell_sty    = sty("vc",  fontSize=9,  textColor=dark, leading=13)
    cell_r_sty  = sty("vcr", fontSize=9,  textColor=dark, leading=13, alignment=TA_RIGHT)
    hdr_sty     = sty("vh",  fontSize=9,  textColor=colors.white, fontName="Helvetica-Bold", leading=13)
    total_sty   = sty("vt",  fontSize=9,  textColor=dark, fontName="Helvetica-Bold",
                       leading=13, alignment=TA_RIGHT)

    contact = " | ".join(filter(None, [(company.get("phone") or ""), (company.get("email") or "")]))
    vat     = f"VAT No: {company['vat_number']}" if (company.get("vat_number") or "") else ""

    logo_img  = None
    logo_data = fetch_logo(company.get("logo_url"))
    if logo_data:
        try:
            logo_img = Image(logo_data, width=40*mm, height=20*mm, kind="proportional")
        except Exception:
            logo_img = None

    if logo_img:
        left_col = [logo_img, Spacer(1, 2*mm),
                    Paragraph((company.get("company_name") or ""), sub_sty),
                    Paragraph((company.get("address") or ""), sub_sty),
                    Paragraph(contact, sub_sty), Paragraph(vat, sub_sty)]
    else:
        left_col = [Paragraph((company.get("company_name") or "Company Name"), name_sty),
                    Paragraph((company.get("address") or ""), sub_sty),
                    Paragraph(contact, sub_sty), Paragraph(vat, sub_sty)]

    right_col = [
        Paragraph(doc_title.upper(), ref_sty),
        Paragraph(f"Ref: {doc_ref}", ref_sub_sty),
        Paragraph(f"Date: {datetime.now().strftime('%d %B %Y')}", ref_sub_sty),
    ]
    if site_label:
        right_col.append(Paragraph(f"Site: {site_label}", ref_sub_sty))

    header = Table([[left_col, right_col]], colWidths=[110*mm, 65*mm])
    header.setStyle(TableStyle([
        ("VALIGN", (0,0), (-1,-1), "TOP"),
        ("LEFTPADDING",  (0,0), (-1,-1), 0),
        ("RIGHTPADDING", (0,0), (-1,-1), 0),
    ]))
    story.append(header)
    story.append(Spacer(1, 4*mm))
    story.append(HRFlowable(width="100%", thickness=2, color=brand))

    # Client box
    project_info = project_info or {}
    client_name  = (project_info.get("client_name") or "")
    client_addr  = (project_info.get("client_address") or "") or (project_info.get("address") or "")
    client_email = (project_info.get("client_email") or "")
    client_phone = (project_info.get("client_phone") or "")

    if any([client_name, client_addr, client_email, client_phone]):
        box_lbl = sty("bl2", fontSize=7, textColor=colors.grey,
                       fontName="Helvetica-Bold", leading=10)
        box_sty2 = sty("bx2", fontSize=9, textColor=dark, leading=13)
        client_lines = []
        if client_name:  client_lines.append(Paragraph(f"<b>{client_name}</b>", box_sty2))
        if client_addr:  client_lines.append(Paragraph(client_addr, box_sty2))
        if client_email: client_lines.append(Paragraph(client_email, box_sty2))
        if client_phone: client_lines.append(Paragraph(client_phone, box_sty2))

        client_content = [Paragraph("CLIENT / INSTRUCTED BY", box_lbl), Spacer(1, 1*mm)] + client_lines
        client_tbl = Table([[client_content]], colWidths=[175*mm])
        client_tbl.setStyle(TableStyle([
            ("BOX",           (0,0), (-1,-1), 0.5, colors.HexColor("#dddddd")),
            ("BACKGROUND",    (0,0), (-1,-1), colors.HexColor("#f7f7f7")),
            ("LEFTPADDING",   (0,0), (-1,-1), 8),
            ("RIGHTPADDING",  (0,0), (-1,-1), 8),
            ("TOPPADDING",    (0,0), (-1,-1), 6),
            ("BOTTOMPADDING", (0,0), (-1,-1), 6),
        ]))
        story.append(Spacer(1, 4*mm))
        story.append(client_tbl)
        story.append(Spacer(1, 6*mm))
    else:
        story.append(Spacer(1, 6*mm))

    # ── ITEMS TABLE — use Paragraph for wrapping ─────────────────────────────
    col_w = [10*mm, 80*mm, 38*mm, 17*mm, 25*mm]  # total 170mm = A4 width minus 40mm margins

    header_row = [
        Paragraph("#",           hdr_sty),
        Paragraph("Description", hdr_sty),
        Paragraph("Location",    hdr_sty),
        Paragraph("Hrs",         hdr_sty),
        Paragraph("Est. Cost",   hdr_sty),
    ]
    table_data  = [header_row]
    total_cost  = 0.0
    total_hours = 0.0

    for i, log in enumerate(logs, 1):
        cost  = float(log.get("cost_estimate") or 0)
        hours = float(log.get("hours") or 0)
        total_cost  += cost
        total_hours += hours
        table_data.append([
            Paragraph(str(i),                                              cell_sty),
            Paragraph((log.get("description") or "") or "",                    cell_sty),
            Paragraph((log.get("location") or "") or "—",                      cell_sty),
            Paragraph(str(hours) if hours else "—",                        cell_r_sty),
            Paragraph(f"£{cost:.2f}" if cost else "—",                     cell_r_sty),
        ])

    table_data.append([
        Paragraph("", cell_sty),
        Paragraph("", cell_sty),
        Paragraph("", cell_sty),
        Paragraph(f"{total_hours:.1f}" if total_hours else "—", total_sty),
        Paragraph(f"£{total_cost:.2f}",                         total_sty),
    ])

    rc  = len(table_data)
    tbl = Table(table_data, colWidths=col_w, repeatRows=1)
    tbl.setStyle(TableStyle([
        ("BACKGROUND",    (0,0),  (-1,0),        brand),
        ("TOPPADDING",    (0,0),  (-1,0),        6),
        ("BOTTOMPADDING", (0,0),  (-1,0),        6),
        ("TOPPADDING",    (0,1),  (-1,-1),       5),
        ("BOTTOMPADDING", (0,1),  (-1,-1),       5),
        ("ROWBACKGROUNDS",(0,1),  (-1,rc-2),     [colors.white, light]),
        ("BACKGROUND",    (0,-1), (-1,-1),       light),
        ("LINEABOVE",     (0,-1), (-1,-1),       1.5, brand),
        ("ALIGN",         (3,0),  (-1,-1),       "RIGHT"),
        ("VALIGN",        (0,0),  (-1,-1),       "TOP"),
        ("GRID",          (0,0),  (-1,-1),       0.4, mid),
        ("LEFTPADDING",   (0,0),  (-1,-1),       6),
        ("RIGHTPADDING",  (0,0),  (-1,-1),       6),
    ]))
    story.append(tbl)
    story.append(Spacer(1, 6*mm))

    # Summary
    summary_data = [
        ["Total Items:",     str(len(logs))],
        ["Total Hours:",     f"{total_hours:.1f} hrs" if total_hours else "—"],
        ["Total Est. Cost:", f"£{total_cost:.2f}"],
    ]
    sum_tbl = Table(summary_data, colWidths=[50*mm, 40*mm])
    sum_tbl.setStyle(TableStyle([
        ("FONTSIZE",      (0,0), (-1,-1), 9),
        ("FONTNAME",      (0,0), (0,-1),  "Helvetica-Bold"),
        ("ALIGN",         (1,0), (1,-1),  "RIGHT"),
        ("TOPPADDING",    (0,0), (-1,-1), 3),
        ("BOTTOMPADDING", (0,0), (-1,-1), 3),
        ("LINEBELOW",     (0,-1),(-1,-1), 1.5, brand),
    ]))
    wrapper = Table([[None, sum_tbl]], colWidths=[85*mm, 90*mm])
    wrapper.setStyle(TableStyle([("VALIGN", (0,0), (-1,-1), "TOP")]))
    story.append(wrapper)
    story.append(Spacer(1, 10*mm))

    # Signatures
    sig_data = [
        ["Authorised by:", "", "Client signature:", ""],
        [" ", "", " ", ""],
        ["_______________________", "", "_______________________", ""],
        ["Name / Date", "", "Name / Date", ""],
    ]
    sig_tbl = Table(sig_data, colWidths=[60*mm, 10*mm, 60*mm, 45*mm])
    sig_tbl.setStyle(TableStyle([
        ("FONTSIZE",      (0,0), (-1,-1), 8),
        ("TEXTCOLOR",     (0,0), (-1,-1), dark),
        ("TOPPADDING",    (0,0), (-1,-1), 4),
        ("BOTTOMPADDING", (0,0), (-1,-1), 4),
    ]))
    story.append(sig_tbl)
    story.append(Spacer(1, 8*mm))
    story.append(HRFlowable(width="100%", thickness=0.5, color=mid))
    story.append(Spacer(1, 2*mm))
    story.append(Paragraph(
        f"{doc_ref} · Generated by Note2Quote on {datetime.now().strftime('%d/%m/%Y at %H:%M')} "
        f"· All variations subject to client approval.",
        footer_sty))

    doc.build(story)
    buffer.seek(0)
    return buffer.getvalue()
