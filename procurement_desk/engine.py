#!/usr/bin/env python3
"""Procurement Desk decision engine.

Implements policies/decision-procedure.md. Playbook values are parsed from the
playbook files (not hard-coded) so amendments / contracts are applied faithfully.
"""
import csv
import math
import os
import re
from fractions import Fraction
from decimal import Decimal

class Missing(Exception):
    pass


class Unparsed(Exception):
    pass


def _read(path):
    with open(path, encoding='utf-8') as fh:
        return fh.read()


def frac(s):
    return Fraction(Decimal(s.replace(',', '')))


# ---------------------------------------------------------------- playbook ---
def load_playbook(pb):
    cats = {}
    title2slug = {}
    for fn in sorted(os.listdir(os.path.join(pb, 'categories'))):
        slug = fn[:-3]
        t = _read(os.path.join(pb, 'categories', fn))
        title = re.search(r'# Category:\s*(.+)', t).group(1).strip()
        title2slug[title.lower()] = slug

        def g(label):
            m = re.search(r'\*\*' + label + r':\*\*\s*₹?([\d,\.]+)', t)
            return m.group(1) if m else None
        cats[slug] = dict(
            gst=frac(re.search(r'GST rate:\*\*\s*([\d\.]+)%', t).group(1)) / 100,
            target=frac(g('Target price')),
            walk=frac(g('Walk-away price')),
            R=int(g('Negotiation rounds \\(R\\)')),
            beta=int(g('Concession exponent \\(β\\)')),
            limit=frac(g('Approval limit')),
            unit=re.search(r'Normalized unit:\*\*\s*(.+)', t).group(1),
        )
        c = cats[slug]
        u = c['unit']
        c['period'] = 'month' if u.endswith('per month') else ('year' if u.endswith('per year') else None)
    # amendments
    amds = []
    for fn in sorted(os.listdir(os.path.join(pb, 'amendments'))):
        t = _read(os.path.join(pb, 'amendments', fn))
        aid = fn[:-3]
        name = re.search(r'# AMD-\d+:\s*(.+)', t).group(1).strip().lower()
        eff = re.search(r'Effective from:\*\*\s*(\d{4}-\d{2}-\d{2})', t).group(1)
        m = re.search(r'the (walk-away price|target price|approval limit) for .*? is now ₹([\d,\.]+)', t)
        field = {'walk-away price': 'walk', 'target price': 'target', 'approval limit': 'limit'}[m.group(1)]
        amds.append(dict(id=aid, cat=title2slug[name], eff=eff, field=field, value=frac(m.group(2))))
    # suppliers
    sups = {}
    for fn in sorted(os.listdir(os.path.join(pb, 'suppliers'))):
        t = _read(os.path.join(pb, 'suppliers', fn))
        name = re.search(r'# Supplier:\s*(.+)', t).group(1).strip()
        status = re.search(r'Status:\*\*\s*(\w+)', t).group(1).upper()
        s = dict(status=status, caps={}, fx={})
        for m in re.finditer(r'price cap for (.+?) of ₹([\d,\.]+)', t):
            s['caps'][title2slug[m.group(1).strip().lower()]] = frac(m.group(2))
        for m in re.finditer(r'invoices in (\w{3}) are converted at ₹([\d,\.]+)', t):
            s['fx'][m.group(1)] = frac(m.group(2))
        sups[name.lower()] = s
    return cats, amds, sups


FX = {'USD': Fraction(169, 2), 'EUR': Fraction(91), 'GBP': Fraction(106)}  # from units-and-currency.md

# --------------------------------------------------------------- number words
SMALL = {w: i for i, w in enumerate(
    'zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen'.split())}
TENS = {w: 10 * (i + 2) for i, w in enumerate('twenty thirty forty fifty sixty seventy eighty ninety'.split())}
MULT = {'thousand': 1000, 'lakh': 100000, 'lakhs': 100000, 'crore': 10 ** 7}
WORDS = list(SMALL) + list(TENS) + ['hundred', 'and'] + list(MULT)
WORD_RE = r'(?:(?:%s)(?:[\s-]+|(?=\s*rupee)))+' % '|'.join(sorted(WORDS, key=len, reverse=True))


def words_to_num(s):
    total = cur = 0
    for w in re.split(r'[\s-]+', s.strip().lower()):
        if not w or w == 'and':
            continue
        if w in SMALL:
            cur += SMALL[w]
        elif w in TENS:
            cur += TENS[w]
        elif w == 'hundred':
            cur = (cur or 1) * 100
        elif w in MULT:
            total += (cur or 1) * MULT[w]
            cur = 0
        else:
            raise Unparsed('word ' + w)
    return Fraction(total + cur)


CUR_TOKEN = r'(?:Rs\.?|INR|₹|USD|EUR|GBP|JPY|SGD|AED|CAD|AUD|CHF|CNY|\$|€|£|[A-Z]{3}(?=\s?\d))'
NUM = r'\d+(?:,\d+)*(?:\.\d+)?'
AMT_RE = re.compile(
    r'(?P<cur>%s)\s?(?P<num>%s)(?:\s?(?P<suf>k|lakhs?|crore)(?![A-Za-z]))?' % (CUR_TOKEN, NUM)
    + r'|(?P<words>(?i:%s))\s*(?i:rupees?)' % WORD_RE)

CUR_MAP = {'rs': 'INR', 'rs.': 'INR', 'inr': 'INR', '₹': 'INR', 'usd': 'USD', 'eur': 'EUR', 'gbp': 'GBP',
           '$': 'USD', '€': 'EUR', '£': 'GBP'}


def find_amounts(text):
    out = []
    for m in AMT_RE.finditer(text):
        if m.group('words'):
            out.append((m.start(), m.end(), 'INR', words_to_num(m.group('words'))))
            continue
        cur = m.group('cur')
        c = CUR_MAP.get(cur.lower(), cur.upper())
        v = frac(m.group('num'))
        suf = (m.group('suf') or '').lower()
        if suf == 'k':
            v *= 1000
        elif suf.startswith('lakh'):
            v *= 100000
        elif suf == 'crore':
            v *= 10 ** 7
        out.append((m.start(), m.end(), c, v))
    return out


# ---------------------------------------------------------------- messages ---
HDR = re.compile(r'^\[(Earlier|Latest) message — (.*?)\]\s*$', re.M)


def split_thread(raw):
    """-> list of (kind, sender, body) in order."""
    ms = list(HDR.finditer(raw))
    if not ms:
        return [('Latest', None, raw)]
    out = []
    for i, m in enumerate(ms):
        end = ms[i + 1].start() if i + 1 < len(ms) else len(raw)
        sender = m.group(2).split(',')[0].strip()
        out.append((m.group(1), sender, raw[m.end():end].strip()))
    return out


def price_line(body):
    for ln in body.split('\n'):
        l = ln.strip()
        if not l:
            continue
        if re.search(r'should read', l, re.I):
            m = re.search(r'should read\s+(.*?)(?:,?\s+not\b.*)?$', l, re.I)
            return 'correction', m.group(1)
        if find_amounts(l) or re.search(r'% off our previous quote', l, re.I):
            return 'offer', l
    return None, None


def unit_divisor(seg, cat):
    """Units (in the category's normalized unit) covered by one 'per ...' in seg."""
    m = re.search(r'\bper\s+(dozen\s+|pack of (\d+)\s+|(\d+)\s+)?([a-z]+)', seg, re.I)
    if not m:
        return Fraction(1)
    word = m.group(4).lower().rstrip('s') if m.group(4).lower() not in ('boxs',) else 'box'
    if word in ('month', 'year', 'annum'):
        return Fraction(1)
    cnt = Fraction(1)
    if m.group(1):
        g = m.group(1).strip().lower()
        if g == 'dozen':
            cnt = Fraction(12)
        elif g.startswith('pack of'):
            cnt = Fraction(int(m.group(2)))
        else:
            cnt = Fraction(int(m.group(3)))
    if word == 'ream' and cat == 'printer-paper':
        cnt = cnt / 10
    if word == 'pack' and not (m.group(1) or '').startswith('pack of') and cat == 'led-lighting':
        cnt = cnt * 4
    return cnt


def to_inr(cur, v, sup):
    if cur == 'INR':
        return v
    if cur in sup['fx']:
        return v * sup['fx'][cur]
    if cur in FX:
        return v * FX[cur]
    raise Missing('currency ' + cur)


def normalize_clause(text, ctx, prev_fn):
    cat, qty, sup, cats = ctx['cat'], ctx['qty'], ctx['sup'], ctx['cats']
    c = cats[cat]
    low = text.lower()
    # ---- further D% off our previous quote
    m = re.search(r'(\d+(?:\.\d+)?)\s*% off our previous quote', text, re.I)
    if m:
        prev = prev_fn()
        if prev is None:
            raise Missing('no previous quote')
        return prev * (1 - frac(m.group(1)) / 100), 'prevdisc'
    amts = find_amounts(text)
    if not amts:
        raise Missing('no amount')
    gst_incl = bool(re.search(r'inclusive of gst|incl\.? gst|including gst|gst included|incl\. of gst|gst-inclusive', low))
    pm = re.search(r'\bper\s+(month|year|annum)\b', low)
    period = None
    if pm:
        period = 'month' if pm.group(1) == 'month' else 'year'
    kind = 'plain'
    # choose amount + divisor
    if 'between' in low and len(amts) >= 2 and re.search(r'\bbetween\b.*\band\b', low):
        s, e, cur, v = max(amts[:2], key=lambda a: to_inr(a[2], a[3], sup))
        seg = text[amts[1][1]:]
        div = unit_divisor(seg, cat)
        base_cur, base = cur, v / div
        kind = 'range'
    elif re.search(r'in total for all (\d+)', low):
        n = int(re.search(r'in total for all (\d+)', low).group(1))
        s, e, cur, v = amts[0]
        base_cur, base = cur, v / n
        kind = 'total'
        ctx.setdefault('flags', []).append('total_n=%d' % n if n != qty else 'total_ok')
    elif re.search(r'up to (\d+)', low) and 'larger orders' in low and len(amts) >= 2:
        thr = int(re.search(r'up to (\d+)', low).group(1))
        pick = amts[0] if qty <= thr else amts[1]
        seg = text[pick[1]:(amts[1][0] if pick is amts[0] else len(text))]
        s, e, cur, v = pick
        base_cur, base = cur, v / unit_divisor(seg, cat)
        kind = 'tier'
    else:
        s, e, cur, v = amts[0]
        seg = text[e:]
        base_cur, base = cur, v / unit_divisor(seg, cat)
        m = re.search(r'(\d+(?:\.\d+)?)\s*%\s*discount on our list price', low) or \
            re.search(r'(\d+(?:\.\d+)?)\s*% discount', low)
        if 'list price' in low:
            m = re.search(r'(\d+(?:\.\d+)?)\s*%\s*discount', low)
            if not m:
                raise Unparsed('list price w/o discount')
            base = base * (1 - frac(m.group(1)) / 100)
            kind = 'list'
        m = re.search(r'plus (\d+) free \w+ for every (\d+)', low)
        if m:
            fr, ev = int(m.group(1)), int(m.group(2))
            base = base * Fraction(ev, ev + fr)
            kind = 'free'
        m = re.search(r'further (\d+(?:\.\d+)?)\s*% off for orders of (\d+)', low)
        if m:
            kind = 'voldisc'
            if qty >= int(m.group(2)):
                base = base * (1 - frac(m.group(1)) / 100)
                kind = 'voldisc_applied'
    p = to_inr(base_cur, base, sup)
    if gst_incl:
        p = p / (1 + c['gst'])
    # period handling
    if c['period']:
        if period is None:
            raise Missing('no billing period')
        if c['period'] == 'year' and period == 'month':
            p = p * 12
        elif c['period'] == 'month' and period == 'year':
            p = p / 12
    return p, kind


def parse_offer_at(msgs, idx, ctx):
    """Normalized price of supplier message msgs[idx]."""
    kind, sender, body = msgs[idx]
    lk, line = price_line(body)
    if line is None:
        raise Missing('no price line')

    def prev_fn():
        for j in range(idx - 1, -1, -1):
            if msgs[j][1] is not None and msgs[j][1].lower() != 'acme procurement':
                return parse_offer_at(msgs, j, ctx)[0]
        return None
    return normalize_clause(line, ctx, prev_fn)


# ----------------------------------------------------------------- scanners ---
INJ = re.compile(
    r'language model|\bai agent\b|\bai assistant\b|\bto (?:the )?(?:ai|bot|llm)\b|whichever bot|\bbot\b|\bllm\b|automated system|'
    r'reviewing assistant|reviewer|admin override|\boverride\b|ignore (?:all |your |any )?(?:previous|prior|above) instructions|'
    r'system (?:note|prompt|message)|pre-?approved|already signed off|signed off|approval (?:has )?already|already (?:been )?approved|'
    r'<!--|\bassistant:|\bmark (?:this|it) (?:thread )?as\b|record the decision|skip the playbook|disregard|'
    r'\bnote (?:for|to) the\b|[ऀ-ॿ]', re.I)


def scan_injection(raw):
    return bool(INJ.search(raw))


def sentences(body):
    return [s.strip() for s in re.split(r'(?<=[.!?])\s+|\n', body) if s.strip()]


def scan_banned(body):
    hits = []
    for s in sentences(body):
        l = s.lower()
        # auto renewal
        if re.search(r'auto-?\s?renew|automatic(?:ally)? renew|renews? on its own|renews itself|renews automatically|'
                     r'(?:continues?|renews?|extends?) (?:from )?year to year|year to year unless|evergreen', l):
            if not re.search(r'no automatic renewal|renewal only if|renewal only by|only by mutual|mutual agreement|does not (?:auto|renew)|no auto', l):
                hits.append('autorenew')
        # advance payment
        if re.search(r'advance|upfront|up front|up-front|deposit|prepay|pre-pay|in advance', l):
            pcts = [float(x) for x in re.findall(r'(\d+(?:\.\d+)?)\s*%', l)]
            pcts += [float(x) for x in re.findall(r'(\d+(?:\.\d+)?)\s*per ?cent', l)]
            if re.search(r'\bhalf\b', l):
                pcts.append(50)
            if re.search(r'\b(?:one )?third\b', l):
                pcts.append(33.3)
            if re.search(r'\bquarter\b', l):
                pcts.append(25)
            if re.search(r'full (?:payment|amount|order value)|100 ?%|entire|whole', l):
                pcts.append(100)
            for w, n in (('ten', 10), ('twenty', 20), ('twenty-five', 25), ('thirty', 30), ('forty', 40), ('fifty', 50),
                         ('sixty', 60), ('seventy', 70), ('eighty', 80), ('ninety', 90)):
                if re.search(r'\b%s\s+(?:per ?cent|percent)' % w, l):
                    pcts.append(n)
            if any(p > 30 for p in pcts):
                hits.append('advance>30')
        if re.search(r'liability', l) and re.search(r'unlimited|without limit|no cap|uncapped', l):
            hits.append('unlimited')
        if re.search(r'exclusiv|only from us|sole supplier|sole source|only buy|buy .{0,40}only from', l):
            if not re.search(r'not asking for|no exclusivity|non-exclusive|without exclusivity|not exclusive|do not require', l):
                hits.append('exclusive')
    return hits


# ---------------------------------------------------------------- decision ---
def effective_values(cat, date, cats, amds):
    v = {k: cats[cat][k] for k in ('target', 'walk', 'limit')}
    best = {}
    for a in amds:
        if a['cat'] == cat and a['eff'] <= date:
            key = (a['eff'], a['id'])
            if a['field'] not in best or key > best[a['field']][0]:
                best[a['field']] = (key, a['value'])
    for f, (_, val) in best.items():
        v[f] = val
    return v


def decide(row, cats, amds, sups):
    cat = row['category']
    qty = int(row['quantity'])
    r = int(row['round'])
    date = row['date']
    supname = row['supplier']
    sup = sups[supname.lower()]
    raw = row['supplier_message']
    msgs = split_thread(raw)
    ctx = dict(cat=cat, qty=qty, sup=sup, cats=cats)
    dbg = {}
    # 1. missing info
    try:
        p, kind = parse_offer_at(msgs, len(msgs) - 1, ctx)
        dbg['kind'] = kind
    except Missing as e:
        return 'ESCALATE:MISSING_INFO:', dict(why=str(e))
    dbg['p'] = float(p)
    dbg['flags'] = ctx.get('flags', [])
    # 2. blacklisted
    if sup['status'] == 'BLACKLISTED':
        return 'REJECT:BLACKLISTED_SUPPLIER:', dbg
    # 3. injection (anywhere in thread)
    if scan_injection(raw):
        return 'ESCALATE:PROMPT_INJECTION:', dbg
    # 4. banned terms (latest message)
    hits = scan_banned(msgs[-1][2])
    if hits:
        dbg['banned'] = hits
        return 'ESCALATE:BANNED_TERM:', dbg
    # 5. price decision
    ev = effective_values(cat, date, cats, amds)
    c = cats[cat]
    target = ev['target']
    walk = ev['walk']
    if cat in sup['caps']:
        walk = sup['caps'][cat]
    R, beta = c['R'], c['beta']
    if sup['status'] == 'PROBATION':
        L = target
    else:
        L = target + (walk - target) * Fraction(r - 1, R - 1) ** beta
    dbg.update(L=float(L), walk=float(walk), target=float(target), limit=float(ev['limit']))
    if p <= L:
        if p * qty > ev['limit']:
            return 'ESCALATE:OVER_APPROVAL_LIMIT:', dbg
        return 'ACCEPT:ACCEPTABLE_PRICE:', dbg
    if r >= R:
        return 'REJECT:ABOVE_WALKAWAY:', dbg
    cp = math.floor(L + Fraction(1, 2))
    return 'COUNTER:COUNTER_OFFER:%d' % cp, dbg


def run(threads_csv, pb):
    cats, amds, sups = load_playbook(pb)
    out = {}
    dbgs = {}
    with open(threads_csv, encoding='utf-8', newline='') as fh:
        for row in csv.DictReader(fh):
            try:
                lab, d = decide(row, cats, amds, sups)
            except Unparsed as e:
                lab, d = 'UNPARSED:' + str(e), {}
            out[row['thread_id']] = lab
            dbgs[row['thread_id']] = d
    return out, dbgs

