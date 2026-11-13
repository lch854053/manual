# 통합민원대 업무지원서비스

문서 **열람·검색·다운로드만** 제공하는 GitHub Pages 정적 웹사이트입니다. 로그인, 업로드, 문서 수정, API, DB는 사용하지 않습니다.

## 로컬 실행

```sh
python3 -m venv .venv-build
.venv-build/bin/pip install -r requirements-build.txt
.venv-build/bin/python scripts/build.py
python3 -m http.server 8080 --directory site
```

http://localhost:8080 에서 확인합니다. `file://` 대신 HTTP 서버로 실행하세요.

## 구성과 자료 갱신

- `web/`: 정적 HTML·CSS·JavaScript
- `documents/`: 공개할 원본 자료. 원본 파일명과 하위 폴더를 유지합니다.
- `scripts/build.py`: 원본 복사 및 PDF·HWPX 검색용 본문 생성
- `site/`: 자동 생성되는 배포물 (Git 제외)
- `.github/workflows/pages.yml`: main 브랜치에서 빌드·Pages 배포

PDF는 쪽별 본문 검색과 브라우저 PDF 열람을 지원합니다. HWPX는 추출 본문 열람 및 검색, 원본 다운로드를 지원합니다. HWP는 다운로드만 지원하며 이미지와 스캔 문서에는 OCR을 적용하지 않습니다. PDF 미리보기 미지원 브라우저에서는 원문 열기나 다운로드를 사용하세요. 표와 강조색은 PDF 원문을 기준으로 확인합니다.

통합매뉴얼의 12개 업무별 쪽수는 `scripts/build.py`의 `RANGES`에 있습니다. 개정 시 실제 원문과 대조해 수정하세요. 현재 원본은 2026.11.12.판 73쪽입니다. 자료 교체·추가는 `documents/`에서 수행한 후 다시 빌드합니다. 삭제된 자료가 남지 않도록 배포는 매번 깨끗한 Actions 환경에서 빌드합니다.

## GitHub Pages 배포

1. **공개 가능한 자료인지 먼저 검토**합니다. 원본·추출 본문·이미지에 개인정보, 서명, 내부 계정·접속정보가 없는지 직접 확인해야 합니다. 자동 개인정보 검사를 수행하거나 안전성을 보증하지 않습니다.
2. 변경을 main 브랜치에 커밋·푸시합니다.
3. 저장소 Settings → Pages → Source를 **GitHub Actions**로 설정합니다.
4. Actions의 `Deploy document library to Pages` 실행 결과를 확인합니다.

예상 주소: https://lch854053.github.io/manual/

모든 자산 경로는 상대경로이므로 `/manual/` 하위 경로에서도 동작합니다. `site/`만 배포하며 기존 `app/`, `static/`, `tests/`의 FastAPI 서버 코드는 이전 구현 참고용이고 정적 사이트에서 사용하지 않습니다. 기존 관리자 화면·API·키는 배포되지 않습니다.

## 확인

```sh
node --check web/app.js
.venv-build/bin/python -m unittest discover -s tests_static
```
