"""
SVG 고급 렌더링 엔진 — Phase별 병렬 SVG 생성 + 에이전트 루프

LLM이 슬라이드를 SVG로 생성하고,
svg_to_pptx 패키지가 DrawingML 네이티브로 변환한다.

Flow:
  ProposalContent → Phase별 SVG 생성 (병렬)
  → SVG 검증 → 실패 시 LLM에 수정 요청 (max 2회)
  → create_pptx_with_native_svg() → PPTX
"""

import asyncio
import json
import logging
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from xml.etree import ElementTree as ET

import httpx

logger = logging.getLogger("svg_generator")

# ppt-master 규칙에 따른 금지 요소
BANNED_SVG_ELEMENTS = {"clipPath", "mask", "style", "foreignObject", "textPath", "animate", "animateTransform", "animateMotion", "set"}
BANNED_SVG_ATTRS = {"class"}

MAX_FIX_RETRIES = 2
MAX_SLIDES = 120

# ── Design System Colors (slide_kit과 동일) ──
COLORS = {
    "primary": "#01696F",
    "secondary": "#0D9488",
    "teal": "#14B8A6",
    "accent": "#F59E0B",
    "dark": "#1E293B",
    "white": "#FFFFFF",
    "light": "#F8FAFC",
    "gray": "#64748B",
    "lgray": "#CBD5E1",
}


# ═══════════════════════════════════════════
# System Prompt
# ═══════════════════════════════════════════

SVG_SYSTEM_PROMPT = """당신은 프레젠테이션 슬라이드를 SVG로 렌더링하는 전문가입니다.
주어진 슬라이드 데이터를 보고 PowerPoint 16:9 슬라이드 규격의 SVG를 생성합니다.

## 규격
- viewBox: `0 0 1280 720` (16:9, 고정)
- 폰트: `font-family="맑은 고딕, Malgun Gothic, Apple SD Gothic Neo, sans-serif"` (반드시 사용)
- 인코딩: UTF-8

## 디자인 시스템 (입찰 제안서 — 격식 있고 깔끔한 스타일)
- Primary: #01696F (타이틀바, 포인트 — 최소한으로만 사용)
- Dark: #1E293B (제목 텍스트)
- White: #FFFFFF (기본 배경 — 대부분의 슬라이드는 흰 배경)
- Light: #F8FAFC (보조 배경, 카드, 구분선 내부)
- Gray: #64748B (보조 텍스트)
- LGray: #E2E8F0 (테이블 행, 구분선)
- Accent: #01696F (강조 숫자 — 제한적 사용)

## 핵심 톤앤매너
- **입찰 제안서**답게: 화려함보다 정보 전달 중심
- **흰 배경 기본**: 80% 이상의 슬라이드는 반드시 흰 배경
- **컬러 절제**: Primary 컬러는 타이틀바와 핵심 강조에만. 그 외는 흑백+회색
- **표, 숫자, 도표 중심**: 그래픽보다 정보 밀도
- **다크 배경 사용 제한**: 표지, 섹션 구분자, 감사 슬라이드만

## 슬라이드 레이아웃 시스템
1. **타이틀 바** (y: 0~63): 상단 Primary 컬러 바 (얇게) + 흰색 제목 텍스트 (18pt)
2. **콘텐츠 영역** (y: 80~620): 메인 콘텐츠 (충분한 여백)
3. **푸터** (y: 680~720): 페이지 번호 + 회사명 (작게)

## 슬라이드 유형별 패턴

### section_divider (섹션 구분자)
- 흰 배경 + 왼쪽에 Primary 세로선 (두께 6px)
- 큰 Phase 번호 (48pt, Primary, 볼드) + 제목 (28pt, Dark)
- 하단에 부제목 (14pt, Gray)

### content (일반 콘텐츠)
- 흰 배경 + Primary 타이틀바
- key_message → 얇은 Primary 좌측 보더 + Light 배경 박스 (화려한 배경 금지)
- bullets → 왼쪽 정렬 리스트 (• 마크, 14pt, Dark)

### two_column (2단)
- 타이틀바 + 좌우 2단 분할 (Light 카드 배경)
- 각 컬럼에 서브 제목 (14pt 볼드) + 불릿 리스트

### table (표)
- 타이틀바 + 깔끔한 테이블 그리드
- 헤더: Primary 배경 + 흰 텍스트 (높이 작게)
- 행: 번갈아 흰/Light 배경, 회색 얇은 구분선

### key_message (강조 메시지)
- 흰 배경 + 중앙 정렬
- 대형 텍스트 (24pt, Dark, 볼드)
- 보조 텍스트 (14pt, Gray)
- 하단에 Primary 가는 선 (accent)

### teaser (티저)
- 흰 배경 + 큰 텍스트 (Dark) + Primary 포인트 밑줄

### cover (표지)
- 흰 배경 + 상단 Primary 바 (높이 80px)
- 프로젝트명 (28pt, Dark, 볼드), 발주처 (16pt, Gray), 연도, 슬로건
- 깔끔하고 격식 있는 느낌

### closing (마무리)
- 흰 배경 + 중앙 "감사합니다" (28pt, Dark) + 연락처 (14pt, Gray)

## ⚠️ SVG 규칙 (PowerPoint 호환성 — 반드시 준수)
- ❌ `<clipPath>` 금지
- ❌ `<mask>` 금지
- ❌ `<style>` 태그 금지 (인라인 스타일만 가능)
- ❌ `class` 속성 금지
- ❌ `<foreignObject>` 금지
- ❌ `<textPath>` 금지
- ❌ `<animate>`, `<animateTransform>`, `<animateMotion>`, `<set>` 금지
- ✅ `<rect>`, `<circle>`, `<ellipse>`, `<line>`, `<polyline>`, `<polygon>`, `<path>`, `<text>`, `<tspan>`, `<g>`, `<image>`, `<defs>`, `<linearGradient>`, `<radialGradient>`, `<stop>` 허용
- 인라인 스타일은 OK (style="fill: #xxx")

## 텍스트 규칙
- 긴 텍스트는 `<tspan>` 으로 줄 바꿈 처리 (dy="1.2em")
- 한 줄 최대 약 35자 (한글 기준, 16pt 이하)
- 불릿 기호: "•" 사용

## 출력 형식
순수 SVG 코드만 반환. ```svg ``` 코드블록으로 감싸세요. 설명 금지.
"""

SVG_SLIDE_USER_TEMPLATE = """다음 슬라이드를 SVG로 렌더링하세요.

## 슬라이드 정보
- 슬라이드 유형: {slide_type}
- 제목: {title}
- 페이지 번호: {page_num}
{extra_data}

순수 SVG 코드만 출력. ```svg ``` 코드블록 하나만 반환."""


SVG_FIX_USER_TEMPLATE = """아래 SVG에 문제가 있습니다. 수정해주세요.

## 문제점
{issues}

## 원본 SVG
```svg
{svg_content}
```

수정된 SVG를 ```svg ``` 코드블록 하나만 반환하세요. 설명 금지."""


# ═══════════════════════════════════════════
# LLM 호출
# ═══════════════════════════════════════════

async def _call_llm(api_key: str, system: str, user: str, max_tokens: int = 16000, model: str = "") -> str:
    import os
    if not model:
        model = os.environ.get("LLM_MODEL", "anthropic/claude-sonnet-4-6")
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
                "temperature": 0.3,
            },
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]


def _extract_svg(response: str) -> Optional[str]:
    """응답에서 SVG 코드 추출"""
    # ```svg ... ```
    m = re.search(r"```svg\s*\n([\s\S]*?)\n```", response)
    if m:
        return m.group(1).strip()
    # ```xml ... ```
    m = re.search(r"```xml\s*\n([\s\S]*?)\n```", response)
    if m:
        return m.group(1).strip()
    # ``` ... ```
    m = re.search(r"```\s*\n([\s\S]*?)\n```", response)
    if m:
        code = m.group(1).strip()
        if code.startswith("<svg") or code.startswith("<?xml"):
            return code
    # raw SVG
    if response.strip().startswith("<svg") or response.strip().startswith("<?xml"):
        return response.strip()
    return None


# ═══════════════════════════════════════════
# SVG 검증
# ═══════════════════════════════════════════

def validate_svg(svg_content: str) -> List[str]:
    """SVG 검증 — 금지 요소/속성, XML 파싱, viewBox 확인"""
    issues = []

    # 1. XML 파싱
    try:
        # namespace 제거 후 파싱
        clean = re.sub(r'\sxmlns[^=]*="[^"]*"', '', svg_content)
        root = ET.fromstring(clean)
    except ET.ParseError as e:
        issues.append(f"XML 파싱 오류: {e}")
        return issues

    # 2. viewBox 확인
    vb = root.get("viewBox", "")
    if not vb:
        issues.append("viewBox 속성 누락 — `viewBox=\"0 0 1280 720\"` 필요")

    # 3. 금지 요소 검사
    def _check_element(el, path=""):
        tag = el.tag.split("}")[-1] if "}" in el.tag else el.tag
        if tag in BANNED_SVG_ELEMENTS:
            issues.append(f"금지 요소 <{tag}> 발견 (경로: {path}/{tag})")
        for attr in el.attrib:
            attr_name = attr.split("}")[-1] if "}" in attr else attr
            if attr_name in BANNED_SVG_ATTRS:
                issues.append(f"금지 속성 '{attr_name}' 발견 (<{tag}>)")
        for child in el:
            _check_element(child, f"{path}/{tag}")

    _check_element(root)

    return issues


# ═══════════════════════════════════════════
# 슬라이드 데이터 → SVG 프롬프트
# ═══════════════════════════════════════════

def _slide_to_prompt_data(slide: dict, page_num: int) -> str:
    """슬라이드 dict를 LLM 프롬프트 데이터로 변환"""
    slide_type = slide.get("slide_type", "content")
    title = slide.get("title", "")
    extra_lines = []

    if slide.get("subtitle"):
        extra_lines.append(f"- 부제목: {slide['subtitle']}")
    if slide.get("key_message"):
        extra_lines.append(f"- 핵심 메시지: {slide['key_message']}")

    # bullets
    bullets = slide.get("bullets", [])
    if bullets:
        bullet_texts = []
        for b in bullets[:8]:
            if isinstance(b, dict):
                bullet_texts.append(b.get("text", str(b)))
            else:
                bullet_texts.append(str(b))
        extra_lines.append("- 불릿 포인트:\n" + "\n".join(f"  • {t}" for t in bullet_texts))

    # table
    if slide.get("table"):
        table = slide["table"]
        headers = table.get("headers", [])
        rows = table.get("rows", [])
        if headers:
            extra_lines.append(f"- 테이블 헤더: {headers}")
        if rows:
            extra_lines.append(f"- 테이블 행 ({len(rows)}개): {json.dumps(rows[:5], ensure_ascii=False)}")

    # two_column
    if slide.get("left_title") or slide.get("right_title"):
        extra_lines.append(f"- 왼쪽 제목: {slide.get('left_title', '')}")
        extra_lines.append(f"- 왼쪽 내용: {slide.get('left_content', [])}")
        extra_lines.append(f"- 오른쪽 제목: {slide.get('right_title', '')}")
        extra_lines.append(f"- 오른쪽 내용: {slide.get('right_content', [])}")

    # kpis
    if slide.get("kpis"):
        extra_lines.append(f"- KPI: {json.dumps(slide['kpis'], ensure_ascii=False)}")

    return SVG_SLIDE_USER_TEMPLATE.format(
        slide_type=slide_type,
        title=title,
        page_num=page_num,
        extra_data="\n".join(extra_lines) if extra_lines else "(추가 데이터 없음)",
    )


# ═══════════════════════════════════════════
# 에이전트 루프: 생성 → 검증 → 수정
# ═══════════════════════════════════════════

async def generate_slide_svg(
    api_key: str,
    slide: dict,
    page_num: int,
    model: str = "",
) -> str:
    """단일 슬라이드 SVG 생성 + 에이전트 루프 (검증→수정)"""
    user_prompt = _slide_to_prompt_data(slide, page_num)
    response = await _call_llm(api_key, SVG_SYSTEM_PROMPT, user_prompt, model=model)
    svg = _extract_svg(response)

    if not svg:
        logger.error(f"슬라이드 {page_num} SVG 추출 실패")
        return _fallback_svg(slide, page_num)

    # 에이전트 루프: 검증 → 수정 (최대 MAX_FIX_RETRIES회)
    for attempt in range(MAX_FIX_RETRIES):
        issues = validate_svg(svg)
        if not issues:
            return svg

        logger.warning(f"슬라이드 {page_num} 검증 실패 (시도 {attempt+1}): {issues}")
        fix_prompt = SVG_FIX_USER_TEMPLATE.format(
            issues="\n".join(f"- {i}" for i in issues),
            svg_content=svg,
        )
        fix_response = await _call_llm(api_key, SVG_SYSTEM_PROMPT, fix_prompt, model=model)
        fixed_svg = _extract_svg(fix_response)
        if fixed_svg:
            svg = fixed_svg
        else:
            break

    # 최종 검증
    final_issues = validate_svg(svg)
    if final_issues:
        logger.warning(f"슬라이드 {page_num} 최종 검증 실패 — 폴백 사용: {final_issues}")
        return _fallback_svg(slide, page_num)

    return svg


def _fallback_svg(slide: dict, page_num: int) -> str:
    """SVG 생성 실패 시 안전한 폴백 SVG"""
    title = slide.get("title", f"슬라이드 {page_num}")
    slide_type = slide.get("slide_type", "content")
    # XML-escape
    title = title.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

    bullets = slide.get("bullets", [])
    bullet_texts = []
    for b in bullets[:6]:
        text = b.get("text", str(b)) if isinstance(b, dict) else str(b)
        text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        bullet_texts.append(text)

    key_msg = slide.get("key_message", "")
    if key_msg:
        key_msg = key_msg.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    if slide_type == "cover":
        return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">
  <rect width="1280" height="720" fill="#FFFFFF"/>
  <rect width="1280" height="80" fill="#01696F"/>
  <text x="640" y="300" text-anchor="middle" font-family="맑은 고딕, Malgun Gothic, sans-serif" font-size="28" fill="#1E293B" font-weight="bold">{title}</text>
  <text x="640" y="400" text-anchor="middle" font-family="맑은 고딕, Malgun Gothic, sans-serif" font-size="14" fill="#64748B">{page_num}</text>
</svg>'''

    if slide_type in ("section_divider",):
        return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">
  <rect width="1280" height="720" fill="#FFFFFF"/>
  <rect x="60" y="280" width="6" height="160" fill="#01696F"/>
  <text x="90" y="340" font-family="맑은 고딕, Malgun Gothic, sans-serif" font-size="48" fill="#01696F" font-weight="bold">{page_num:02d}</text>
  <text x="90" y="400" font-family="맑은 고딕, Malgun Gothic, sans-serif" font-size="28" fill="#1E293B" font-weight="bold">{title}</text>
  <text x="640" y="700" text-anchor="middle" font-family="맑은 고딕, Malgun Gothic, sans-serif" font-size="12" fill="#64748B">{page_num}</text>
</svg>'''

    if slide_type in ("key_message", "teaser", "closing"):
        return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">
  <rect width="1280" height="720" fill="#FFFFFF"/>
  <text x="640" y="330" text-anchor="middle" font-family="맑은 고딕, Malgun Gothic, sans-serif" font-size="24" fill="#1E293B" font-weight="bold">{title}</text>
  <rect x="560" y="360" width="160" height="3" fill="#01696F"/>
  <text x="640" y="700" text-anchor="middle" font-family="맑은 고딕, Malgun Gothic, sans-serif" font-size="12" fill="#64748B">{page_num}</text>
</svg>'''

    # 일반 콘텐츠 슬라이드
    bullet_svg = ""
    y = 180 if key_msg else 120
    if key_msg:
        bullet_svg += f'  <rect x="40" y="80" width="4" height="50" fill="#01696F"/>\n'
        bullet_svg += f'  <rect x="40" y="80" width="1200" height="50" rx="4" fill="#F8FAFC"/>\n'
        bullet_svg += f'  <text x="60" y="112" font-family="맑은 고딕, Malgun Gothic, sans-serif" font-size="16" fill="#1E293B" font-weight="bold">{key_msg}</text>\n'

    for i, bt in enumerate(bullet_texts):
        bullet_svg += f'  <text x="80" y="{y + i * 45}" font-family="맑은 고딕, Malgun Gothic, sans-serif" font-size="16" fill="#1E293B">• {bt}</text>\n'

    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">
  <rect width="1280" height="720" fill="#FFFFFF"/>
  <rect width="1280" height="63" fill="#01696F"/>
  <text x="60" y="40" font-family="맑은 고딕, Malgun Gothic, sans-serif" font-size="20" fill="#FFFFFF" font-weight="bold">{title}</text>
  <text x="1220" y="40" text-anchor="end" font-family="맑은 고딕, Malgun Gothic, sans-serif" font-size="14" fill="#CBD5E1">{page_num}</text>
{bullet_svg}  <text x="640" y="700" text-anchor="middle" font-family="맑은 고딕, Malgun Gothic, sans-serif" font-size="12" fill="#64748B">{page_num}</text>
</svg>'''


# ═══════════════════════════════════════════
# Phase별 병렬 SVG 생성
# ═══════════════════════════════════════════

def _flatten_slides(content: dict) -> List[Tuple[dict, str]]:
    """ProposalContent dict에서 전체 슬라이드 목록 추출 — (slide_dict, context_label)"""
    slides = []

    # Phase 0: Teaser
    teaser = content.get("teaser", {})
    if teaser:
        # cover slide
        slides.append(({
            "slide_type": "cover",
            "title": content.get("project_name", ""),
            "subtitle": content.get("slogan", ""),
            "key_message": content.get("client_name", ""),
            "bullets": [
                content.get("company_name", ""),
                content.get("submission_date", ""),
            ],
        }, "cover"))

        for s in teaser.get("slides", []):
            if isinstance(s, dict) and s.get("slide_type") != "title":
                slides.append((s, "teaser"))

    # Phase 1~7
    for phase in content.get("phases", []):
        pnum = phase.get("phase_number", 0)
        ptitle = phase.get("phase_title", f"Phase {pnum}")

        # section divider
        slides.append(({
            "slide_type": "section_divider",
            "title": f"Phase {pnum}. {ptitle}",
            "subtitle": phase.get("phase_subtitle", ""),
        }, f"phase_{pnum}_divider"))

        for s in phase.get("slides", []):
            if isinstance(s, dict):
                slides.append((s, f"phase_{pnum}"))

    # closing
    next_step = content.get("next_step")
    if next_step:
        slides.append(({
            "slide_type": "content",
            "title": "Next Step",
            "bullets": [
                step.get("title", "") if isinstance(step, dict) else str(step)
                for step in (next_step.get("steps", []) or [])
            ],
        }, "next_step"))

    # 감사합니다
    slides.append(({
        "slide_type": "closing",
        "title": "감사합니다",
        "subtitle": content.get("project_name", ""),
    }, "closing"))

    return slides[:MAX_SLIDES]


async def generate_svg_pptx(
    content_json: Dict[str, Any],
    output_path: Path,
    api_key: str,
    progress_callback: Optional[Callable] = None,
    model: str = "",
) -> Path:
    """
    전체 SVG 기반 PPTX 생성 파이프라인

    1. content → 슬라이드 목록 추출
    2. 슬라이드별 SVG 병렬 생성 (에이전트 루프 포함)
    3. SVG → PPTX 변환 (create_pptx_with_native_svg)
    """
    import os
    if not model:
        model = os.environ.get("LLM_MODEL", "anthropic/claude-sonnet-4-6")
    if not api_key:
        raise ValueError("API 키가 설정되지 않음")

    t0 = time.time()
    slides = _flatten_slides(content_json)
    total = len(slides)
    logger.info(f"SVG 생성 시작: {total}장 슬라이드")

    if progress_callback:
        progress_callback({"phase": "rendering", "message": f"SVG 고급 렌더링: {total}장 생성 중..."})

    # 병렬 SVG 생성 (8개씩 배치)
    BATCH_SIZE = 8
    svg_results: List[str] = []

    for batch_start in range(0, total, BATCH_SIZE):
        batch = slides[batch_start:batch_start + BATCH_SIZE]
        tasks = []
        for i, (slide_data, label) in enumerate(batch):
            page_num = batch_start + i + 1
            tasks.append(generate_slide_svg(api_key, slide_data, page_num, model=model))

        batch_results = await asyncio.gather(*tasks, return_exceptions=True)
        for j, r in enumerate(batch_results):
            page = batch_start + j + 1
            if isinstance(r, Exception):
                logger.error(f"슬라이드 {page} SVG 생성 실패: {r}")
                svg_results.append(_fallback_svg(slides[batch_start + j][0], page))
            else:
                svg_results.append(r)

        if progress_callback:
            done = min(batch_start + BATCH_SIZE, total)
            pct = int(done / total * 100)
            progress_callback({"phase": "rendering", "message": f"SVG 생성: {done}/{total}장 완료 ({pct}%)"})

    elapsed = time.time() - t0
    logger.info(f"SVG 생성 완료: {total}장, {elapsed:.1f}s")

    # SVG 파일을 임시 디렉토리에 저장
    svg_dir = Path(tempfile.mkdtemp(prefix="svg_slides_"))
    svg_files = []
    for i, svg_content in enumerate(svg_results):
        svg_path = svg_dir / f"slide_{i+1:03d}.svg"
        svg_path.write_text(svg_content, encoding="utf-8")
        svg_files.append(svg_path)

    logger.info(f"SVG 파일 저장 완료: {svg_dir}")

    # SVG → PPTX 변환
    if progress_callback:
        progress_callback({"phase": "rendering", "message": "SVG → PPTX 변환 중 (DrawingML 네이티브)..."})

    from .svg_to_pptx import create_pptx_with_native_svg

    success = create_pptx_with_native_svg(
        svg_files=svg_files,
        output_path=output_path,
        canvas_format="ppt169",
        verbose=False,
        transition="fade",
        transition_duration=0.3,
        use_native_shapes=True,
    )

    if not success:
        logger.warning("일부 SVG → PPTX 변환 실패, 비네이티브 모드 재시도")
        success = create_pptx_with_native_svg(
            svg_files=svg_files,
            output_path=output_path,
            canvas_format="ppt169",
            verbose=False,
            use_native_shapes=False,
        )

    # 임시 디렉토리 정리
    import shutil
    shutil.rmtree(svg_dir, ignore_errors=True)

    total_elapsed = time.time() - t0
    logger.info(f"SVG PPTX 생성 완료: {output_path} ({total_elapsed:.1f}s)")

    if progress_callback:
        progress_callback({"phase": "rendering", "message": f"SVG 고급 렌더링 완료 ({total}장, {total_elapsed:.0f}s)"})

    return output_path
