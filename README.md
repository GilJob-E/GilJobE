# giljobe

면접 시뮬레이션 시스템의 **입력단** — 사용자의 WebRTC 웹캠+마이크 스트림을 받아 **gje(MMM) 모듈로 비언어 비평 + STT 전사**를 수행하고, 다운스트림 면접관 LLM이 먹을 **`.json`을 송출**한다.

- 전체 루프: `USER(webRTC) → [gje(MMM)+STT(.json)] → 면접관 LLM → SpatialReal/Elevenlabs TTS → USER`
- 이 레포 스코프: 위 루프의 **첫 박스 하나**(입력 수신 + 비언어 비평 + 전사 + JSON 송출). 면접관 LLM·TTS·아바타·최종평가는 범위 밖.
- 구조도: `system_diagram_main.png`

## 기술 스택
Python 3.12 · aiortc(WebRTC) · gje(MMM)=vLLM+Gemma-4-E4B(네이티브 AV) · faster-whisper(STT 폴백) · aiohttp · ffmpeg

## 상태
초기 스캐폴드 단계. 구현은 시작 전.
- 구현 설계·빌드 순서 → **`PLAN.md`**
- 프로젝트 지도·작업 규칙 → **`CLAUDE.md`**, `.claude/rules/`

## gje 원본
gje(MMM)는 `/home/kio/workspace/gje`의 별도 repo다. **import하지 않고**, 필수 모듈만 `src/giljobe/`로 **복사**해 리팩토링한다. (원본 디렉토리는 이동/수정하지 않는다.)

## 실행
빌드 후 채움 — `PLAN.md` §6(검증) 참고.
