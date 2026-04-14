"""
생성된 generate_proposal.py를 subprocess로 실행

Claude가 생성한 Python 스크립트를 격리된 서브프로세스에서 실행하고,
PPTX 파일 생성을 검증한다.
"""

import logging
import subprocess
import sys
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger("code_runner")


class CodeRunnerError(Exception):
    """코드 실행 실패"""
    pass


def run_generated_code(
    script_path: Path,
    expected_output: Path,
    timeout: int = 120,
    cwd: Optional[Path] = None,
    env: Optional[Dict[str, str]] = None,
) -> Path:
    """
    Python 스크립트를 subprocess로 실행 → PPTX 파일 반환

    Args:
        script_path: 실행할 Python 스크립트 경로
        expected_output: 스크립트가 생성해야 할 PPTX 파일 경로
        timeout: 실행 타임아웃 (초, default 120)
        cwd: 작업 디렉토리 (default: proposal-bid-app 루트)
        env: 추가 환경변수

    Returns:
        생성된 PPTX 파일 경로

    Raises:
        CodeRunnerError: 스크립트 없음, 실행 실패, 타임아웃, PPTX 미생성
    """
    if not script_path.exists():
        raise CodeRunnerError(f"스크립트 파일 없음: {script_path}")

    # cwd를 프로젝트 루트로 (slide_kit import 경로)
    if cwd is None:
        cwd = Path(__file__).parent.parent.parent  # app/generators → app → root

    logger.info(f"코드 실행 시작: {script_path}")
    logger.info(f"작업 디렉토리: {cwd}")

    # 기존 출력 파일 삭제 (새로 생성 확인용)
    if expected_output.exists():
        expected_output.unlink()
        logger.debug(f"기존 출력 파일 삭제: {expected_output}")

    try:
        result = subprocess.run(
            [sys.executable, str(script_path)],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired:
        raise CodeRunnerError(f"실행 타임아웃 ({timeout}초)")
    except Exception as e:
        raise CodeRunnerError(f"subprocess 실행 실패: {e}")

    # 종료 코드 확인
    if result.returncode != 0:
        err_msg = (
            f"실행 실패 (exit code {result.returncode})\n"
            f"--- stdout ---\n{result.stdout[:1500]}\n"
            f"--- stderr ---\n{result.stderr[:2000]}"
        )
        logger.error(err_msg)
        raise CodeRunnerError(err_msg)

    logger.info(f"코드 실행 완료")
    if result.stdout:
        logger.info(f"stdout: {result.stdout[:500]}")

    # PPTX 파일 생성 검증
    if not expected_output.exists():
        raise CodeRunnerError(
            f"PPTX 파일이 생성되지 않음: {expected_output}\n"
            f"stdout: {result.stdout[:500]}"
        )

    size = expected_output.stat().st_size
    if size < 1024:
        raise CodeRunnerError(f"PPTX 파일이 너무 작음 ({size} bytes) — 손상된 파일")

    logger.info(f"PPTX 생성 확인: {expected_output} ({size:,} bytes)")
    return expected_output


def verify_pptx(pptx_path: Path) -> Dict[str, any]:
    """
    PPTX 파일 검증 — 슬라이드 수, 파일 크기, 읽기 가능 여부

    Returns:
        {"slide_count": int, "file_size": int, "valid": bool}
    """
    try:
        from pptx import Presentation
        prs = Presentation(str(pptx_path))
        return {
            "slide_count": len(prs.slides),
            "file_size": pptx_path.stat().st_size,
            "valid": True,
        }
    except Exception as e:
        return {
            "slide_count": 0,
            "file_size": pptx_path.stat().st_size if pptx_path.exists() else 0,
            "valid": False,
            "error": str(e),
        }
