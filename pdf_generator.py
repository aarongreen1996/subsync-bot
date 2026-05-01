from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
from reportlab.lib.enums import TA_LEFT, TA_RIGHT, TA_CENTER
from io import BytesIO
from datetime import datetime


def generate_pdf(company, logs, doc_title="Variation Order", doc_ref="VO-001", site_label=None):
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

    # ── Header: two-column layout (company left, ref right) ───────────────────
    name_style = ParagraphStyle(
        "name", fontSize=18, textColor=brand,
        fontName="Helvetica-Bold", leading=22
    )
    sub_style = ParagraphStyle(
        "sub", fontSize=8, textColor=dark_grey, leading=13
    )
    ref_style = ParagraphStyle(
        "ref", fontSize=11, textColor=brand,
        fontName="Helvetica-Bold", alignment=TA_RIGHT, leading=16
    )
    ref_sub_style = ParagraphStyle(
        "refsub", fontSize=8, textColor=dark_grey,
        alignment=TA_RIGHT, leading=13
    )

    contact_line = " | ".join(filter(None, [
        company.get("phone"),
        company.get("email"),
    ]))
    vat_line = f"VAT No: {company['vat_number']}" if company.get("vat_number") else ""

    left_col = [
        Paragraph(company.get("company_name", "Company Name"), name_style),
        Paragraph(company.get("address", ""), sub_style),
        Paragraph(contact_line, sub_style),
        Paragraph(vat_line, sub_style),
    ]
    right_col = [
        Paragraph(doc_title.upper(), ref_style),
        Paragraph(f"Ref: {doc_ref}", ref_sub_style),
        Paragraph(f"Date: {datetime.now().strftime('%d %B %Y')}", ref_sub_style),
    ]

    header_table = Table(
        [[left_col, right_col]],
        colWidths=[110*mm, 65*mm]
    )
    header_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING",  (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
    ]))
    story.append(header_table)
    story.append(Spacer(1, 4*mm))
    story.append(HRFlowable(width="100%", thickness=2, color=brand))
    story.append(Spacer(1, 6*mm))

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
        # Header row
        ("BACKGROUND",    (0, 0),  (-1, 0),           brand),
        ("TEXTCOLOR",     (0, 0),  (-1, 0),           colors.white),
        ("FONTNAME",      (0, 0),  (-1, 0),           "Helvetica-Bold"),
        ("FONTSIZE",      (0, 0),  (-1, 0),           9),
        ("BOTTOMPADDING", (0, 0),  (-1, 0),           6),
        ("TOPPADDING",    (0, 0),  (-1, 0),           6),
        # Data rows
        ("FONTSIZE",      (0, 1),  (-1, -1),          9),
        ("TOPPADDING",    (0, 1),  (-1, -1),          5),
        ("BOTTOMPADDING", (0, 1),  (-1, -1),          5),
        ("ROWBACKGROUNDS",(0, 1),  (-1, row_count-2), [colors.white, light_grey]),
        # Totals row
        ("BACKGROUND",    (0, -1), (-1, -1),          light_grey),
        ("FONTNAME",      (0, -1), (-1, -1),          "Helvetica-Bold"),
        ("LINEABOVE",     (0, -1), (-1, -1),          1.5, brand),
        # Alignment
        ("ALIGN",         (3, 0),  (-1, -1),          "RIGHT"),
        ("VALIGN",        (0, 0),  (-1, -1),          "MIDDLE"),
        # Grid
        ("GRID",          (0, 0),  (-1, -1),          0.4, mid_grey),
        ("LEFTPADDING",   (0, 0),  (-1, -1),          6),
        ("RIGHTPADDING",  (0, 0),  (-1, -1),          6),
    ]))

    story.append(tbl)
    story.append(Spacer(1, 6*mm))

    # ── Summary box ───────────────────────────────────────────────────────────
    summary_data = [
        ["Total Items:",     str(len(logs))],
        ["Total Hours:",     f"{total_hours:.1f} hrs" if total_hours else "—"],
        ["Total Est. Cost:", f"£{total_cost:.2f}"],
    ]
    sum_tbl = Table(summary_data, colWidths=[50*mm, 40*mm])
    sum_tbl.setStyle(TableStyle([
        ("FONTSIZE",      (0, 0), (-1, -1), 9),
        ("FONTNAME",      (0, 0), (0, -1),  "Helvetica-Bold"),
        ("ALIGN",         (1, 0), (1, -1),  "RIGHT"),
        ("TOPPADDING",    (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LINEBELOW",     (0, -1),(-1, -1), 1.5, brand),
    ]))

    wrapper = Table([[None, sum_tbl]], colWidths=[85*mm, 90*mm])
    wrapper.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story.append(wrapper)
    story.append(Spacer(1, 10*mm))

    # ── Signature section ─────────────────────────────────────────────────────
    sig_data = [
        ["Authorised by:", "", "Client signature:", ""],
        [" ", "", " ", ""],
        ["_______________________", "", "_______________________", ""],
        ["Name / Date", "", "Name / Date", ""],
    ]
    sig_tbl = Table(sig_data, colWidths=[60*mm, 10*mm, 60*mm, 45*mm])
    sig_tbl.setStyle(TableStyle([
        ("FONTSIZE",      (0, 0), (-1, -1), 8),
        ("TEXTCOLOR",     (0, 0), (-1, -1), dark_grey),
        ("TOPPADDING",    (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(sig_tbl)
    story.append(Spacer(1, 8*mm))
    story.append(HRFlowable(width="100%", thickness=0.5, color=mid_grey))
    story.append(Spacer(1, 2*mm))

    # ── Footer ────────────────────────────────────────────────────────────────
    footer_style = ParagraphStyle(
        "footer", fontSize=7, textColor=colors.grey, alignment=TA_CENTER
    )
    story.append(Paragraph(
        f"{doc_ref} · Generated by SubSync on {datetime.now().strftime('%d/%m/%Y at %H:%M')} "
        f"· All variations subject to client approval.",
        footer_style
    ))

    doc.build(story)
    buffer.seek(0)
    return buffer.getvalue()
