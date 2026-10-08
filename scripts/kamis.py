"""KAMIS 일별 도·소매 가격 조회 공통 함수.

인증키는 환경변수 KAMIS_SERVICE_KEY(GitHub Actions 비밀값)에서 읽고,
없으면 이 PC의 D:/new2/.secrets/kamis.env 에서 읽는다. 키는 코드·결과물에 남기지 않는다.
"""
import datetime as dt
import os
import time
from pathlib import Path

import requests

BASE = "https://apis.data.go.kr/B552845/perDay"


def _key() -> str:
    if os.environ.get("KAMIS_SERVICE_KEY"):
        return os.environ["KAMIS_SERVICE_KEY"].strip()
    env = Path("D:/new2/.secrets/kamis.env")
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith("KAMIS_SERVICE_KEY="):
                return line.split("=", 1)[1].strip()
    raise SystemExit("KAMIS 인증키가 없어요. 환경변수 KAMIS_SERVICE_KEY 를 설정해 주세요.")


KEY = _key()
URL = BASE + "/price"


def fetch(ctgry, item, start: dt.date, end: dt.date, se="01"):
    rows, page = [], 1
    while True:
        p = {"serviceKey": KEY, "returnType": "JSON", "pageNo": page, "numOfRows": 1000,
             "cond[exmn_ymd::GTE]": start.strftime("%Y%m%d"), "cond[exmn_ymd::LTE]": end.strftime("%Y%m%d"),
             "cond[se_cd::EQ]": se, "cond[ctgry_cd::EQ]": ctgry, "cond[item_cd::EQ]": item}
        for attempt in range(4):
            try:
                r = requests.get(URL, params=p, timeout=30)
                r.raise_for_status()
                d = r.json()
                break
            except Exception as e:
                if attempt == 3:
                    # 오류 메시지에 인증키가 섞여 나가지 않게 주소는 빼고 알린다
                    raise RuntimeError(f"KAMIS 조회 실패: {ctgry}/{item} {type(e).__name__}") from None
                time.sleep(2 * (attempt + 1))
        body = d.get("response", {}).get("body", d.get("body", {}))
        its = body.get("items") or []
        its = its.get("item", []) if isinstance(its, dict) else its
        rows += its
        total = int(body.get("totalCount") or 0)
        if page * 1000 >= total or not its:
            return rows
        page += 1
