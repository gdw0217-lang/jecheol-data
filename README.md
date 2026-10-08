# jecheol-data

토스 미니앱 「제철장보기」가 읽는 농수산물 소매가격 파일을 만든다.

- 결과: `docs/market.json` → GitHub Pages 로 공개 (`https://<계정>.github.io/jecheol-data/market.json`)
- 갱신: GitHub Actions 가 하루 세 번(17시·21시·다음 날 7시, 한국 시간) `scripts/fetch_market.py` 를 돌린다
- 데이터: 한국농수산식품유통공사 KAMIS 일별 도·소매 가격정보 (공공데이터포털, 이용허락범위 제한 없음)
- 인증키: 저장소 비밀값 `KAMIS_SERVICE_KEY` (코드와 결과 파일에는 들어가지 않는다)

로컬에서 돌리기:

```sh
pip install -r requirements.txt
KAMIS_SERVICE_KEY=... python scripts/fetch_market.py
```

품목·제철 달·고르는 법은 `scripts/catalog.py`, 품목 코드는 `scripts/codes.xlsx`(공공데이터포털 제공 코드표)에 있다.
