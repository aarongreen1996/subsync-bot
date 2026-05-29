# pdf_generator.py — Pennyfarthing Perfect Delivery PDF generator
import io
import os
from datetime import datetime
from typing import Optional

try:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
        HRFlowable, Image as RLImage, KeepTogether
    )
    from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT
    REPORTLAB_AVAILABLE = True
except ImportError:
    REPORTLAB_AVAILABLE = False


def _hex_to_color(hex_str: str):
    try:
        hex_str = (hex_str or "#1a1a2e").lstrip('#')
        if len(hex_str) == 3:
            hex_str = ''.join(c*2 for c in hex_str)
        r, g, b = int(hex_str[0:2], 16), int(hex_str[2:4], 16), int(hex_str[4:6], 16)
        return colors.Color(r/255, g/255, b/255)
    except Exception:
        return colors.Color(0.1, 0.1, 0.18)


def _fetch_image_bytes(url: str) -> Optional[bytes]:
    if not url:
        return None
    try:
        import requests as req
        r = req.get(url, timeout=10)
        if r.status_code == 200:
            return r.content
    except Exception as e:
        print(f"Image fetch error {url}: {e}")
    return None


def generate_submission_pdf(
    submission: dict,
    plot: dict,
    site: dict,
    stage_items: list,
    photos_by_item: dict,
    tenant: dict = None,
) -> bytes:
    if not REPORTLAB_AVAILABLE:
        raise RuntimeError("ReportLab not installed. Add 'reportlab' to requirements.txt")

    tenant = tenant or {}
    company_name    = tenant.get("name") or "Pennyfarthing Homes"
    company_address = tenant.get("address") or ""
    company_phone   = tenant.get("phone") or ""
    logo_url        = tenant.get("logo_url") or ""
    primary_hex     = tenant.get("primary_color") or "#1a1a2e"
    secondary_hex   = tenant.get("secondary_color") or "#C5962A"

    PRIMARY    = _hex_to_color(primary_hex)
    SECONDARY  = _hex_to_color(secondary_hex)
    WHITE      = colors.white
    LIGHT_GREY = colors.Color(0.95, 0.95, 0.95)
    MID_GREY   = colors.Color(0.75, 0.75, 0.75)
    YES_GREEN  = colors.Color(0.086, 0.639, 0.29)
    NO_RED     = colors.Color(0.863, 0.149, 0.149)
    NA_GREY    = colors.Color(0.5, 0.5, 0.5)
    AMBER      = colors.Color(0.855, 0.647, 0.125)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=15*mm, rightMargin=15*mm,
        topMargin=15*mm, bottomMargin=15*mm,
    )
    W = A4[0] - 30*mm

    def s(name, **kw):
        base = getSampleStyleSheet()['Normal']
        return ParagraphStyle(name, parent=base, **kw)

    s_body  = s('body',  fontSize=9,  leading=13, textColor=colors.Color(0.15,0.15,0.15))
    s_small = s('small', fontSize=7.5,leading=11, textColor=colors.Color(0.4,0.4,0.4))
    s_bold  = s('bold',  fontSize=9,  leading=13, textColor=colors.Color(0.1,0.1,0.1), fontName='Helvetica-Bold')
    s_head  = s('head',  fontSize=11, leading=15, textColor=PRIMARY, fontName='Helvetica-Bold')

    story = []

    # ── Header ───────────────────────────────────────────────────────────────
    logo_cell = None
    if logo_url:
        img_bytes = _fetch_image_bytes(logo_url)
        if img_bytes:
            try:
                logo_cell = RLImage(io.BytesIO(img_bytes), width=35*mm, height=18*mm, kind='bound')
            except Exception:
                logo_cell = None

    if logo_cell is None:
        logo_cell = Paragraph(
            company_name,
            s('ln', fontSize=13, fontName='Helvetica-Bold', textColor=WHITE)
        )

    title_cell = Paragraph(
        'PERFECT DELIVERY<br/>STAGE SIGN-OFF',
        s('dt', fontSize=13, fontName='Helvetica-Bold', textColor=SECONDARY,
          alignment=TA_RIGHT, leading=18)
    )

    hdr = Table([[logo_cell, title_cell]], colWidths=[W*0.5, W*0.5])
    hdr.setStyle(TableStyle([
        ('BACKGROUND',   (0,0),(-1,-1), PRIMARY),
        ('VALIGN',       (0,0),(-1,-1), 'MIDDLE'),
        ('LEFTPADDING',  (0,0),(0,0),   10),
        ('RIGHTPADDING', (1,0),(1,0),   10),
        ('TOPPADDING',   (0,0),(-1,-1), 10),
        ('BOTTOMPADDING',(0,0),(-1,-1), 10),
    ]))
    story.append(hdr)
    story.append(Spacer(1, 4*mm))

    # ── Meta grid ────────────────────────────────────────────────────────────
    sub_date = str(submission.get("submitted_at",""))[:16].replace("T"," ")
    rev_date = str(submission.get("reviewed_at",""))[:16].replace("T"," ") if submission.get("reviewed_at") else "—"
    status   = (submission.get("status") or "pending").upper()

    meta = [
        [Paragraph('<b>Site</b>', s_bold),     Paragraph(site.get("name",""), s_body),
         Paragraph('<b>Plot</b>', s_bold),      Paragraph(plot.get("plot_number",""), s_body)],
        [Paragraph('<b>Stage</b>', s_bold),     Paragraph(submission.get("stage_name",""), s_body),
         Paragraph('<b>Submitted</b>', s_bold), Paragraph(sub_date, s_body)],
        [Paragraph('<b>Subcontractor</b>', s_bold), Paragraph(submission.get("submitted_by_name",""), s_body),
         Paragraph('<b>Company</b>', s_bold),   Paragraph(submission.get("submitted_by_company",""), s_body)],
        [Paragraph('<b>Reviewed by</b>', s_bold), Paragraph(submission.get("reviewed_by","—"), s_body),
         Paragraph('<b>Review date</b>', s_bold), Paragraph(rev_date, s_body)],
    ]
    mt = Table(meta, colWidths=[W*0.18, W*0.32, W*0.18, W*0.32])
    mt.setStyle(TableStyle([
        ('BACKGROUND',    (0,0),(-1,-1), LIGHT_GREY),
        ('GRID',          (0,0),(-1,-1), 0.5, MID_GREY),
        ('LEFTPADDING',   (0,0),(-1,-1), 6),
        ('RIGHTPADDING',  (0,0),(-1,-1), 6),
        ('TOPPADDING',    (0,0),(-1,-1), 4),
        ('BOTTOMPADDING', (0,0),(-1,-1), 4),
        ('VALIGN',        (0,0),(-1,-1), 'TOP'),
    ]))
    story.append(mt)
    story.append(Spacer(1, 3*mm))

    # ── Status banner ─────────────────────────────────────────────────────────
    if status == "APPROVED":
        sc = YES_GREEN
        st = "APPROVED — Payment can be progressed for this stage"
    elif status == "REJECTED":
        sc = NO_RED
        st = "REJECTED — See site manager notes below"
    else:
        sc = AMBER
        st = "PENDING — Awaiting site manager review"

    sb = Table(
        [[Paragraph(st, s('st', fontSize=10, fontName='Helvetica-Bold', textColor=WHITE, alignment=TA_CENTER))]],
        colWidths=[W],
    )
    sb.setStyle(TableStyle([
        ('BACKGROUND',    (0,0),(-1,-1), sc),
        ('TOPPADDING',    (0,0),(-1,-1), 7),
        ('BOTTOMPADDING', (0,0),(-1,-1), 7),
    ]))
    story.append(sb)

    # Manager notes
    if submission.get("manager_notes"):
        story.append(Spacer(1, 3*mm))
        nt = Table(
            [[Paragraph('<b>Site Manager Notes:</b>', s_bold),
              Paragraph(str(submission["manager_notes"]), s_body)]],
            colWidths=[W*0.22, W*0.78],
        )
        nt.setStyle(TableStyle([
            ('BACKGROUND',    (0,0),(-1,-1), colors.Color(1, 0.98, 0.88)),
            ('GRID',          (0,0),(-1,-1), 0.5, colors.Color(0.9,0.8,0.4)),
            ('LEFTPADDING',   (0,0),(-1,-1), 6),
            ('RIGHTPADDING',  (0,0),(-1,-1), 6),
            ('TOPPADDING',    (0,0),(-1,-1), 5),
            ('BOTTOMPADDING', (0,0),(-1,-1), 5),
            ('VALIGN',        (0,0),(-1,-1), 'TOP'),
        ]))
        story.append(nt)

    if submission.get("additional_notes"):
        story.append(Spacer(1, 2*mm))
        ant = Table(
            [[Paragraph('<b>Subcontractor Notes:</b>', s_bold),
              Paragraph(str(submission["additional_notes"]), s_body)]],
            colWidths=[W*0.22, W*0.78],
        )
        ant.setStyle(TableStyle([
            ('BACKGROUND',    (0,0),(-1,-1), colors.Color(0.94,0.97,1.0)),
            ('GRID',          (0,0),(-1,-1), 0.5, MID_GREY),
            ('LEFTPADDING',   (0,0),(-1,-1), 6),
            ('RIGHTPADDING',  (0,0),(-1,-1), 6),
            ('TOPPADDING',    (0,0),(-1,-1), 5),
            ('BOTTOMPADDING', (0,0),(-1,-1), 5),
            ('VALIGN',        (0,0),(-1,-1), 'TOP'),
        ]))
        story.append(ant)

    # ── Checklist ─────────────────────────────────────────────────────────────
    story.append(Spacer(1, 5*mm))
    story.append(Paragraph("Checklist Answers", s_head))
    story.append(Spacer(1, 2*mm))

    answers = submission.get("answers") or {}

    # Stats
    yes_c = sum(1 for a in answers.values() if isinstance(a,dict) and a.get("value")=="yes")
    no_c  = sum(1 for a in answers.values() if isinstance(a,dict) and a.get("value")=="no")
    na_c  = sum(1 for a in answers.values() if isinstance(a,dict) and a.get("value")=="n/a")
    un_c  = len(stage_items) - yes_c - no_c - na_c

    stat_row = Table([[
        Paragraph(f'<b>{yes_c}</b><br/>Yes',     s('sy', fontSize=9, alignment=TA_CENTER, textColor=YES_GREEN)),
        Paragraph(f'<b>{no_c}</b><br/>No',       s('sn', fontSize=9, alignment=TA_CENTER, textColor=NO_RED)),
        Paragraph(f'<b>{na_c}</b><br/>N/A',      s('sa', fontSize=9, alignment=TA_CENTER, textColor=NA_GREY)),
        Paragraph(f'<b>{un_c}</b><br/>Skipped',  s('su', fontSize=9, alignment=TA_CENTER, textColor=AMBER)),
        Paragraph(f'<b>{len(stage_items)}</b><br/>Total', s('st2', fontSize=9, alignment=TA_CENTER, textColor=PRIMARY)),
    ]], colWidths=[W/5]*5)
    stat_row.setStyle(TableStyle([
        ('BACKGROUND',    (0,0),(-1,-1), LIGHT_GREY),
        ('GRID',          (0,0),(-1,-1), 0.5, MID_GREY),
        ('TOPPADDING',    (0,0),(-1,-1), 5),
        ('BOTTOMPADDING', (0,0),(-1,-1), 5),
    ]))
    story.append(stat_row)
    story.append(Spacer(1, 3*mm))

    # Items
    for idx, item in enumerate(stage_items, 1):
        item_id  = item.get("id","")
        item_txt = item.get("text","")
        ad       = answers.get(item_id) or {}
        val      = ad.get("value","") if isinstance(ad,dict) else ""
        conf     = ad.get("text","")  if isinstance(ad,dict) else ""

        if val=="yes":   bg,fg,bt = YES_GREEN, WHITE, "YES"
        elif val=="no":  bg,fg,bt = NO_RED,   WHITE, "NO"
        elif val=="n/a": bg,fg,bt = NA_GREY,  WHITE, "N/A"
        else:            bg,fg,bt = AMBER,    WHITE, "SKIPPED"

        row = Table(
            [[Paragraph(f'<b>{idx:02d}.</b> {item_txt}', s_body),
              Paragraph(bt, s(f'b{idx}', fontSize=8, fontName='Helvetica-Bold',
                              textColor=fg, alignment=TA_CENTER))]],
            colWidths=[W*0.82, W*0.18],
        )
        row_bg = LIGHT_GREY if idx % 2 == 0 else WHITE
        row.setStyle(TableStyle([
            ('BACKGROUND',    (0,0),(0,0), row_bg),
            ('BACKGROUND',    (1,0),(1,0), bg),
            ('GRID',          (0,0),(-1,-1), 0.3, MID_GREY),
            ('LEFTPADDING',   (0,0),(-1,-1), 5),
            ('RIGHTPADDING',  (0,0),(-1,-1), 5),
            ('TOPPADDING',    (0,0),(-1,-1), 4),
            ('BOTTOMPADDING', (0,0),(-1,-1), 4),
            ('VALIGN',        (0,0),(-1,-1), 'TOP'),
        ]))

        els = [row]

        if conf:
            ct = Table(
                [[Paragraph(f'<i>Confirmed: {conf}</i>', s_small)]],
                colWidths=[W],
            )
            ct.setStyle(TableStyle([
                ('BACKGROUND',    (0,0),(-1,-1), colors.Color(0.94,0.97,1.0)),
                ('LEFTPADDING',   (0,0),(-1,-1), 5),
                ('TOPPADDING',    (0,0),(-1,-1), 2),
                ('BOTTOMPADDING', (0,0),(-1,-1), 2),
            ]))
            els.append(ct)

        # Photos
        item_photos = (photos_by_item or {}).get(item_id, [])
        if item_photos:
            imgs = []
            for url in item_photos[:4]:
                ib = _fetch_image_bytes(url)
                if ib:
                    try:
                        imgs.append(RLImage(io.BytesIO(ib), width=38*mm, height=30*mm, kind='bound'))
                    except Exception:
                        pass
            if imgs:
                while len(imgs) < 4:
                    imgs.append('')
                pt = Table([imgs], colWidths=[W/4]*4)
                pt.setStyle(TableStyle([
                    ('LEFTPADDING',   (0,0),(-1,-1), 2),
                    ('RIGHTPADDING',  (0,0),(-1,-1), 2),
                    ('TOPPADDING',    (0,0),(-1,-1), 3),
                    ('BOTTOMPADDING', (0,0),(-1,-1), 3),
                    ('ALIGN',         (0,0),(-1,-1), 'CENTER'),
                    ('VALIGN',        (0,0),(-1,-1), 'MIDDLE'),
                    ('BACKGROUND',    (0,0),(-1,-1), LIGHT_GREY),
                ]))
                els.append(pt)

        story.append(KeepTogether(els))
        story.append(Spacer(1, 1*mm))

    # ── Footer ────────────────────────────────────────────────────────────────
    story.append(Spacer(1, 6*mm))
    story.append(HRFlowable(width=W, thickness=1, color=SECONDARY))
    story.append(Spacer(1, 2*mm))

    ft = company_name
    if company_address:
        ft += f' · {company_address}'
    if company_phone:
        ft += f' · {company_phone}'
    ft += f' · Generated {datetime.now().strftime("%d %b %Y %H:%M")}'

    story.append(Paragraph(ft, s('ft', fontSize=7, textColor=MID_GREY, alignment=TA_CENTER)))

    doc.build(story)
    return buf.getvalue()
