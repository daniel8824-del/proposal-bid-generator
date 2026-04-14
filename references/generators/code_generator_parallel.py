"""
Phase별 병렬 코드 생성 — 8개 Phase를 동시 호출

각 Phase의 슬라이드 코드를 함수로 생성하고,
최종 스크립트에서 순차 호출하는 방식.

장점:
- 각 호출이 짧음 (2~3K 토큰) → 빠르고 잘 안 잘림
- 8개 병렬 → 전체 시간 = 가장 느린 Phase 1개 시간
- 토큰 한도 여유
"""

import asyncio
import ast
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import httpx

logger = logging.getLogger("code_generator_parallel")


PHASE_CODEGEN_SYSTEM = """당신은 slide_kit.py 전문가입니다. 주어진 Phase의 슬라이드 데이터를 받아서 Python 함수 하나를 작성합니다.

## 함수 시그니처
```python
def render_phase_{N}(prs, pg, WIN):
    \"\"\"Phase {N} 슬라이드 렌더링. pg는 시작 페이지번호. 반환: 마지막+1 페이지번호\"\"\"
    # 슬라이드들...
    return pg
```

## slide_kit 핵심 API
- new_slide(prs) → s
- bg(s, c) — 단색 배경
- TB(s, title, pg=pg) — Action Title 상단바 + 페이지번호
- WB(s, theme_key, WIN) — Win Theme 뱃지
- slide_section_divider(prs, num, title, subtitle="", win_theme_key=None, win_themes=WIN)
- slide_cover(prs, project_name, client_name, year, tagline, company_name)
- slide_closing(prs, message, tagline, project_title, contact)
- slide_exec_summary(prs, title, one_liner, win_themes_dict, kpis, why_us_points)
- slide_next_step(prs, headline, steps, contact="") — steps는 **4-튜플 리스트**: `[(step_label, title, desc, color), ...]` 예: `[("STEP 1", "킥오프", "2주", C["primary"]), ("STEP 2", "실행", "8주", C["teal"])]`. contact는 **문자열** (dict 금지).
- slide_closing(prs, message="감사합니다", tagline="", project_title="", contact="") — 모든 인자는 **문자열**. contact가 dict면 `f"{name} | {email}"` 형태로 먼저 변환할 것.
- slide_toc(prs, title, items, pg)
- T(s, l, t, w, h, text, sz=13, c=None, b=False, fn=None)
- MT(s, l, t, w, h, lines, sz=11, bul=True)
- BOX(s, l, t, w, h, f, text="", sz=13, tc=None, b=False)
- HIGHLIGHT(s, text, sub="", y=None, color=None, grad=False)
- KPIS(s, items, y=Inches(1.2)) — items=[{"value","label","basis"}]
- COLS(s, items, y=Inches(1.2)) — items=[{"title","body":[..]}]
- FLOW(s, items, y=Inches(1.2)) — items=[("title","desc")]
- TABLE(s, headers, rows, y=Inches(1.2))
- COMPARE(s, left_title, left_items, right_title, right_items, y=Inches(1.2))
- TIMELINE(s, items, y=Inches(1.2)) — items=[("기간","내용")]
- PYRAMID(s, levels, y=Inches(1.2)) — levels=[("text", color)]

## 상수
- C["primary"], C["secondary"], C["teal"], C["accent"], C["dark"], C["white"], C["light"], C["gray"], C["lgray"]
- SZ["hero"], SZ["divider"], SZ["action"], SZ["subtitle"], SZ["body"], SZ["body_sm"], SZ["caption"]
- FONT_W["bold"], FONT_W["semibold"], FONT_W["medium"]
- ML, CW, SW, SH, Inches, Pt, PP_ALIGN

## 규칙
1. **함수 하나만 작성** — `def render_phase_{N}(prs, pg, WIN):` ... `return pg`
2. **모든 슬라이드 렌더링** — 데이터의 슬라이드 전부 처리 (누락 금지)
3. **Action Title 유지** — slide.title을 그대로 TB()에 전달
4. **각 슬라이드 후 pg += 1**
5. **Phase 시작 시 section_divider 추가** (Phase 0 제외): `slide_section_divider(prs, PHASE_NUM, title, subtitle="...", win_theme_key=None, win_themes=WIN)` 그 다음 `pg += 1`
6. **import 금지** — 호출자가 처리함
7. **C[], SZ[], FONT_W[] 상수 사용** — 하드코딩 금지
8. **try/except 금지**
9. **빈 슬라이드/None 체크**: 슬라이드 데이터가 없는 필드는 스킵

## 🎨 디자인 품질 규칙 (필수 준수)

### Zone 시스템 (Y 좌표 절대 규칙)
- **타이틀 바**: 0.0" ~ 0.88" (TB() 함수가 자동 처리)
- **콘텐츠 영역**: 1.1" ~ 6.5" (콘텐츠는 반드시 이 영역 안에)
- **푸터**: 6.7" ~ 7.5" (페이지 번호, 출처)
- ❌ 콘텐츠가 6.5"를 초과하면 안 됨
- ❌ Y < 1.1이면 타이틀 바와 겹침

### 컬러 대비 (가독성 필수)
- **다크 배경** (bg(s, C["dark"])) → 텍스트는 반드시 **C["white"]** 또는 **C["lgray"]** 만 사용
- **흰 배경** (bg(s, C["white"])) → 텍스트는 C["dark"], C["primary"], C["gray"] 사용
- ❌ 다크 배경에 C["dark"] 텍스트 금지 (안 보임)
- ❌ 흰 배경에 C["white"] 텍스트 금지
- ❌ BOX의 f(채우기)와 tc(텍스트)가 같은 계열이면 안 됨

### 텍스트 크기 & 줄 수
- **대형 타이틀 (44pt+)**: 18자 초과 시 반드시 2줄로 분리 (T() 2번 호출)
- **Action Title (TB)**: 30자 이내 권장
- **MT 불릿**: SZ["body_sm"] (11pt) 또는 SZ["body"] (13pt) 사용
- **MT 높이 계산**: h = 줄수 × 0.35" 최소 (3줄=1.1", 4줄=1.4", 5줄=1.7", 6줄=2.0")
- ❌ 높이 고정 h=4.0" 에 줄수 2개만 넣으면 여백 과다 → **h는 반드시 줄수에 비례**
- ❌ 긴 텍스트를 작은 영역에 넣으면 오버플로우

### 빈 공간 방지 (필수)
- **HIGHLIGHT만 1~2개인 슬라이드 금지** — 반드시 MT, COLS, TABLE, FLOW 중 하나 이상 추가
- **콘텐츠 영역(1.1"~6.5") 중 50% 이상 비어있으면 안 됨** — 요소를 추가하거나 기존 요소 크기 조정
- **MT 높이는 실제 줄수에 맞추기** — `h = max(줄수 × 0.35, 1.0)` 패턴 사용. 고정값 h=4.0 금지
- 예시: 4줄 bullet → `MT(s, ML, y, CW, Inches(1.4), items)` (4×0.35=1.4)

### 요소 간 최소 간격
- HIGHLIGHT → 다음 요소: **0.75"**
- COLS → 다음 요소: **0.2"**
- MT → 다음 요소: **0.2"**
- KPIS → 다음 요소: **0.15"**
- 요소 겹침 절대 금지

### 다크 배경 슬라이드 패턴 (teaser, section_divider, key_message)
```python
s = new_slide(prs)
bg(s, C["dark"])
T(s, ML, Inches(2.5), CW, Inches(1.5), "강한 한 줄", sz=SZ["divider"], c=C["white"], b=True, al=PP_ALIGN.CENTER, fn=FONT_W["bold"])
PN(s, pg)
```

### 슬라이드 렌더링 기본 패턴 (콘텐츠 슬라이드)
```python
s = new_slide(prs)
bg(s, C["white"])
TB(s, slide_title, pg=pg)  # 타이틀 바 (자동으로 0~0.88 영역)
# 콘텐츠는 Inches(1.2) ~ Inches(6.5) 영역에만
HIGHLIGHT(s, key_message, y=Inches(1.2), color=C["primary"])  # 높이 약 0.7"
MT(s, ML, Inches(2.1), CW, Inches(4.0), bullets, sz=SZ["body_sm"], bul=True)  # 1.2+0.7+0.2=2.1
pg += 1
```

### KPIS 사용 시
items의 각 dict는 반드시 `value`, `label`, `basis` 키 포함:
```python
KPIS(s, [
    {"value": "30,000+", "label": "팔로워", "basis": "릴스 확대 +40%"},
    {"value": "500만+", "label": "도달", "basis": "월 40만 × 12"},
], y=Inches(1.2))
```

### COLS 사용 시
items의 각 dict는 `title`, `body` (list) 키 포함:
```python
COLS(s, [
    {"title": "Content", "body": ["제철 콘텐츠", "릴스 중심", "월 40건+"]},
    {"title": "Community", "body": ["참여형 이벤트", "UGC 수집"]},
], y=Inches(1.2))
```

### 오버플로우 방지
- 한 슬라이드에 너무 많은 요소 넣지 말 것 (최대 3~4개)
- 긴 텍스트는 줄여서 핵심만
- 각 요소의 Y 좌표 누적 계산 확인

### Phase 7 특별 규칙
- **slide_next_step(), slide_closing() 호출 금지** — 이 함수들은 별도 render_closing()에서 처리
- Phase 7 데이터에 "다음 단계", "next step" 관련 슬라이드가 있어도 **일반 슬라이드(TB+MT/COLS/FLOW)로 렌더링** — slide_next_step 호출하지 말 것

## 출력 형식
```python
def render_phase_{N}(prs, pg, WIN):
    # ...
    return pg
```

코드 블록 하나만 반환하세요. 설명 금지.
"""


PHASE_USER_TEMPLATE = """Phase {phase_num} ({phase_title}) 슬라이드를 렌더링하는 함수를 작성하세요.

## Phase 데이터
```json
{phase_json}
```

## Win Themes (참조용)
```json
{win_themes_json}
```

`def render_phase_{phase_num}(prs, pg, WIN):` 함수를 작성하세요. 모든 슬라이드를 렌더링하고 마지막에 `return pg`. ```python ``` 코드블록 하나만 반환."""


TEASER_USER_TEMPLATE = """Phase 0 (HOOK / Teaser) 슬라이드를 렌더링하는 함수를 작성하세요.

## Teaser 데이터
```json
{teaser_json}
```

## 표지 정보
- project_name: {project_name}
- client_name: {client_name}
- year: {year}
- tagline: {tagline}
- company_name: {company_name}

## 규칙
- Teaser 슬라이드: bg(s, C["dark"]) + 대형 텍스트 + 페이지번호
- title 슬라이드 발견 시: slide_cover(prs, project_name, client_name, year, tagline, company_name)
- key_message 슬라이드 발견 시: 다크 배경 + 36pt 중앙 텍스트

`def render_phase_0(prs, pg, WIN):` 함수를 작성하세요. ```python ``` 코드블록 하나만 반환."""


CLOSING_USER_TEMPLATE = """제안서 마지막의 Next Step + Closing 슬라이드를 렌더링하는 함수를 작성하세요.

## Next Step 데이터
```json
{next_step_json}
```

## 마무리 정보
- project_title: {project_title}
- contact: {contact}

## ⚠️ Closing 필수 규칙
1. **slide_next_step + slide_closing 두 함수만 호출** — 이 외 슬라이드 함수 호출 금지
2. **slide_section_divider 절대 금지** — section_divider를 추가하지 마세요
3. **slide_closing의 message는 8자 이내** — 예: "감사합니다", "Thank You". 긴 문장 금지 (SZ["hero"] 폰트에서 오버플로우 발생)
4. **slide_closing의 모든 인자는 문자열** — dict 금지

`def render_closing(prs, pg, WIN):` 함수를 작성하세요. slide_next_step과 slide_closing만 호출. ```python ``` 코드블록 하나만 반환."""


async def _call_llm(api_key: str, system: str, user: str, max_tokens: int = 8000, model: str = "anthropic/claude-sonnet-4-6") -> str:
    """단일 LLM 호출"""
    async with httpx.AsyncClient(timeout=300) as client:
        resp = await client.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]},
                    {"role": "user", "content": user},
                ],
                "max_tokens": max_tokens,
                "temperature": 0.2,
            },
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]


def _extract_code(response: str) -> Optional[str]:
    """코드 블록 추출 (관대)"""
    # ```python ... ```
    m = re.search(r"```python\s*\n([\s\S]*?)\n```", response)
    if m:
        return m.group(1).strip()
    # ``` ... ```
    m = re.search(r"```\s*\n([\s\S]*?)\n```", response)
    if m:
        return m.group(1).strip()
    # ```python ... (잘림)
    m = re.search(r"```python\s*\n([\s\S]*)$", response)
    if m:
        code = m.group(1).strip()
        if code.endswith("```"):
            code = code[:-3].strip()
        return code
    # raw
    if response.strip().startswith(("def ", "import ", "from ")):
        return response.strip()
    return None


def _slim_phase(p: dict) -> dict:
    """Phase 데이터 압축 — 필수 필드만"""
    def _slim_slide(s: dict) -> dict:
        return {k: v for k, v in s.items() if v not in (None, [], {}, "") and k not in ("notes", "raw_sections")}

    return {
        "phase_number": p.get("phase_number"),
        "phase_title": p.get("phase_title", ""),
        "phase_subtitle": p.get("phase_subtitle", ""),
        "win_theme": p.get("win_theme"),
        "slides": [_slim_slide(s) for s in p.get("slides", [])],
    }


async def generate_phase_code(api_key: str, phase_num: int, phase_data: dict, win_themes: dict, model: str = "") -> str:
    """단일 Phase 함수 코드 생성 (max_tokens=32768)"""
    import os as _os
    if not model:
        model = _os.environ.get("LLM_MODEL", "anthropic/claude-sonnet-4-6")
    system = PHASE_CODEGEN_SYSTEM.replace("{N}", str(phase_num))
    user = PHASE_USER_TEMPLATE.format(
        phase_num=phase_num,
        phase_title=phase_data.get("phase_title", ""),
        phase_json=json.dumps(_slim_phase(phase_data), ensure_ascii=False, indent=2),
        win_themes_json=json.dumps(win_themes, ensure_ascii=False),
    )
    response = await _call_llm(api_key, system, user, max_tokens=32768, model=model)
    code = _extract_code(response)
    if not code or f"def render_phase_{phase_num}" not in code:
        logger.error(f"Phase {phase_num} 함수 추출 실패 — 폴백 생성")
        return _fallback_phase_code(phase_num, phase_data)
    # 문법 검증 — 잘린 코드면 폴백 (슬라이드 복구)
    try:
        ast.parse(code)
    except SyntaxError as e:
        logger.warning(f"Phase {phase_num} 코드 잘림 ({e}) — 슬라이드 복구 폴백")
        return _fallback_phase_code(phase_num, phase_data)
    return code


def _fallback_phase_code(phase_num: int, phase_data: dict) -> str:
    """폴백: phase의 모든 슬라이드를 단순 TB + MT 형태로 복구 (내용 누락 방지)"""
    phase_title = phase_data.get("phase_title", f"Phase {phase_num}")
    phase_subtitle = phase_data.get("phase_subtitle", "")
    slides = phase_data.get("slides", [])

    def _escape(s):
        if not isinstance(s, str):
            s = str(s)
        return s.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n')

    lines = [f"def render_phase_{phase_num}(prs, pg, WIN):"]
    # 섹션 구분자
    lines.append(f'    slide_section_divider(prs, {phase_num}, "{_escape(phase_title)}", subtitle="{_escape(phase_subtitle)}", win_themes=WIN)')
    lines.append("    pg += 1")

    for slide in slides:
        if not isinstance(slide, dict):
            continue
        title = _escape(slide.get("title", "") or "(제목 없음)")
        lines.append(f"    # slide: {title[:40]}")
        lines.append("    s = new_slide(prs)")
        lines.append('    bg(s, C["white"])')
        lines.append(f'    TB(s, "{title}", pg=pg)')

        # bullets 복구
        bullets = slide.get("bullets") or []
        bullet_texts = []
        for b in bullets[:6]:
            if isinstance(b, dict):
                txt = b.get("text", "")
            elif isinstance(b, str):
                txt = b
            else:
                txt = str(b)
            if txt:
                bullet_texts.append(_escape(txt))

        # key_message
        key_msg = slide.get("key_message")
        if key_msg:
            lines.append(f'    HIGHLIGHT(s, "{_escape(key_msg)}", y=Inches(1.2), color=C["primary"])')
            if bullet_texts:
                items = "[" + ", ".join(f'"{b}"' for b in bullet_texts) + "]"
                lines.append(f'    MT(s, ML, Inches(2.2), CW, Inches(4.0), {items}, sz=SZ["body_sm"], bul=True)')
        elif bullet_texts:
            items = "[" + ", ".join(f'"{b}"' for b in bullet_texts) + "]"
            lines.append(f'    MT(s, ML, Inches(1.3), CW, Inches(4.5), {items}, sz=SZ["body_sm"], bul=True)')

        lines.append("    pg += 1")
        lines.append("")

    lines.append("    return pg")
    return "\n".join(lines)


async def generate_teaser_code(api_key: str, content: dict, model: str = "") -> str:
    """Phase 0 Teaser 함수 코드 생성"""
    import os as _os
    if not model:
        model = _os.environ.get("LLM_MODEL", "anthropic/claude-sonnet-4-6")
    teaser = content.get("teaser", {})
    if not teaser:
        return "def render_phase_0(prs, pg, WIN):\n    return pg"

    system = PHASE_CODEGEN_SYSTEM.replace("{N}", "0")
    user = TEASER_USER_TEMPLATE.format(
        teaser_json=json.dumps(teaser, ensure_ascii=False, indent=2),
        project_name=content.get("project_name", "[프로젝트명]"),
        client_name=content.get("client_name", "[발주처명]"),
        year=content.get("submission_date", "2026")[:4],
        tagline=content.get("slogan") or teaser.get("main_slogan", ""),
        company_name=content.get("company_name", "[회사명]"),
    )
    response = await _call_llm(api_key, system, user, max_tokens=16384, model=model)
    code = _extract_code(response)
    if not code or "def render_phase_0" not in code:
        logger.error("Teaser 함수 추출 실패 — 폴백")
        return _fallback_teaser_code(content)
    try:
        ast.parse(code)
    except SyntaxError:
        logger.warning("Teaser 코드 잘림 — 폴백")
        return _fallback_teaser_code(content)
    return code


def _fallback_teaser_code(content: dict) -> str:
    """Teaser 폴백: 다크 배경 슬라이드 + 표지"""
    def _esc(s):
        if not isinstance(s, str):
            s = str(s)
        return s.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n')

    project_name = _esc(content.get("project_name", "[프로젝트명]"))
    client_name = _esc(content.get("client_name", "[발주처명]"))
    year = str(content.get("submission_date", "2026"))[:4] or "2026"
    slogan = _esc(content.get("slogan") or content.get("teaser", {}).get("main_slogan", ""))
    company_name = _esc(content.get("company_name", "[회사명]"))

    lines = ["def render_phase_0(prs, pg, WIN):"]
    teaser = content.get("teaser") or {}
    slides = teaser.get("slides", [])
    for slide in slides:
        if not isinstance(slide, dict):
            continue
        stype = slide.get("slide_type", "teaser")
        title = _esc(slide.get("title", "") or slide.get("key_message", "") or "")
        if stype == "title":
            continue  # 표지는 아래서 별도 처리
        lines.append("    s = new_slide(prs)")
        lines.append('    bg(s, C["dark"])')
        lines.append(f'    T(s, ML, Inches(2.8), CW, Inches(1.5), "{title}", sz=SZ["divider"], c=C["white"], b=True, al=PP_ALIGN.CENTER, fn=FONT_W["bold"])')
        lines.append("    PN(s, pg)")
        lines.append("    pg += 1")
        lines.append("")

    # 표지
    lines.append(f'    slide_cover(prs, "{project_name}", "{client_name}", year="{year}", tagline="{slogan}", company_name="{company_name}")')
    lines.append("    pg += 1")
    lines.append("    return pg")
    return "\n".join(lines)


async def generate_closing_code(api_key: str, content: dict, model: str = "") -> str:
    """Closing 함수 코드 생성"""
    import os as _os
    if not model:
        model = _os.environ.get("LLM_MODEL", "anthropic/claude-sonnet-4-6")
    next_step = content.get("next_step")
    if not next_step:
        return None  # 없으면 생성 안 함

    system = PHASE_CODEGEN_SYSTEM.replace("def render_phase_{N}", "def render_closing").replace("Phase {N}", "Closing")
    user = CLOSING_USER_TEMPLATE.format(
        next_step_json=json.dumps(next_step, ensure_ascii=False, indent=2),
        project_title=content.get("project_name", ""),
        contact=str((next_step or {}).get("contact_info", "")),
    )
    response = await _call_llm(api_key, system, user, max_tokens=8000, model=model)
    code = _extract_code(response)
    if not code or "def render_closing" not in code:
        return None
    try:
        ast.parse(code)
    except SyntaxError:
        return None
    return code


async def generate_pptx_code_parallel(
    content_json: Dict[str, Any],
    output_path: Path,
    script_path: Path,
    api_key: str,
    progress_callback: Optional[Callable] = None,
    model: str = "",
) -> Path:
    """
    Phase별 병렬 코드 생성 → 합쳐서 generate_proposal.py 저장
    """
    import os as _os
    if not model:
        model = _os.environ.get("LLM_MODEL", "anthropic/claude-sonnet-4-6")
    if not api_key:
        raise ValueError("API 키가 설정되지 않음")

    # Win Themes 추출
    win_themes = {}
    for i, wt in enumerate(content_json.get("win_themes") or []):
        if isinstance(wt, dict):
            key = wt.get("key") or f"theme{i}"
            win_themes[key] = wt.get("name", f"Win Theme {i+1}")

    if progress_callback:
        progress_callback({"phase": "codegen", "message": "8개 Phase를 병렬로 코드 생성 중..."})

    t0 = time.time()
    logger.info("Phase별 병렬 코드 생성 시작")

    # 병렬 호출 준비
    phases = content_json.get("phases", [])
    tasks = [generate_teaser_code(api_key, content_json, model=model)]
    for phase in phases:
        phase_num = phase.get("phase_number")
        if phase_num is not None:
            tasks.append(generate_phase_code(api_key, phase_num, phase, win_themes, model=model))

    # closing 별도 시도
    if content_json.get("next_step"):
        tasks.append(generate_closing_code(api_key, content_json, model=model))

    # 병렬 실행
    results = await asyncio.gather(*tasks, return_exceptions=True)

    elapsed = time.time() - t0
    logger.info(f"병렬 코드 생성 완료 ({elapsed:.1f}s, {len(results)}개)")

    # 결과 분리
    phase_codes = []
    closing_code = None
    has_closing = bool(content_json.get("next_step"))

    for i, r in enumerate(results):
        if isinstance(r, Exception):
            logger.error(f"Task {i} 실패: {r}")
            continue
        if r is None:
            continue
        if has_closing and i == len(results) - 1:
            closing_code = r
        else:
            phase_codes.append(r)

    if not phase_codes:
        raise ValueError("모든 Phase 코드 생성 실패")

    logger.info(f"성공한 Phase 코드: {len(phase_codes)}개")

    # 최종 스크립트 조립
    final_code = _assemble_script(
        phase_codes=phase_codes,
        closing_code=closing_code,
        content=content_json,
        win_themes=win_themes,
        output_path=output_path,
    )

    # 문법 검증
    try:
        ast.parse(final_code)
    except SyntaxError as e:
        debug_path = script_path.with_suffix(".debug.py")
        debug_path.parent.mkdir(parents=True, exist_ok=True)
        debug_path.write_text(final_code, encoding="utf-8")
        raise ValueError(f"조립된 코드 문법 오류: {e} (디버그: {debug_path})")

    # 저장
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text(final_code, encoding="utf-8")
    logger.info(f"스크립트 저장: {script_path} ({len(final_code):,}자)")

    if progress_callback:
        progress_callback({"phase": "codegen", "message": f"코드 생성 완료 ({len(final_code):,}자, {elapsed:.0f}s)"})

    return script_path


def _patch_code(code: str) -> str:
    """LLM 코드의 흔한 실수 자동 수정"""
    # num=숫자 → num="0숫자" (slide_section_divider 시그니처 보호)
    def fix_num(m):
        n = int(m.group(1))
        return f'num="{n:02d}"'
    code = re.sub(r'num\s*=\s*(\d+)', fix_num, code)
    return code


def _strip_closing_calls(code: str) -> str:
    """Phase 7 코드에서 slide_next_step/slide_closing 호출 라인 제거 (중복 방지)"""
    lines = code.split("\n")
    filtered = []
    skip_next_pg = False
    for line in lines:
        stripped = line.strip()
        if "slide_next_step(" in stripped or "slide_closing(" in stripped:
            skip_next_pg = True
            continue
        if skip_next_pg and stripped == "pg += 1":
            skip_next_pg = False
            continue
        skip_next_pg = False
        filtered.append(line)
    return "\n".join(filtered)


def _assemble_script(phase_codes: List[str], closing_code: Optional[str], content: dict, win_themes: dict, output_path: Path) -> str:
    """Phase별 함수들을 합쳐서 최종 스크립트 작성"""
    win_themes_str = json.dumps(win_themes, ensure_ascii=False, indent=4)
    # 프로젝트 루트 절대경로
    project_root = str(Path(__file__).parent.parent.parent.resolve())

    # 각 phase code 패치
    phase_codes = [_patch_code(c) for c in phase_codes]
    if closing_code:
        closing_code = _patch_code(closing_code)
        # 이중 안전장치: closing_code가 있으면 Phase 7에서 slide_next_step/slide_closing 호출 제거
        phase_codes = [_strip_closing_calls(c) if "render_phase_7" in c else c for c in phase_codes]

    header = f'''#!/usr/bin/env python3
"""자동 생성된 제안서 PPTX 스크립트 — Phase별 병렬 코드 생성"""
import sys, os
sys.path.insert(0, r"{project_root}")
from app.generators.slide_kit import *

WIN = {win_themes_str}

OUTPUT_PATH = r"{output_path}"

'''

    # 각 Phase 함수 합치기
    body = "\n\n".join(phase_codes)
    if closing_code:
        body += "\n\n" + closing_code

    # main 호출 부분
    phase_nums = []
    for code in phase_codes:
        m = re.search(r"def render_phase_(\d+)", code)
        if m:
            phase_nums.append(int(m.group(1)))
    phase_nums.sort()

    main_calls = []
    for n in phase_nums:
        main_calls.append(f"    pg = render_phase_{n}(prs, pg, WIN)")
    if closing_code and "render_closing" in closing_code:
        main_calls.append(f"    pg = render_closing(prs, pg, WIN)")

    main = f'''

if __name__ == "__main__":
    prs = new_presentation()
    pg = 1
{chr(10).join(main_calls)}
    save_pptx(prs, OUTPUT_PATH)
'''

    return header + body + main
