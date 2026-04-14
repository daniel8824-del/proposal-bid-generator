#!/usr/bin/env python3
"""
SVG → PPTX 변환 러너 — Claude Code가 생성한 SVG 파일들을 DrawingML 네이티브 PPTX로 변환

사용법:
  # 특정 프로젝트의 svgs/ 디렉토리 → PPTX
  python3 convert_svgs.py output/경기도농수산진흥원_SNS운영

  # SVG 디렉토리와 출력 경로 직접 지정
  python3 convert_svgs.py output/경기도농수산진흥원_SNS운영/svgs output/경기도농수산진흥원_SNS운영/제안서_svg.pptx
"""

import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.resolve()
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("convert_svgs")


def validate_all_svgs(svg_dir: Path) -> dict:
    """SVG 파일들의 유효성 검증"""
    from src.generators.svg_generator import validate_svg

    svg_files = sorted(svg_dir.glob("*.svg"))
    results = {"total": len(svg_files), "valid": 0, "issues": []}

    for svg_path in svg_files:
        content = svg_path.read_text(encoding="utf-8")
        issues = validate_svg(content)
        if issues:
            results["issues"].append((svg_path.name, issues))
        else:
            results["valid"] += 1

    return results


def main():
    if len(sys.argv) < 2:
        print("사용법: python3 convert_svgs.py <프로젝트_디렉토리>")
        print("        python3 convert_svgs.py <svgs_디렉토리> <출력.pptx>")
        sys.exit(1)

    arg1 = Path(sys.argv[1])

    # 인자 해석: 프로젝트 디렉토리 or SVG 디렉토리
    if len(sys.argv) >= 3:
        svg_dir = arg1
        output_pptx = Path(sys.argv[2])
    elif arg1.is_dir() and (arg1 / "svgs").is_dir():
        svg_dir = arg1 / "svgs"
        output_pptx = arg1 / "제안서_svg.pptx"
    elif arg1.is_dir():
        svg_dir = arg1
        output_pptx = arg1.parent / "제안서_svg.pptx"
    else:
        logger.error(f"디렉토리 없음: {arg1}")
        sys.exit(1)

    # SVG 파일 수집
    svg_files = sorted(svg_dir.glob("*.svg"))
    if not svg_files:
        logger.error(f"SVG 파일 없음: {svg_dir}")
        sys.exit(1)

    logger.info(f"SVG 디렉토리: {svg_dir}")
    logger.info(f"SVG 파일: {len(svg_files)}개")
    logger.info(f"출력: {output_pptx}")

    # 1. SVG 검증
    logger.info("SVG 검증 중...")
    results = validate_all_svgs(svg_dir)
    logger.info(f"  유효: {results['valid']}/{results['total']}")

    if results["issues"]:
        logger.warning(f"  문제: {len(results['issues'])}개 파일")
        for name, issues in results["issues"][:5]:
            logger.warning(f"    {name}: {issues[0]}")
        if len(results["issues"]) > 5:
            logger.warning(f"    ... 외 {len(results['issues']) - 5}개")

    # 2. SVG → PPTX 변환
    logger.info("SVG → PPTX 변환 중 (DrawingML 네이티브)...")

    from src.generators.svg_to_pptx import create_pptx_with_native_svg

    success = create_pptx_with_native_svg(
        svg_files=svg_files,
        output_path=output_pptx,
        canvas_format="ppt169",
        verbose=True,
        transition="fade",
        transition_duration=0.3,
        use_native_shapes=True,
    )

    if not success:
        logger.warning("네이티브 모드 실패, 호환 모드로 재시도...")
        success = create_pptx_with_native_svg(
            svg_files=svg_files,
            output_path=output_pptx,
            canvas_format="ppt169",
            verbose=True,
            use_native_shapes=False,
        )

    if not success:
        logger.error("PPTX 변환 실패")
        sys.exit(1)

    # 3. PPTX 검증
    try:
        from pptx import Presentation
        prs = Presentation(str(output_pptx))
        slide_count = len(prs.slides)
        file_size = output_pptx.stat().st_size
        logger.info(f"✅ PPTX 생성 완료: {output_pptx}")
        logger.info(f"   슬라이드: {slide_count}장, 크기: {file_size:,} bytes")
    except Exception as e:
        logger.error(f"❌ PPTX 검증 실패: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
