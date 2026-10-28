# data 브랜치 (자동 기록 전용)

1시간 경비원 작업이 쓰는 데이터. 코드는 없음. main 과 섞지 않으려고 따로 둠.

- `history/metrics.jsonl` — 매시간 핵심 지표 한 줄(JSON). 대시보드 성장 그래프의 원자료
- `notes/market_notes.json` — BTC 급변 시 찾은 뉴스 메모(시각, 변동, 요약, 출처)
- `guard/state.json` — 이미 보낸 알림 기록(중복 방지), 마지막 확인 시각

대시보드는 `https://raw.githubusercontent.com/btypenine/chamdom/data/...` 로 읽을 수 있음.
