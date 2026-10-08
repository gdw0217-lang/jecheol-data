"""KAMIS 소매가격을 받아 앱이 읽는 market.json 을 만든다.

    python scripts/fetch_market.py [--out 파일 ...]      (기본: docs/market.json)

- GitHub Actions 가 하루 세 번 돌려 docs/market.json 을 갱신하고, 앱은 그 파일을 받아 쓴다.
- 인증키는 환경변수(또는 이 PC의 .secrets)에서만 읽는다. 결과 JSON 에는 가격만 들어간다.
- 가격은 품목별 가장 최근 조사일의 전국 시장 중간값, '지난주'는 7일 전(없으면 그 앞 조사일),
  '작년'은 1년 전 앞뒤 10일의 중간값이다. 그래프는 최근 8주의 주별 중간값이다.
- 단위(예: 1포기, 10개, 100g)는 최근 열흘 동안 가장 많이 조사된 단위로 고정한다.
- 결과가 비정상이면(품목이 너무 적거나 배추 가격이 없으면) 파일을 바꾸지 않고 실패로 끝낸다.
"""
import argparse
import datetime as dt
import functools
import json
import statistics
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from catalog import ALIASES, CATS, ITEMS, KIMJANG, KIMJANG_SOURCE, SURVEY  # noqa: E402
from kamis import fetch  # noqa: E402

OUT = HERE.parent / "docs" / "market.json"
CODES = HERE / "codes.xlsx"   # 공공데이터포털 '(참고)일별 도,소매 가격정보_코드.xlsx'
WEEKS = 8
KST = dt.timezone(dt.timedelta(hours=9))
MIN_ITEMS = 60


@functools.cache
def item_codes():
    return pd.read_excel(CODES, sheet_name="품목코드", dtype=str)


def resolve(c):
    """품목명 → (부류코드, 품목코드)"""
    t = item_codes()
    m = t[t["품목명"] == c["name"]]
    if c.get("item"):
        m = m[m["품목코드"] == c["item"]]
    if len(m) != 1:
        raise SystemExit(f"품목 코드를 하나로 정할 수 없어요: {c['label']} {m.values.tolist()}")
    return m.iloc[0]["부류코드"], m.iloc[0]["품목코드"]


def monday(d: dt.date) -> dt.date:
    return d - dt.timedelta(days=d.weekday())


def ymd(s: str) -> dt.date:
    return dt.datetime.strptime(s, "%Y%m%d").date()


def clean(rows, c):
    """품종을 거르고, 등급을 정했으면 그 등급만, 아니면 상품 등급이 있을 때 상품만 쓴다."""
    vr = c.get("vrty")
    rows = [r for r in rows if r.get("exmn_dd_prc") not in (None, "", "0", 0)]
    if vr:
        rows = [r for r in rows if str(r.get("vrty_cd")) in vr]
    if c.get("grd"):
        return [r for r in rows if r.get("grd_nm") in c["grd"]]
    top = [r for r in rows if r.get("grd_nm") == "상품"]
    return top or rows


def num(v):
    try:
        return float(str(v).replace(",", ""))
    except ValueError:
        return None


def unit_key(r):
    return (str(r.get("unit") or "").strip(), str(r.get("unit_sz") or "").strip())


def unit_label(u):
    name, size = u
    try:
        n = float(size)
        size = str(int(n)) if n == int(n) else str(n)
    except ValueError:
        pass
    return f"{size}{name}" if size not in ("", "1") or name in ("g",) else f"1{name}"


def daily(rows, value):
    by = defaultdict(list)
    for r in rows:
        v = value(r)
        if v:
            by[r["exmn_ymd"]].append(v)
    return {ymd(d): statistics.median(vs) for d, vs in by.items()}


def weekly(days: dict):
    by = defaultdict(list)
    for d, v in days.items():
        by[monday(d)].append(v)
    return {w: statistics.median(vs) for w, vs in sorted(by.items())}


def at_or_before(days: dict, d: dt.date, back: int = 6):
    """d 날짜 값, 없으면(주말·공휴일) 그 앞 조사일 값. back 일까지만 거슬러 간다."""
    for i in range(back + 1):
        v = days.get(d - dt.timedelta(days=i))
        if v is not None:
            return v
    return None


def window_median(days: dict, start: dt.date, end: dt.date):
    vs = [v for d, v in days.items() if start <= d <= end]
    return statistics.median(vs) if vs else None


def year_ago(days: dict, d: dt.date):
    """1년 전 같은 날 앞뒤 10일의 중간값. 명절이 끼어 조사가 없는 날이 있어 넓게 잡는다."""
    y = d - dt.timedelta(days=364)
    return window_median(days, y - dt.timedelta(days=10), y + dt.timedelta(days=10))


def r10(v):
    return None if v is None else int(round(v / 10) * 10)


def pct(now, before):
    if not now or not before:
        return None
    return round((now - before) / before * 100, 1)


def get(c, start, end):
    cg, ic = c["_code"]
    return clean(fetch(cg, ic, start, end), c)


def build_item(c, latest: dt.date):
    this_mon = monday(latest)
    rows = get(c, this_mon - dt.timedelta(weeks=WEEKS), latest)
    recent = [r for r in rows if ymd(r["exmn_ymd"]) > latest - dt.timedelta(days=10)]
    if not recent:
        return None
    unit = Counter(unit_key(r) for r in recent).most_common(1)[0][0]
    rows = [r for r in rows if unit_key(r) == unit]
    days = daily(rows, lambda r: num(r["exmn_dd_prc"]))
    if not days:
        return None
    last = max(days)
    if last < latest - dt.timedelta(days=7):
        return None   # 조사가 끊긴 품목(철이 지난 과일 등)은 빼고, 다시 조사되면 저절로 나타난다
    now = days[last]
    prev = at_or_before(days, last - dt.timedelta(days=7))
    y_start = last - dt.timedelta(days=374)
    y_rows = [r for r in get(c, y_start, y_start + dt.timedelta(days=20)) if unit_key(r) == unit]
    year = year_ago(daily(y_rows, lambda r: num(r["exmn_dd_prc"])), last)

    # 사과 10개, 꽁치 5마리처럼 묶음으로 조사된 건 1개(1마리) 값으로 바꿔 보여 준다
    div = 1.0
    if unit[0] in ("개", "마리") and (num(unit[1]) or 1) > 1:
        div, unit = num(unit[1]), (unit[0], "1")
    now, prev, year = now / div, prev and prev / div, year and year / div
    wk = weekly(days)
    series = [{"w": w.isoformat(), "p": r10(v / div)} for w, v in wk.items() if w > this_mon - dt.timedelta(weeks=WEEKS)]
    return {
        "key": c["key"], "label": c["label"], "cat": c["cat"], "emoji": c["emoji"], "months": c["months"],
        "pick": c["pick"], "keep": c["keep"], "unit": unit_label(unit), "day": last.isoformat(),
        "price": r10(now), "prev": r10(prev), "year": r10(year),
        "wow": pct(now, prev), "yoy": pct(now, year),
        "alias": " ".join(a for a, keys in ALIASES.items() if c["key"] in keys),
        "grade": grade_of(c, recent),
        "markets": len({r.get("mrkt_cd") for r in rows if ymd(r["exmn_ymd"]) == last}),
        "weeks": series,
    }


def next_survey_month(key: str, latest: dt.date):
    """다음에 조사되는 달. 조사 달 정보가 없으면 None"""
    months = SURVEY.get(key)
    if not months:
        return None
    for k in range(12):
        m = (latest.month - 1 + k) % 12 + 1
        if m in months:
            return m
    return None


def build_upcoming(c, latest: dt.date):
    """지금 조사되지 않는 품목: 다음 조사 달과, 지난번 그 달 첫 조사 무렵 가격"""
    nxt = next_survey_month(c["key"], latest)
    ref = None
    if nxt:
        # 다음 조사 달이 이번 달이거나 뒤면 작년 그 달, 앞이면 올해 그 달을 본다
        year = latest.year - 1 if nxt >= latest.month else latest.year
        start = dt.date(year, nxt, 1)
        rows = get(c, start, start + dt.timedelta(days=30))
        if rows:
            first = min(ymd(r["exmn_ymd"]) for r in rows)
            rows = [r for r in rows if ymd(r["exmn_ymd"]) < first + dt.timedelta(days=7)]
            unit = Counter(unit_key(r) for r in rows).most_common(1)[0][0]
            vals = [num(r["exmn_dd_prc"]) for r in rows if unit_key(r) == unit and num(r["exmn_dd_prc"])]
            if vals:
                price = statistics.median(vals)
                if unit[0] in ("개", "마리") and (num(unit[1]) or 1) > 1:
                    price, unit = price / num(unit[1]), (unit[0], "1")
                ref = {"price": r10(price), "unit": unit_label(unit), "when": first.strftime("%Y-%m")}
    return {
        "key": c["key"], "label": c["label"], "cat": c["cat"], "emoji": c["emoji"], "months": c["months"],
        "pick": c["pick"], "keep": c["keep"],
        "alias": " ".join(a for a, keys in ALIASES.items() if c["key"] in keys),
        "next": nxt, "ref": ref,
    }


def grade_of(c, rows):
    """화면에 '○○ 기준'으로 적을 등급. 품종 이름과 같거나 '-' 같은 값은 적지 않는다."""
    if c.get("grd"):
        return {"大": "큰 것", "中": "중간 크기"}.get(c["grd"][0], c["grd"][0])
    names = {r.get("grd_nm") for r in rows}
    return "상품" if names == {"상품"} else None


def per_unit_price(rows, per):
    """김장 재료: kg 기준이면 kg 환산가, 개수 기준이면 개당 가격"""
    if per == "kg":
        return lambda r: num(r.get("exmn_dd_cnvs_prc"))

    def each(r):
        p, n = num(r.get("exmn_dd_prc")), num(r.get("unit_sz")) or 1
        return p / n if p else None
    return each


def build_kimjang(c, latest: dt.date):
    rows = get(c, latest - dt.timedelta(days=21), latest)
    y_start = latest - dt.timedelta(days=374)
    y_rows = get(c, y_start, y_start + dt.timedelta(days=20))
    if c["per"] == "kg":
        # kg 환산가가 없는 단위(포기·개)는 kg 기준 계산에 쓸 수 없다
        rows = [r for r in rows if str(r.get("unit")).lower() in ("kg", "g")]
        y_rows = [r for r in y_rows if str(r.get("unit")).lower() in ("kg", "g")]
    val = per_unit_price(rows, c["per"])
    days = daily(rows, val)
    now = at_or_before(days, latest)
    prev = at_or_before(days, latest - dt.timedelta(days=7))
    year = year_ago(daily(y_rows, val), latest)
    return {
        "key": c["key"], "label": c["label"], "qty": c["qty"], "unit": c["unit"],
        "unitPrice": r10(now), "prevUnitPrice": r10(prev), "yearUnitPrice": r10(year),
        "next": None if now else next_survey_month(c["key"], latest),
    }


def latest_day() -> dt.date:
    today = dt.datetime.now(KST).date()
    c = dict(name="배추", label="배추")
    c["_code"] = resolve(c)
    rows = fetch(*c["_code"], today - dt.timedelta(days=21), today)
    if not rows:
        raise SystemExit("최근 3주 배추 가격이 없어요. KAMIS 응답을 확인해 주세요.")
    return max(ymd(r["exmn_ymd"]) for r in rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", nargs="+", default=[str(OUT)], help="결과 JSON 경로 (여러 개 가능)")
    args = ap.parse_args()

    latest = latest_day()
    print("최근 조사일", latest)
    for c in ITEMS + KIMJANG:
        c["_code"] = resolve(c)

    with ThreadPoolExecutor(4) as ex:
        items = list(ex.map(lambda c: build_item(c, latest), ITEMS))
        kim = list(ex.map(lambda c: build_kimjang(c, latest), KIMJANG))

    missing = [c for c, it in zip(ITEMS, items) if it is None]
    items = [it for it in items if it]
    with ThreadPoolExecutor(4) as ex:
        upcoming = list(ex.map(lambda c: build_upcoming(c, latest), missing))
    for it in items:
        print(f"{it['emoji']} {it['label']:6} {it['unit']:>6} {it['price']:>8,}원  {it['day']}  "
              f"전주 {it['wow'] if it['wow'] is not None else '-':>6}%  "
              f"1년전 {it['yoy'] if it['yoy'] is not None else '-':>6}%  시장 {it['markets']}")
    for u in upcoming:
        ref = u["ref"]
        print(f"곧 나와요 {u['emoji']} {u['label']:6} 다음 {u['next']}월  "
              f"{'지난번 ' + ref['when'] + ' ' + ref['unit'] + ' ' + format(ref['price'], ',') + '원' if ref else '참고 가격 없음'}")
    total = 0
    for k in kim:
        sub = (k["unitPrice"] or 0) * k["qty"]
        total += sub
        print(f"김장 {k['label']:5} {k['qty']}{k['unit']} × {k['unitPrice']} = {sub:,.0f}  "
              f"(전주 {k['prevUnitPrice']}, 1년전 {k['yearUnitPrice']})")
    print(f"김장 합계(20포기) {total:,.0f}원")

    cabbage = next((k for k in kim if k["label"] == "배추"), None)
    if len(items) < MIN_ITEMS or not cabbage or not cabbage["unitPrice"]:
        raise SystemExit(f"결과가 비정상이라 저장하지 않아요: 품목 {len(items)}개, 김장 배추 {cabbage}")

    out = {
        "schema": 2,
        "asOf": latest.isoformat(),
        "generatedAt": dt.datetime.now(KST).isoformat(timespec="minutes"),
        "source": "KAMIS 농산물유통정보(한국농수산식품유통공사) 일별 소매가격",
        "cats": [{"key": k, "emoji": v} for k, v in CATS.items()],
        "items": items,
        "upcoming": upcoming,
        "kimjang": {"source": KIMJANG_SOURCE, "base": 20, "items": kim},
    }
    body = json.dumps(out, ensure_ascii=False, separators=(",", ":"))
    for path in args.out:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
        print("저장:", p, f"{p.stat().st_size:,} bytes")


if __name__ == "__main__":
    main()
