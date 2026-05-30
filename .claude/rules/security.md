---
description: 보안 민감 영역 규칙 (시크릿 · 환경변수)
globs: ["**/.env*", "**/config.py", "scripts/**"]
---

# 보안 민감 영역

현재 giljobe엔 인증·DB·SQL이 없다. 지금 적용되는 건 **시크릿/환경변수**뿐.

- `HF_TOKEN`·`VLLM_BASE_URL` 등 비밀·환경값을 **출력·로그·커밋 금지**. 하드코딩 금지(환경변수 사용).
- `serving/*.sh`·docker 실행에 토큰을 넘길 때 콘솔/로그에 토큰이 찍히지 않게 한다.

> 인증·DB·SQL·마이그레이션 모듈이 실제로 생기면 그때 이 파일에 parameterized-query·인증변경 승인·마이그레이션 dry-run 규칙을 추가한다. **없는 제약을 미리 만들지 않는다**(`project-structure.md` 원칙).
