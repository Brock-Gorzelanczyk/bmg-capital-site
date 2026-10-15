"""Observations from explicitly tabulated financial-statement notes.

Additive evidence only: never replace a primary metric or its period selection.
Revenue mixes must reconcile for every column, and comparisons stay in one
row with distinct, comparable periods. No narrative estimates or screens.
"""
from __future__ import annotations

import re
from collections import defaultdict

from .facts import SCALE, fmt_value

NOTE = re.compile(r"^note\s+\d+\b", re.I)
REVENUE_TOTAL = re.compile(r"^total (?:net )?(?:sales|revenues?)$")
# Note totals that stand on their own in a report; other "Total ..." lines need their table to be read.
NOTE_TOTAL = re.compile(r"^total (?:(?:operating |finance )?lease costs?|property and equipment net|(?:non operating|other) income(?: expense)?(?: net)?|cash taxes paid(?: net of refunds)?|(?:long term )?debt(?: principal(?: amount)?| face value)?|face value of (?:long term )?debt)$")


def observations(fb, make_signal):
    groups = defaultdict(list)
    for r in fb.rows:
        if (NOTE.match(r['table_title']) and r['statement'] == 'other'
                and not r.get('other_registrant') and r.get('scope') not in ('affiliate', 'subsidiary', 'supplemental')
                and r.get('pdf_page') and r.get('scale') in SCALE):
            groups[r['table_id']].append(r)
    out, seen = [], set()
    declarations = []
    for m in re.finditer(r'notes to (?:consolidated )?financial statements[^\n]{0,80}\n\s*(\([^)]{0,160}(?:millions|thousands|billions)[^)]*\))', fb.doc.body, re.I):
        declarations.append((m.start(), m[1], fb.doc.location(m.start())))

    def prepared(r, unit=None):
        row = dict(r)
        note_start = fb.doc.body.rfind(r['table_title'], 0, r['offset'])
        local = fb.doc.body[note_start:r['offset']] if note_start >= 0 else ''
        # The parser's nearby caption may precede this note; never inherit it silently.
        if not r.get('caption') or r['caption'] not in local:
            row['scale'] = ''
        caption = ' '.join([r.get('caption', '') if row['scale'] else '', r.get('intro', '')])
        caption = re.sub(r'\bshar\s+es\b', 'shares', caption, flags=re.I)
        if unit == 'shares':
            row['unit'] = unit
            m = re.search(r"shares in (thousands|millions|000['’]?s)", caption, re.I)
            if m:
                row['scale'] = 'M' if m[1].lower() == 'millions' else 'K'
            elif not row['scale']:
                return None
        elif not row['scale']:
            dec = next((x for x in reversed(declarations) if x[0] < r['offset']), None)
            if not dec:
                return None
            m = re.search(r'amounts in (thousands|millions|billions)', dec[1], re.I)
            if not m:
                return None
            row['scale'] = {'thousands': 'K', 'millions': 'M', 'billions': 'B'}[m[1].lower()]
            row['unit_declaration'] = {**dec[2], 'excerpt': dec[1]}
            row['caption'] = dec[1]
        return row

    def evidence(r, i, unit=None):
        row = prepared(r, unit)
        f = fb._fact_from('note_table', row, i, 'flow')
        f['id'] = f"note:{r['table_id']}:{r['row_index']}:{i}"
        f['name'] = r['full_label']
        f['metric'] = 'note_table'
        f['scope'] = r['table_title'] + (': ' + r['heading'] if r.get('heading') else '')
        f['method'] = 'Financial-statement note table cell'
        if row.get('unit_declaration'):
            f['source']['unit_declaration'] = row['unit_declaration']
        fb.facts[f['id']] = f
        return f

    def emit(r, category, unit=None, absolute=False, total=None, label=None, ident=None):
        if prepared(r, unit) is None:
            return
        cols = r['cols']
        if not cols or len(cols) != len(r['values']):
            return
        # Duplicate years can be segment columns, not time series; do not guess.
        keys = [(c.get('type') or '', c.get('end') or c.get('year')) for c in cols]
        if any(not k[1] for k in keys) or len(set(keys)) != len(keys):
            return
        order = sorted(range(len(cols)), key=lambda i: str(keys[i][1]), reverse=True)
        cur_i = order[0]
        # All note flow comparisons must be like-length and consecutive fiscal years.
        prev_i = next((i for i in order[1:] if keys[i][0] == keys[cur_i][0]
                       and cols[i].get('year') and cols[cur_i].get('year')
                       and int(cols[cur_i]['year']) - int(cols[i]['year']) == 1), None)
        label = label or re.sub(r'\s*\(\d{1,2}\)$', '', r['label'])   # drop a trailing footnote marker
        identity = (category, ident or r['key'])
        if identity in seen:
            return
        cur = evidence(r, cur_i, unit)
        prior = evidence(r, prev_i, unit) if prev_i is not None else None
        value = abs(cur['value']) if absolute else cur['value']
        old = (abs(prior['value']) if absolute else prior['value']) if prior else None
        actual_unit = unit or r['unit']
        display = fmt_value(value, actual_unit)
        headline = f"{label}: {display} ({cols[cur_i]['label']})"
        text = headline + '.'
        inputs = [cur]
        if prior:
            inputs.append(prior)
            text += f" Prior ({cols[prev_i]['label']}): {fmt_value(old, actual_unit)}."
            if old > 0 and value >= 0 and old >= value * 0.05:   # growth off a near-zero base says nothing
                growth = (value / old - 1) * 100
                headline += f", {growth:+.1f}%"
                text += f" Change: {growth:+.1f}%."
        formula = 'Current and prior table cells; change = (current / prior - 1) x 100 when prior > 0'
        if total:
            tf = evidence(total, cur_i)
            if tf['value'] > 0:
                inputs.append(tf)
                text += f" {value / tf['value'] * 100:.1f}% of the table's {total['label']} ({tf['display']})."
                formula += '; share = component / table total x 100'
        pages = list(dict.fromkeys(str(f['source']['printed_page']) if f['source'].get('printed_page') else f"{fb.doc.kind.upper()} page {f['source']['pdf_page']}" for f in inputs))
        text += ' Filing page ' + ', '.join(pages) + '.'
        unit_pages = list(dict.fromkeys(str(f['source']['unit_declaration'].get('printed_page') or
                                          f['source']['unit_declaration']['pdf_page'])
                                       for f in inputs if f['source'].get('unit_declaration')))
        if unit_pages:
            text += ' Units declared on filing page ' + ', '.join(unit_pages) + '.'
        sid = f"note_{category}:" + re.sub(r'[^a-z0-9]+', '_', (ident or r['key']).lower())
        sig = make_signal(sid, f"{label} ({category.replace('_', ' ')})", value, display, 'observation',
                          headline, text, 'Observation, not screened. Arithmetic on cited note table cells only.',
                          inputs, 'notes', unit=actual_unit,
                          definition={'numerator': label, 'denominator': total['label'] if total else '',
                                      'period': cur['period']['label'], 'scope': cur['scope'], 'formula': formula,
                                      'missing': 'Growth omitted without a positive prior value in a comparable consecutive period, or when the prior is under 5% of the current value.',
                                      'applicability': 'Only explicitly tabulated financial-statement notes.'})
        out.append(sig)
        seen.add(identity)

    def context_of(r):
        return ' '.join([r['table_title'], r.get('intro', ''), r.get('heading', '')])

    def reconciles(parts, total):
        return all(abs(sum(p['values'][i] for p in parts) - v) <= max(1e-6, abs(v) * 1e-6) for i, v in enumerate(total['values']))

    def segment_rules(rows):
        if not rows or not re.search(r'segment', ' '.join(context_of(x) for x in rows[:3]), re.I):
            return
        # Segment results: each segment's net sales and operating income, only when the
        # segments add up to the table's Consolidated row in every column.
        metric = lambda k: 'net sales' if re.fullmatch(r'(?:total )?(?:net sales|revenues?)', k) else \
            'operating income' if re.fullmatch(r'operating (?:income|loss)(?: \(?loss\)?| loss)?', k) else None
        by = defaultdict(list)
        for x in rows:
            m = metric(x['key'])
            if m and x['unit'] == 'USD' and x.get('heading'):
                by[m].append(x)
        for m, xs in by.items():
            cons = next((x for x in xs if x['heading'].strip().lower() == 'consolidated'), None)
            parts = [x for x in xs if x is not cons and x['cols'] == (cons or x)['cols']]
            if not cons or not 2 <= len(parts) <= 12 or len({p['heading'] for p in parts}) != len(parts) or not reconciles(parts, cons):
                continue
            for p in parts:
                emit(p, 'segment_results', total=cons, label=f"{p['heading']} {m}", ident=f"{p['heading']} {m}")
        # Segment breakdowns of one measure (assets, property, additions, depreciation):
        # rows that add up to a Consolidated/Total row in every column.
        for pos, total in enumerate(rows):
            if total['unit'] != 'USD' or total['key'] not in ('consolidated', 'total') or re.search(r'net sales|revenue', context_of(total), re.I):
                continue
            parts = []
            for x in reversed(rows[:pos]):
                if x['cols'] != total['cols'] or x['scale'] != total['scale'] or x['unit'] != 'USD' or x['key'].startswith('total') or x.get('heading') != total.get('heading'):
                    break
                parts.insert(0, x)
            if not 2 <= len(parts) <= 12 or not reconciles(parts, total):
                continue
            intro = re.sub(r'^(?:the )?following table (?:presents|shows|sets forth|summarizes)\s*', '', total.get('intro', ''), flags=re.I)
            measure = re.split(r'\s*(?:,?\s*by (?:segment|geographic area|region|country)|reconciled to|(?:are|is) as follows|\(in )', intro, maxsplit=1, flags=re.I)[0].strip(' :,.')
            if not measure or len(measure) > 70:
                continue
            measure = measure[0].lower() + measure[1:]
            for p in parts:
                emit(p, 'segment_breakdown', total=total, label=f"{p['label']}: {measure}", ident=f"{measure} {p['key']}")

    for rows in groups.values():
        for pos, total in enumerate(rows):
            if total['unit'] != 'USD' or not (REVENUE_TOTAL.match(total['key']) or (
                    total['key'] in ('consolidated', 'total') and re.search(r'net sales|revenue', context_of(total), re.I))):
                continue
            parts = []
            for r in reversed(rows[:pos]):
                if (r['cols'] != total['cols'] or r['scale'] != total['scale'] or r['unit'] != 'USD'
                        or r['key'].startswith('total ') or re.search(r'cost|expense|income|profit|assets|liabilities', r['key'])):
                    break
                parts.insert(0, r)
            if not 2 <= len(parts) <= 20:
                continue
            if not all(abs(sum(r['values'][i] for r in parts) - v) <= max(1e-6, abs(v) * 1e-6)
                       for i, v in enumerate(total['values'])):
                continue
            from .profiles import _GEO
            geographic = bool(re.search(r'geograph|countr|region', total['table_title'] + ' ' + total['intro'], re.I)) or sum(bool(_GEO.search(r['label'])) for r in parts) >= len(parts) / 2
            category = 'geography_revenue' if geographic else 'product_segment_revenue'
            for r in parts:
                if all(v >= 0 for v in r['values']):
                    emit(r, category, total=total)
        for r in rows:
            context = ' '.join([r['table_title'], r['block'], r['intro'], r['heading']])
            share_count = bool(re.search(r'changes in shares|shares of common stock|number of shares', context, re.I))
            if share_count and re.search(r'^(?:common stock|(?:common )?shares?) (?:outstanding.*(?:ending|end)|repurchased|issued)', r['key']):
                emit(r, 'shares', unit='shares', absolute='repurchased' in r['key'])
            elif r['unit'] == 'shares' and re.search(r'weighted average (?:basic|diluted) shares', r['key']):
                emit(r, 'weighted_average_shares', unit='shares')
            elif r['unit'] == 'USD' and not re.search(r'income tax|deferred tax', r['table_title'] + ' ' + r['heading'], re.I) and re.fullmatch(r'(?:total )?(?:share|stock) based compensation expense', r['key']):
                emit(r, 'stock_compensation')
            elif r['unit'] == 'USD' and NOTE_TOTAL.match(r['key']):
                # a cited "Total ..." line of a note table (lease cost, property and equipment, ...)
                emit(r, 'note_total')
        segment_rules(rows)
    return out
