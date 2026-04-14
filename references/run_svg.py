#!/usr/bin/env python3
"""
SVG 고급 렌더링 러너 — proposal_content.json → SVG (Sonnet 4.6) → PPTX

사용법:
  # OPENROUTER_API_KEY 필요
  export OPENROUTER_API_KEY="sk-or-..."

  # 기본 실행 (output 내 최신 프로젝트)
  python3 run_svg.py

  # 특정 프로젝트
  python3 run_svg.py output/경기도농수산진흥원_SNS운영/proposal_content.json

  # 모델 변경
  LLM_MODEL=anthropic/claude-sonnet-4-6 python3 run_svg.py
"""

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

# 프로젝트 루트 설정
PROJECT_ROOT = Path(__file__).parent.resolve()
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("run_svg")


def find_latest_content_json() -> Path:
    """output/ 내 가장 최근 proposal_content.json 찾기"""
    output_dir = PROJECT_ROOT / "output"
    candidates = list(output_dir.rglob("proposal_content*.json"))
    if not candidates:
        raise FileNotFoundError(f"output/ 내 proposal_content.json 없음")
    # 수정일 기준 최신
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0]


def validate_content_density(content: dict) -> dict:
    """콘텐츠 밀도 검증 — 하한선 미달 시 경고"""
    issues = []

    # 슬라이드 수 집계
    total_slides = 0
    total_chars = 0
    phase_counts = {}

    # teaser (Phase 0)
    teaser = content.get("teaser", {})
    teaser_slides = teaser.get("slides", [])
    total_slides += len(teaser_slides)
    teaser_chars = sum(len(json.dumps(s, ensure_ascii=False)) for s in teaser_slides)
    total_chars += teaser_chars
    phase_counts[0] = len(teaser_slides)

    # Phases 1-7
    for phase in content.get("phases", []):
        pn = phase.get("phase_number", 0)
        slides = phase.get("slides", [])
        total_slides += len(slides)
        pc = sum(len(json.dumps(s, ensure_ascii=False)) for s in slides)
        total_chars += pc
        phase_counts[pn] = len(slides)

    avg_chars = total_chars // max(total_slides, 1)

    # 밀도 체크 (Marketing/PR 기준)
    MIN_SLIDES = {0: 6, 1: 4, 2: 10, 3: 10, 4: 35, 5: 6, 6: 8, 7: 4}

    for pn, min_count in MIN_SLIDES.items():
        actual = phase_counts.get(pn, 0)
        if actual < min_count:
            issues.append(f"Phase {pn}: {actual}장 (최소 {min_count}장 필요)")

    if total_slides < 83:
        issues.append(f"총 {total_slides}장 (최소 83장 필요)")

    if avg_chars < 500:
        issues.append(f"평균 {avg_chars}자/장 (최소 500자 필요)")

    return {
        "total_slides": total_slides,
        "total_chars": total_chars,
        "avg_chars": avg_chars,
        "phase_counts": phase_counts,
        "issues": issues,
        "pass": len(issues) == 0,
    }


async def main():
    # 1. 콘텐츠 JSON 로드 (API 키보다 먼저 — 밀도 검증을 위해)
    if len(sys.argv) > 1:
        content_path = Path(sys.argv[1])
    else:
        content_path = find_latest_content_json()

    if not content_path.exists():
        logger.error(f"파일 없음: {content_path}")
        sys.exit(1)

    logger.info(f"콘텐츠 JSON: {content_path}")

    with open(content_path, "r", encoding="utf-8") as f:
        content = json.load(f)

    # 2. 밀도 검증 (API 호출 전에 실행)
    density = validate_content_density(content)
    logger.info(f"슬라이드: {density['total_slides']}장, 평균: {density['avg_chars']}자/장")

    phase_names = {0: "HOOK", 1: "SUMMARY", 2: "INSIGHT", 3: "CONCEPT",
                   4: "ACTION", 5: "MGMT", 6: "WHY US", 7: "INVEST"}
    for pn, count in sorted(density["phase_counts"].items()):
        name = phase_names.get(pn, f"Phase {pn}")
        logger.info(f"  Phase {pn} {name}: {count}장")

    if density["issues"]:
        logger.warning("⚠️  밀도 하한선 미달:")
        for issue in density["issues"]:
            logger.warning(f"  - {issue}")
        logger.warning("계속 진행합니다 (결과물 품질이 낮을 수 있음)")
    else:
        logger.info("✅ 밀도 검증 통과")

    # 3. API 키 확인
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        logger.error("OPENROUTER_API_KEY 환경변수가 설정되지 않았습니다.")
        logger.error("export OPENROUTER_API_KEY='sk-or-...'")
        sys.exit(1)

    model = os.environ.get("LLM_MODEL", "anthropic/claude-sonnet-4-6")
    logger.info(f"모델: {model}")

    # 4. 출력 경로 결정
    output_dir = content_path.parent
    output_pptx = output_dir / "제안서_svg.pptx"

    # 6. SVG 파이프라인 실행
    from src.generators.svg_generator import generate_svg_pptx

    def progress(info):
        logger.info(f"  {info.get('message', '')}")

    logger.info(f"SVG 렌더링 시작 (Sonnet 4.6, 배치 8장씩)...")

    result = await generate_svg_pptx(
        content_json=content,
        output_path=output_pptx,
        api_key=api_key,
        progress_callback=progress,
        model=model,
    )

    # 7. PPTX 검증
    try:
        from pptx import Presentation
        prs = Presentation(str(result))
        slide_count = len(prs.slides)
        file_size = result.stat().st_size
        logger.info(f"✅ PPTX 생성 완료: {result}")
        logger.info(f"   슬라이드: {slide_count}장, 크기: {file_size:,} bytes")
    except Exception as e:
        logger.error(f"❌ PPTX 검증 실패: {e}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
