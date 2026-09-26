"""產生 index.html 第 7 幕「漲跌回放」的 `const RP` 資料。

用法：
    python tools/gen_rp.py            # 輸出可直接貼進 index.html 的一行：const RP = {...};
    python tools/gen_rp.py --legacy   # 重現舊版資料（沒有「上線不回補」規則、舊事件文字），用來核對邏輯

規則以 D:\\trade_SRF\\tools\\research\\compare_etf.py 的 srf()（--up-grid loss --up-trigger 0.02）為準：
口數 N = floor(權益 × 4.6 ÷ (價格 × 1000))、下限 3；配置 3：4：3（Python round）；上層間距
max((H − MA60) ÷ 4, H × 1.5%)，掉到 MA60 之下的線不用；賣出目標 max(上一條線, 買價 × 1.02)；
下層 MA60 × (1 − 10%/17%/22%)，賣 max(MA60, 買價 × 1.02)；跌破模式（收盤 < MA60 × 0.98 且持有上層）
上層停買、在期間低點到 MA60 分 4 層認賠賣；N 縮小時立即賣多出的底倉；費用 12 元／口＋期交稅 0.002%。

每段都用 25 萬重新啟動：價格換算成 112.4 起跳（今天的 0050 價位），圖上顯示為指數（起點 = 100）。
初始 H = 前 240 個交易日最高還原收盤；起始日只建底倉，不做加碼判斷。
規劃 md「初始化與重啟」第 2 點（2026-09-26 加入，--legacy 不套用）：起始日已在收盤價之上（含等於）
的買線標記待啟用，之後某日收盤高於該線才恢復掛單。

days 每筆欄位：
  0 日期  1 價格指數  2 MA60 指數  3 H 指數  4 底倉口數  5 上層持有  6 下層持有  7 目標口數 N
  8 權益（元）  9 槓桿  10 跌破模式中（0/1）  11 七條買線 [U1..U4, L1..L3]（上層掉到 MA60 之下、下層沒配到口數、或待啟用 = null）
  12 網格已實現損益（元，累計）  13 事件 [[類型, 文字], ...]  14 跌破模式賣出層 4 條（非跌破 = null）
"""
import argparse
import csv
import json
import os
import sys

ROOT = r"D:\trade_SRF"
START_PX = 112.4          # 換算基準：2026-09-24 0050 收盤
EQ0 = 250000.0
K = 4.6
FEE, TAX, MULT = 12.0, 0.00002, 1000
BASE_R, UP_R = 0.3, 0.4
LOW_BIAS = [.10, .17, .22]
TRIG = 0.02
PATCH = {"2026-09-24": 112.40}

EPISODES = [
    ("bull19", "2019 多頭", "2019-01-02", "2019-12-31"),
    ("covid", "2020 疫情急殺", "2020-01-02", "2020-08-31"),
    ("bear22", "2022 升息空頭", "2021-12-01", "2023-01-31"),
    ("v24", "2024-08 V 型急殺", "2024-06-03", "2024-11-29"),
    ("tariff", "2025-04 關稅急跌", "2025-02-03", "2025-08-29"),
    ("bull26", "2026 大多頭", "2025-11-03", "2026-09-24"),
]


def load():
    """同 compare_etf.load("0050")：Close 空值用 PATCH 補（還原價 = 收盤 × 前一日還原比），
    沒有補值的空列跳過；2014-01-02 以前 yfinance 沒套 1 拆 4，要 ÷4。回傳 [(日期, 還原收盤)]。"""
    out = []
    prev_ratio = 1.0
    with open(os.path.join(ROOT, "data", "0050_daily.csv"), encoding="utf-8") as f:
        for r in csv.DictReader(f):
            d = r["Date"][:10]
            c, a = r["Close"], r["Adj Close"]
            if not c:
                if d in PATCH:
                    c = PATCH[d]
                    a = c * prev_ratio
                else:
                    continue
            c, a = float(c), float(a)
            if d < "2014-01-02":
                c, a = c / 4, a / 4
            prev_ratio = a / c
            out.append((d, a))
    return out


def alloc(n):
    b = round(BASE_R * n)
    u = round(UP_R * n)
    return b, u, n - b - u


def spread(total, lines):
    q = [total // lines] * lines
    for j in range(total % lines):
        q[lines - 1 - j] += 1
    return q


def cost(px, lots):
    return lots * (FEE + px * MULT * TAX)


def episode(series, key, title, d_start, d_end, legacy):
    DS = [d for d, _ in series]
    raw = [a for _, a in series]
    i0 = DS.index(d_start)
    i1 = DS.index(d_end)
    sc = START_PX / raw[i0]
    PX = [a * sc for a in raw]
    idx = lambda v: v / START_PX * 100          # noqa: E731
    r2 = lambda v: round(idx(v), 2)             # noqa: E731
    f1 = lambda v: f"{r2(v):.1f}"              # noqa: E731  文字先取到 0.01 再取 0.1（與舊資料一致）
    MA = [None] * len(PX)
    for i in range(59, len(PX)):
        MA[i] = sum(PX[i - 59:i + 1]) / 60

    eq = EQ0
    p0 = PX[i0]
    N = max(3, int(eq * K / (p0 * MULT)))
    base, up_n, low_n = alloc(N)
    eq -= cost(p0, base)
    H = max(PX[max(0, i0 - 239):i0 + 1])
    held_up, held_low = [], []
    ulow = None
    g = 0.0
    buys = sells = bears = 0
    days = []
    dormant_up, dormant_low = set(), set()

    def lines_for(H, m):
        sp = max((H - m) / 4, H * 0.015)
        ok = [k for k in range(1, 5) if H - k * sp > m] or [1]
        return sp, ok

    def line_row(H, m, sp, ok, q_up, q_low):
        """上層：不用的線（掉到 MA60 之下）為 null；下層：沒配到口數為 null；待啟用的線不掛單，也是 null。"""
        ups = [r2(H - k * sp) if k in ok and k not in dormant_up else None for k in range(1, 5)]
        lows = [r2(m * (1 - b)) if q_low[j] > 0 and j not in dormant_low else None for j, b in enumerate(LOW_BIAS)]
        return ups + lows

    # 起始日：只建底倉、定線
    m = MA[i0]
    sp, ok = lines_for(H, m)
    q_up = dict(zip(ok, spread(up_n, len(ok))))
    q_low = spread(low_n, 3)
    if not legacy:
        dormant_up = {k for k in ok if H - k * sp >= PX[i0]}
        dormant_low = {j for j, b in enumerate(LOW_BIAS) if m * (1 - b) >= PX[i0]}
    lots = base
    ev0 = [["start", f"用 25 萬啟動：N = {N} 口，市價建底倉 {base} 口，H = {f1(H)}（前 240 日最高）"]]
    if dormant_up or dormant_low:
        names = [f"U{k}" for k in sorted(dormant_up)] + [f"L{j + 1}" for j in sorted(dormant_low)]
        ev0.append(["start", f"{'、'.join(names)} 買線已在收盤價之上，不回補，等收盤漲回線上才開始掛單"])
    days.append([DS[i0], r2(PX[i0]), r2(m), r2(H), base, 0, 0, N, round(eq),
                 round(lots * PX[i0] * MULT / eq, 2), 0, line_row(H, m, sp, ok, q_up, q_low), 0, ev0, None])

    for i in range(i0 + 1, i1 + 1):
        p, pp, m = PX[i], PX[i - 1], MA[i]
        ev = []
        lots = base + len(held_up) + len(held_low)
        eq += lots * (p - pp) * MULT
        n_new = max(3, int(eq * K / (p * MULT)))
        up_lbl = "權益成長，" if legacy else "口數上限 "
        dn_lbl = "權益縮水，" if legacy else "口數上限 "
        if n_new < N:
            nb, _, _ = alloc(n_new)
            k_sell = base - nb
            eq -= cost(p, k_sell)
            ev.append(["down", f"{dn_lbl}N {N}→{n_new}：" + (f"市價賣出多餘底倉 {k_sell} 口" if k_sell > 0 else "減少加碼額度")])
            N = n_new
            base, up_n, low_n = alloc(N)
        if not held_up and not held_low:
            if p > H:
                H = p
            if n_new > N:
                nb, _, _ = alloc(n_new)
                k_buy = nb - base
                eq -= cost(p, k_buy)
                ev.append(["up", f"{up_lbl}N {N}→{n_new}：" + (f"市價補底倉 {k_buy} 口" if k_buy > 0 else "加碼額度增加")])
                N = n_new
                base, up_n, low_n = alloc(N)
        sp, ok = lines_for(H, m)
        q_up = dict(zip(ok, spread(up_n, len(ok))))
        # 待啟用的線：收盤高於該線才恢復
        for k in list(dormant_up):
            if p > H - k * sp:
                dormant_up.discard(k)
                ev.append(["wake", f"收盤 {r2(p):.2f} 高於 U{k} 買線 {r2(H - k * sp):.2f} → 恢復掛買單"])
        for j in list(dormant_low):
            if p > m * (1 - LOW_BIAS[j]):
                dormant_low.discard(j)
                ev.append(["wake", f"收盤 {r2(p):.2f} 高於 L{j + 1} 買線 {r2(m * (1 - LOW_BIAS[j])):.2f} → 恢復掛買單"])
        still = []
        below = p < m * (1 - TRIG) if ulow is None else p < m
        bear_lv = None
        if below and held_up:
            if ulow is None:
                bears += 1
                ev.append(["bear", f"收盤 {f1(p)} 跌破 MA60×0.98（{f1(m * (1 - TRIG))}）→ 進入跌破模式：上層停買，改掛反彈賣單"])
            ulow = p if ulow is None else min(ulow, p)
            lv = [ulow + q * (m - ulow) / 4 for q in range(1, 5)]
            order = sorted(held_up, key=lambda x: x[1])
            n = len(order)
            sold = []
            for j, (k, bp) in enumerate(order):
                if p >= lv[j * 4 // n]:
                    c = cost(p, 1)
                    eq -= c
                    sold.append((k, bp))
                else:
                    still.append((k, bp))
            for (k, bp), cnt in group(sold):
                pnl = (p - bp) * MULT * cnt
                g += pnl
                sells += cnt
                ev.append(["bs", f"跌破模式反彈賣出 U{k}（{f1(bp)} → {f1(p)}，{pnl / cnt / 1e4:+.2f} 萬）" + (f" ×{cnt}" if cnt > 1 else "")])
        else:
            sold = []
            for k, bp in held_up:
                tgt = max(H - (k - 1) * sp if k > 1 else H, bp * 1.02)
                if p >= tgt:
                    c = cost(p, 1)
                    eq -= c
                    sold.append((k, bp))
                else:
                    still.append((k, bp))
            for (k, bp), cnt in group(sold):
                pnl = (p - bp) * MULT * cnt
                g += pnl
                sells += cnt
                ev.append(["us", f"賣出 U{k}（{f1(bp)} → {f1(p)}，{pnl / cnt / 1e4:+.2f} 萬）" + (f" ×{cnt}" if cnt > 1 else "")])
        if not below and ulow is not None:
            ulow = None
            ev.append(["bearoff", f"收盤站回 MA60（{f1(m)}）→ 解除跌破模式，上層恢復照前高掛買單"])
        if ulow is not None:
            bear_lv = [round(idx(ulow + q * (m - ulow) / 4), 2) for q in range(1, 5)]
        held_up = still
        still = []
        sold = []
        for j, bp in held_low:
            if p >= max(m, bp * 1.02):
                c = cost(p, 1)
                eq -= c
                sold.append((j, bp))
            else:
                still.append((j, bp))
        for (j, bp), cnt in group(sold):
            pnl = (p - bp) * MULT * cnt
            g += pnl
            sells += cnt
            ev.append(["ls", f"賣出 L{j + 1}（{f1(bp)} → {f1(p)}，{pnl / cnt / 1e4:+.2f} 萬）" + (f" ×{cnt}" if cnt > 1 else "")])
        held_low = still

        def can_buy():
            return (base + len(held_up) + len(held_low) + 1) * p * MULT / eq < 8
        blocked = 0
        for k in (ok if ulow is None and not below else []):
            if k in dormant_up:
                continue
            have = sum(1 for kk, _ in held_up if kk == k)
            nb = 0
            while have < q_up.get(k, 0) and p <= H - k * sp:
                if not can_buy():
                    blocked += 1
                    break
                held_up.append((k, p))
                have += 1
                nb += 1
                eq -= cost(p, 1)
            if nb:
                buys += nb
                ev.append(["ub", f"收盤 {f1(p)} ≤ U{k} 買線 {f1(H - k * sp)} → 買進 {nb} 口"])
        q_low = spread(low_n, 3)
        for j, b in enumerate(LOW_BIAS):
            if j in dormant_low:
                continue
            have = sum(1 for jj, _ in held_low if jj == j)
            nb = 0
            while have < q_low[j] and p <= m * (1 - b):
                if not can_buy():
                    blocked += 1
                    break
                held_low.append((j, p))
                have += 1
                nb += 1
                eq -= cost(p, 1)
            if nb:
                buys += nb
                ev.append(["lb", f"乖離 MA60 −{int(b * 100)}% 跌到 L{j + 1} {f1(m * (1 - b))} → 買進 {nb} 口"])
        if blocked:
            ev.append(["halt", "槓桿到 8x：擋掉買單，只賣不買"])
        lots = base + len(held_up) + len(held_low)
        row_lines = line_row(H, m, sp, ok, q_up, q_low)
        days.append([DS[i], r2(p), r2(m), r2(H), base, len(held_up), len(held_low), N, round(eq),
                     round(lots * p * MULT / eq, 2), 1 if ulow is not None else 0, row_lines, round(g), ev, bear_lv])

    px = [d[1] for d in days]
    eqs = [d[8] for d in days]

    def mdd(a):
        pk, w = a[0], 0.0
        for v in a:
            pk = max(pk, v)
            w = max(w, 1 - v / pk)
        return w

    meta = {"p1": round(px[-1] - 100, 1), "pmin": round(min(px) - 100, 1), "eq1": round((eqs[-1] / EQ0 - 1) * 100, 1),
            "mdd": round(mdd(eqs) * 100, 1), "pmdd": round(mdd(px) * 100, 1), "levmax": max(d[9] for d in days),
            "buys": buys, "sells": sells, "bear": bears, "g": round(g)}
    return {"title": title, "meta": meta, "days": days}


def group(sold):
    """同一條線、同一買價的連續賣出合併成一則事件（×n）。"""
    out = []
    for k, bp in sold:
        if out and out[-1][0] == (k, bp):
            out[-1][1] += 1
        else:
            out.append([(k, bp), 1])
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--legacy", action="store_true", help="重現舊版（無上線不回補、舊事件文字）")
    ap.add_argument("--json", action="store_true", help="只輸出 JSON 物件，不加 const RP = ")
    a = ap.parse_args()
    series = load()
    rp = {k: episode(series, k, t, s, e, a.legacy) for k, t, s, e in EPISODES}
    js = json.dumps(rp, ensure_ascii=False, separators=(",", ":"))
    sys.stdout.reconfigure(encoding="utf-8")
    print(js if a.json else f"const RP = {js};")


if __name__ == "__main__":
    main()
