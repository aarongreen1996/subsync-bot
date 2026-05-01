from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
from reportlab.lib.enums import TA_LEFT, TA_RIGHT, TA_CENTER
from io import BytesIO
from datetime import datetime


def generate_pdf(company, logs, doc_title="Variation Order"):
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        topMargin=18*mm, bottomMargin=18*mm,
        leftMargin=20*mm, rightMargin=20*mm
    )

    brand_hex  = company.get("primary_color", "#1a3a6b")
    brand      = colors.HexColor(brand_hex)
    light_grey = colors.HexColor("#f7f7f7")
    mid_grey   = colors.HexColor("#dddddd")
    dark_grey  = colors.HexColor("#555555")

    story = []

    # ── Company header ────────────────────────────────────────────────────────
    name_style = ParagraphStyle("name", fontSize=22, textColor=brand,
                                fontName="Helvetica-Bold", spaceAfter=1*mm)
    sub_style  = ParagraphStyle("sub",  fontSize=9,  textColor=dark_grey, spaceAfter=1*mm)

    story.append(Paragraph(company.get("company_name", "Company Name"), name_style))
    story.append(Paragraph(company.get("address", ""), sub_style))

    contact_line = " | ".join(filter(None, [
        company.get("phone"),
        company.get("email"),
        f"VAT: {company['vat_number']}" if company.get("vat_number") else None
    ]))
    story.append(Paragraph(contact_line, sub_style))
    story.append(Spacer(1, 3*mm))
    story.append(HRFlowable(width="100%", thickness=2, color=brand))
    story.append(Spacer(1, 4*mm))

    # ── Document title + date ─────────────────────────────────────────────────
    title_style = ParagraphStyle("title", fontSize=15, textColor=brand,
                                 fontName="Helvetica-Bold", spaceAfter=1*mm)
    date_style  = ParagraphStyle("date",  fontSize=9,  textColor=dark_grey, spaceAfter=4*mm)

    story.append(Paragraph(doc_title.upper(), title_style))
    story.append(Paragraph(f"Date: {datetime.now().strftime('%d %B %Y')}", date_style))
    story.append(Spacer(1, 4*mm))

    # ── Items table ───────────────────────────────────────────────────────────
    header_row = ["#", "Description", "Location", "Hrs", "Est. Cost"]
    table_data = [header_row]
    total_cost  = 0.0
    total_hours = 0.0

    for i, log in enumerate(logs, 1):
        cost  = float(log.get("cost_estimate") or 0)
        hours = float(log.get("hours") or 0)
        total_cost  += cost
        total_hours += hours
        table_data.append([
            str(i),
            log.get("description", ""),
            log.get("location", "—"),
            str(hours) if hours else "—",
            f"£{cost:.2f}" if cost else "—",
        ])

    # Totals row
    table_data.append([
        "", "", "",
        f"{total_hours:.1f}" if total_hours else "—",
        f"£{total_cost:.2f}"
    ])

    col_widths = [10*mm, 80*mm, 42*mm, 18*mm, 25*mm]
    tbl = Table(table_data, colWidths=col_widths, repeatRows=1)

    row_count = len(table_data)
    tbl.setStyle(TableStyle([
        # Header
        ("BACKGROUND",    (0, 0), (-1, 0),         brand),
        ("TEXTCOLOR",     (0, 0), (-1, 0),         colors.white),
        ("FONTNAME",      (0, 0), (-1, 0),         "Helvetica-Bold"),
        ("FONTSIZE",      (0, 0), (-1, 0),         9),
        ("BOTTOMPADDING", (0, 0), (-1, 0),         5),
        ("TOPPADDING",    (0, 0), (-1, 0),         5),
        # Data rows
        ("FONTSIZE",      (0, 1), (-1, -1),        9),
        ("TOPPADDING",    (0, 1), (-1, -1),        4),
        ("BOTTOMPADDING", (0, 1), (-1, -1),        4),
        ("ROWBACKGROUNDS",(0, 1), (-1, row_count-2), [colors.white, light_grey]),
        # Totals row
        ("BACKGROUND",    (0, -1), (-1, -1),       light_grey),
        ("FONTNAME",      (0, -1), (-1, -1),       "Helvetica-Bold"),
        ("LINEABOVE",     (0, -1), (-1, -1),       1, brand),
        # Alignment
        ("ALIGN",         (3, 0), (-1, -1),        "RIGHT"),
        ("VALIGN",        (0, 0), (-1, -1),        "MIDDLE"),
        # Grid
        ("GRID",          (0, 0), (-1, -1),        0.4, mid_grey),
    ]))

    story.append(tbl)
    story.append(Spacer(1, 6*mm))

    # ── Summary box ───────────────────────────────────────────────────────────
    summary_data = [
        ["Total Items:",      str(len(logs))],
        ["Total Hours:",      f"{total_hours:.1f} hrs" if total_hours else "—"],
        ["Total Est. Cost:",  f"£{total_cost:.2f}"],
    ]
    sum_tbl = Table(summary_data, colWidths=[50*mm, 40*mm])
    sum_tbl.setStyle(TableStyle([
        ("FONTSIZE",  (0, 0), (-1, -1), 9),
        ("FONTNAME",  (0, 0), (0, -1),  "Helvetica-Bold"),
        ("ALIGN",     (1, 0), (1, -1),  "RIGHT"),
        ("TOPPADDING",(0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0,0),(-1,-1),3),
        ("LINEBELOW", (0, -1),(-1, -1), 1, brand),
    ]))

    # Right-align the summary box
    wrapper = Table([[None, sum_tbl]], colWidths=[85*mm, 90*mm])
    wrapper.setStyle(TableStyle([("VALIGN", (0,0), (-1,-1), "TOP")]))
    story.append(wrapper)
    story.append(Spacer(1, 10*mm))

    # ── Signature section ─────────────────────────────────────────────────────
    sig_data = [
        ["Authorised by:", "", "Client signature:", ""],
        ["", "", "", ""],
        ["_______________________", "", "_______________________", ""],
        ["Name / Date", "", "Name / Date", ""],
    ]
    sig_tbl = Table(sig_data, colWidths=[55*mm, 15*mm, 55*mm, 50*mm])
    sig_tbl.setStyle(TableStyle([
        ("FONTSIZE",      (0, 0), (-1, -1), 8),
        ("TEXTCOLOR",     (0, 0), (-1, -1), dark_grey),
        ("TOPPADDING",    (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    story.append(sig_tbl)
    story.append(Spacer(1, 8*mm))
    story.append(HRFlowable(width="100%", thickness=0.5, color=mid_grey))
    story.append(Spacer(1, 2*mm))

    # ── Footer ────────────────────────────────────────────────────────────────
    footer_style = ParagraphStyle("footer", fontSize=7, textColor=colors.grey,
                                  alignment=TA_CENTER)
    story.append(Paragraph(
        f"Generated by SubSync on {datetime.now().strftime('%d/%m/%Y at %H:%M')} · "
        "All variations subject to client approval.",
        footer_style
    ))

    doc.build(story)
    buffer.seek(0)
    return buffer.getvalue()
