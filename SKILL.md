---
name: proposal-bid
description: "RFP-driven bid proposal PPTX generator with Impact-8 Framework and SVG→DrawingML pipeline. Triggers: /proposal-bid, 제안서, 입찰, RFP, proposal, bid"
---

# proposal-bid Skill

> **이 스킬은 오케스트레이터다.** 웹앱 `proposal-bid-app`의 파이프라인을 그대로 따르되, OpenRouter API 호출 자리를 Claude Code가 직접 LLM 작업으로 대체한다.
> **재구현 금지 · 요약 금지 · 순서 변경 금지** — `references/`의 웹앱 원본을 **순서대로** 읽고, 그 지시대로 작업한다.

---

## 0. 정체성 · 트리거 · 기본값

| 항목 | 값 |
|------|-----|
| 역할 | 오케스트레이터 (Claude Code = LLM 엔진) |
| 트리거 키워드 | "제안서 만들어줘", "입찰 제안", "RFP 분석", "proposal", "bid", `/proposal-bid` |
| 웹앱 원본 | `/home/daniel/proposal-bid-app` / `https://proposal-bid.up.railway.app` |
| 작업 디렉토리 | `/mnt/c/Users/daniel/Desktop/프레젠테이션/proposal-bid/` |
| 최종 출력 | `output/{프로젝트명}/제안서_svg.pptx` |
| **기본 모드** | **SVG 모드** (품질 우선) |
| 대안 모드 | slide_kit 모드 (속도·안정성 우선 시) |
| 슬라이드 상한 | MAX_SLIDES = 120장 |

### ★ 최우선 원칙 (위반 시 중단)

1. **웹앱 재구현 금지** — `references/`에 원본 존재. 참조만, 재작성 불가.
2. **프롬프트 verbatim** — `SVG_SYSTEM_PROMPT` · Phase 프롬프트 · `content_guidelines`는 **원문 그대로** 사용. 요약·축약·변형 금지.
3. **단계 역행 금지** — STEP 1→2→3→4→5→6 순서 고정. STEP 3 완료 + **사용자 확인** 전 STEP 4 금지.
4. **슬라이드 1장 = 프롬프트 1개 = 검증 1회** — Phase 단위 일괄 생성 금지.
5. **Claude가 LLM** — 새 API 호출 스크립트 작성 금지. 에이전트 위임 시에도 원문 전달.
6. **기계적 작업만 스크립트** — 유일한 공인 스크립트는 `scripts/convert_svgs.py`.

### ⚠ 기존 스크립트 취급

작업 디렉토리(`/mnt/c/.../proposal-bid/`)에 `run_proposal_claude.py`, `run_svg_claude.py`, `run_svg.py`, `main.py` 등 레거시 파일이 존재할 수 있다.
**모두 무시한다.** 이 SKILL.md의 STEP 1~6 절차만 따른다. 레거시 스크립트 실행·수정·삭제 금지(참고용으로만 읽기 허용).

---

## 1. 패키지 구조

```
~/.claude/skills/proposal-bid/
├── SKILL.md                          ← 이 파일 (오케스트레이션 로직)
├── scripts/
│   └── convert_svgs.py               ← 유일한 실행 스크립트 (SVG → PPTX DrawingML)
└── references/                       ← 웹앱 소스 미러 (39 files)
    ├── prompts/                      ← 큐레이션 프롬프트 (verbatim 인용 대상)
    │   ├── content_guidelines.txt    (308줄, 매 Phase 필수)
    │   ├── rfp_analysis.txt          (37줄)
    │   ├── svg_design_system.txt     (99줄)
    │   └── phase0_hook.txt ~ phase7_investment.txt  (Phase별 108~187줄)
    ├── parsers/
    │   ├── pdf_parser.py
    │   └── docx_parser.py
    ├── agents/                       ← 웹앱의 LLM 에이전트 (Claude가 역할 대행)
    │   ├── base_agent.py
    │   ├── rfp_analyzer.py           ← RFP 분석 프롬프트 + 스키마
    │   └── content_generator.py      ← Impact-8 콘텐츠 생성 로직 (32K)
    ├── generators/
    │   ├── svg_generator.py          ← ★ SVG_SYSTEM_PROMPT / validate_svg / _slide_to_prompt_data / _fallback_svg
    │   ├── slide_kit.py              ← slide_kit 모드 API (89K)
    │   ├── code_generator_parallel.py
    │   └── svg_to_pptx/              ← SVG→PPTX 네이티브 변환 내부 로직
    ├── orchestrators/
    │   └── proposal_orchestrator.py  ← 웹앱 전체 파이프라인 정의 (참조용 기준)
    ├── schemas/
    │   ├── rfp_schema.py             ← RFPAnalysis
    │   └── proposal_schema.py        ← ProposalContent
    └── config/
        └── proposal_types.py         ← Phase 가중치 / 유형별 설정
```

---

## 2. 오케스트레이션 파이프라인 (6 STEP 고정)

웹앱 `proposal_orchestrator.py`의 흐름이 정답. 각 STEP의 **진입 조건(Entry) · 읽을 파일(Read) · 작업(Do) · 종료 조건(Exit)** 을 반드시 충족해야 다음 STEP으로 넘어간다.

```
[입력] → STEP 1 파싱 → STEP 2 RFP 분석 → STEP 3 콘텐츠 기획
      → [★ 사용자 확인 게이트] → STEP 4 SVG 생성 → STEP 5 PPTX 변환 → STEP 6 검증
```

---

### STEP 1 — 입력 파싱

**Entry:** 사용자가 제안서 요청 + `input/` 폴더 존재
**Read:**
- `input/**/*.{pdf,docx,md,txt}` — RFP 본문 (복수 파일 합본)
- `input/company/*.{pdf,docx,json,txt}` — 회사소개서 (선택, Phase 6·5에서 사용)
- 파일 형식별 파서 참조: `references/parsers/pdf_parser.py`, `docx_parser.py`

**Do:**
| 파일 | 처리 |
|------|------|
| `.md`, `.txt` | Read 도구로 직접 |
| `.pdf` | Read 도구(PDF 모드) |
| `.docx` | `docx_parser.py` 방식 |

회사소개서가 있으면 **구조화 추출**: 회사 역량 / 수행 실적 / 핵심 인력 / 인증·수상 4개 버킷.

**Exit:** RFP 전문(합본) + 회사 데이터(있으면) 확보.
**Exit 불가 시:** 사용자에게 파일 배치 요청 후 중단.

---

### STEP 2 — RFP 분석 → `rfp_analysis.json`

**Entry:** STEP 1 Exit 완료
**Read (순서 고정):**
1. `references/prompts/rfp_analysis.txt` — 분석 프레임워크
2. `references/agents/rfp_analyzer.py` — 웹앱 프롬프트 + 출력 스키마
3. `references/schemas/rfp_schema.py` — `RFPAnalysis` 필드 정의

**Do:**
1. RFP 전문을 `RFPAnalysis` 스키마에 맞춰 JSON 작성
2. **프로젝트 유형 자동 판별** — `marketing_pr` / `event` / `it_system` / `public` / `consulting` (키워드: 마케팅·홍보·SNS→marketing_pr, 행사·이벤트→event, 시스템·구축→it_system, 공공·입찰→public, 컨설팅·전략→consulting)
3. **평가기준 배점 추출** → `evaluation_criteria[]` (각 항목·배점·공략전략)
4. **Brave Search 최소 4건** — 시장 통계·트렌드·발주처 현황·경쟁사. 각 결과를 `rfp_analysis.json`에 출처와 함께 저장.

**Exit:** `output/{프로젝트명}/rfp_analysis.json` 생성 + 필수 필드(`project_name`, `client_name`, `proposal_type`, `evaluation_criteria`, `pain_points`) 채워짐.
**Exit 불가 시:** RFP에 필수 정보 누락 → 사용자에게 구두 확인 요청 후 재작성.

---

### STEP 2.5 — 배점→비중 매핑 (STEP 2 결과를 STEP 3에 주입)

**Do:** `rfp_analysis.json`의 `evaluation_criteria`를 기반으로 Phase별 슬라이드 가중치를 조정한다.

| 평가항목 배점 | 대응 슬라이드 | 비고 |
|--------------|-------------|------|
| 25% 이상 | 전용 3장 이상 | Phase 내 최다 배분 |
| 15~24% | 전용 2장 이상 | 충분한 분량 |
| 15% 미만 | 1장 커버 | 기본 분량 |

**기본 Phase 가중치** (`references/config/proposal_types.py` 기준):

| # | Phase | Marketing | Event | IT | Public | Consulting | General |
|---|-------|-----------|-------|-----|--------|------------|---------|
| 0 | HOOK | 8% | 6% | 3% | 3% | 5% | 5% |
| 1 | SUMMARY | 5% | 5% | 8% | 8% | 8% | 6% |
| 2 | INSIGHT | 12% | 8% | 12% | 15% | 15% | 10% |
| 3 | CONCEPT | 12% | 10% | 10% | 10% | 12% | 10% |
| 4 | **ACTION** | **40%** | **45%** | **35%** | **30%** | **30%** | **35%** |
| 5 | MGMT | 8% | 10% | 12% | 12% | 10% | 10% |
| 6 | WHY US | 10% | 10% | 12% | 15% | 12% | 12% |
| 7 | INVEST | 5% | 6% | 8% | 7% | 8% | 7% |

배점 정렬 규칙이 기본 가중치와 충돌하면 **평가기준이 우선**한다.

**총 슬라이드 수 가이드:**
Marketing/PR 100~150 · Event 80~120 · IT 60~100 · Public 60~90 · Consulting 50~80

---

### STEP 3 — Impact-8 콘텐츠 기획 → `proposal_content.json`

**Entry:** STEP 2 Exit + STEP 2.5 가중치 확정
**Read (매 Phase 시작 전 필수):**
1. `references/prompts/content_guidelines.txt` (308줄) — Action Title / Win Theme / C-E-I / KPI 산출근거 / 플레이스홀더 규칙
2. `references/prompts/phase{N}_*.txt` — 해당 Phase 상세
3. `references/agents/content_generator.py` — Phase 브릿지·Win Theme 반복 로직
4. `references/schemas/proposal_schema.py` — `ProposalContent` 스키마

**★ Phase별 순차 생성 + Context Chaining (웹앱 개선)**

웹앱은 RFP 분석 + Win Theme만 전달하지만, **스킬은 이전 Phase 핵심을 다음 Phase에 누적 주입**한다:
- Phase 0~1 생성 후 → **Win Theme 3개 + 핵심 KPI 수치** 추출
- Phase 2~3 생성 시 → 위 + **시장 데이터·벤치마크 수치** 추출
- Phase 4~7 생성 시 → 위 + **전략 프레임워크명·채널별 목표 수치** 추출
- 각 Phase 프롬프트에 `"## 이전 Phase 핵심 (일관성 유지)"` 블록으로 누적 context 주입

이를 통해 후반부 Phase에서도 앞서 확정된 수치·용어·Win Theme이 정확히 반복된다.
에이전트에 위임할 경우에도 **누적 context 전문을 반드시 함께 전달**한다.

**Phase 작성 순서 (0→7, 역행 금지):**

| # | Phase | 최소 슬라이드 (100장 기준) | 필수 산출물 |
|---|-------|---------------------------|------------|
| 0 | HOOK | 6 | teaser 3+, key_message, cover |
| 1 | SUMMARY | 4 | **★ Win Theme 3개 정의**, Exec Summary, ROI 요약 |
| 2 | INSIGHT | 10 | 트렌드 3+, 벤치마크, RFP 대응표, As-Is/To-Be |
| 3 | CONCEPT | 10 | concept_reveal, 프레임워크, 차별화 비교 |
| 4 | **ACTION** | **35** | **전체의 30~45%**, 로드맵, 채널별·캠페인·콘텐츠 예시 |
| 5 | MGMT | 6 | 조직도, 검수 프로세스, 리포팅, 위기대응 |
| 6 | WHY US | 8 | **회사소개서 실적 반영**, Win Theme 증거 매핑, 케이스 스터디 3건 |
| 7 | INVEST | 4 | 비용 총괄, **KPI + 산출근거**, Next Step, closing |

**Phase 6 (WHY US) 특별 지침:**
- 회사소개서가 있으면 실제 실적·수치로 구성
- 회사소개서가 없으면 `[회사명]·[실적 수치]` 플레이스홀더 + 사용자에게 보강 요청 명시
- Win Theme 3개 각각에 **증거 1건 이상** 매핑

**밀도 하한선 (JSON 기준, 위반 시 재작성):**
- 평균 500자/장 (목표 700자+)
- bullets 4개/장 이상 (1개 50자+)
- table 5행 이상
- 모든 데이터에 `data_source` 필수

**★★★ 사용자 확인 게이트 (STOP)**

`proposal_content.json` 작성 완료 후 **반드시 정지**하고 사용자에게:
```
proposal_content.json 기획 완료 ({N}장, Phase별 분포: 0={a}, 1={b}, ...).
Win Theme 3개: {theme1} / {theme2} / {theme3}.
검토 후 "진행" 또는 수정사항을 알려주세요.
```
사용자 승인 없이 STEP 4 진입 금지.

**Exit:** `output/{프로젝트명}/proposal_content.json` + 사용자 승인.

---

### STEP 4 — SVG 생성 (슬라이드 1장씩)

**Entry:** STEP 3 사용자 승인 완료
**Read (verbatim 사용 대상):**
1. `references/generators/svg_generator.py` — **원문 그대로 사용**:
   - `SVG_SYSTEM_PROMPT` (요약 금지)
   - `_slide_to_prompt_data(slide)` (슬라이드 → 프롬프트 규칙)
   - `validate_svg(svg)` (XML 파싱 + viewBox + 금지 태그)
   - `_fallback_svg(slide)` (최종 실패 시)
   - `BATCH_SIZE = 8`, viewBox `0 0 1280 720` (웹앱 원본 규격)
2. `references/prompts/svg_design_system.txt` — 디자인 토큰

**Claude의 루프 (슬라이드 1장씩, 엄격 준수):**
```
for idx, slide in enumerate(proposal_content.slides):
    prompt = _slide_to_prompt_data(slide)           # 웹앱 규칙 그대로
    svg    = <Claude가 SVG_SYSTEM_PROMPT(원문) + prompt로 직접 생성>
    attempts = 0
    while not validate_svg(svg) and attempts < 2:
        svg = <검증 실패 메시지를 포함한 수정 요청>
        attempts += 1
    if not validate_svg(svg):
        svg = _fallback_svg(slide)                  # 최종 폴백
    save → output/{프로젝트명}/svgs/slide_{idx:03d}.svg
```

**병렬화:** 8장 단위로 에이전트 디스패치 가능. **단**, 에이전트에 전달할 때도 `SVG_SYSTEM_PROMPT` **원문**과 `_slide_to_prompt_data` 결과를 그대로 건넨다.

**절대 금지:**
- ❌ `SVG_SYSTEM_PROMPT` 요약·축약·변형
- ❌ Phase 단위 일괄 생성
- ❌ `validate_svg` 우회
- ❌ 에이전트에 자연어 위임 ("이런 느낌으로 그려줘")
- ❌ 폴백을 건너뛰고 실패 슬라이드 방치

**Exit:** `output/{프로젝트명}/svgs/slide_000.svg` ~ `slide_{N-1}.svg` 전량 존재 + 각 파일 `validate_svg == True` 또는 폴백 적용됨.

---

### STEP 5 — PPTX 변환 (유일한 스크립트)

**Entry:** STEP 4 Exit
**Do:**
```bash
cd /mnt/c/Users/daniel/Desktop/프레젠테이션/proposal-bid
python3 ~/.claude/skills/proposal-bid/scripts/convert_svgs.py output/{프로젝트명}
```
내부: `create_pptx_with_native_svg()` — SVG → DrawingML 네이티브 (래스터화 없음).

**Exit:** `output/{프로젝트명}/제안서_svg.pptx` 생성.
**실패 시:** 스크립트 에러 로그 사용자에게 전달 + STEP 4 개별 SVG 재검증.

---

### STEP 6 — 검증 · 품질 게이트

**Do (기계 검증):**
```bash
python3 -c "from pptx import Presentation; p=Presentation('output/{프로젝트명}/제안서_svg.pptx'); print(f'{len(p.slides)} slides OK')"
python3 -m markitdown output/{프로젝트명}/제안서_svg.pptx | grep -iE "xxxx|lorem|TODO|OOO"
```

**체크리스트 (모두 ✅ 되어야 완료):**
- [ ] 슬라이드 수 유형별 범위 내 (§ STEP 2.5 참조)
- [ ] Phase 0~7 모두 포함
- [ ] Phase 4 = 전체의 30~45%
- [ ] Win Theme 3개가 전체 일관 반복
- [ ] 모든 제목이 Action Title (Topic Title 0건)
- [ ] KPI 산출근거 명시 (모호 표현 0건)
- [ ] 플레이스홀더는 `[대괄호]`만 (OOO·XXX·___ 0건)
- [ ] 빈 슬라이드 0건, 평균 500자/장 이상
- [ ] 폴백 SVG 적용 슬라이드 5% 이하 (초과 시 STEP 4 재시도 권장)

**Exit:** 체크리스트 전부 ✅ + 사용자에게 최종 파일 경로 전달.

---

## 3. 대안 모드 — slide_kit (속도·안정성 우선)

SVG 품질이 불필요하거나 120장+ 대형 제안서 안정성이 중요할 때만 선택.

**파이프라인:** STEP 1~3 동일 → STEP 4 대신 `references/generators/slide_kit.py` API로 Python 스크립트 작성 → 실행.

**슬라이드 kit 엄수 원칙:**
- ❌ `slide_kit` 함수 재정의 금지
- ❌ `RGBColor` 하드코딩 금지 → `C["primary"]` 등 상수 사용
- ❌ Pretendard 외 폰트 사용 금지

API 시그니처·레이아웃·컬러 토큰은 `references/generators/slide_kit.py` (89K) 직접 참조. 여기 재기재하지 않는다.

**모드 선택 기준:**

| 상황 | 권장 |
|------|------|
| 디자인 최우선, 고객 프레젠테이션 | **SVG (기본)** |
| 사용자가 "고급 모드"/"SVG" 명시 | **SVG (기본)** |
| 100장+, 안정성 최우선 | slide_kit |
| 빠른 내부 검토용 | slide_kit |
| 사용자가 "기본 모드"/"빠르게" 명시 | slide_kit |

---

## 4. Failure Modes · 대처

| 상황 | 대처 |
|------|------|
| RFP 필수 정보 누락 (예산·기간·유형) | 사용자에게 구두 확인, 받은 정보로 `rfp_analysis.json` 보강 |
| 회사소개서 없음 | Phase 5·6에 `[회사명]·[PM 성명]·[실적 수치]` 플레이스홀더 + 보강 요청 명시 |
| Brave Search 결과 부족 | 쿼리 구체화 (연도+수치+업계) 후 재시도, 그래도 부족하면 `data_source: "산업평균 추정"` 명시 |
| `validate_svg` 2회 연속 실패 | `_fallback_svg` 적용, 플레이스홀더 슬라이드 표시, STEP 6에서 비율 확인 |
| Phase 4가 30% 미만 | STEP 3 Phase 4 재작성 — 채널별·캠페인·콘텐츠 예시 슬라이드 추가 |
| 평균 자수 500 미만 | bullets·table 행 추가, data_source 보강, 재검증 |
| PPTX 변환 실패 | 개별 SVG `validate_svg` 재실행 → 실패 파일만 STEP 4 재생성 |
| 레거시 스크립트 실행 유혹 | **무시**. 이 SKILL.md 절차만 따른다. |

---

## 5. 초기 셋업 (프로젝트 디렉토리 없을 때)

```bash
cd /mnt/c/Users/daniel/Desktop/프레젠테이션/
git clone https://github.com/steveaimkt/proposal-agent-github.git proposal-bid
cd proposal-bid && pip install -r requirements.txt python-pptx pydantic "markitdown[pptx]"
```

---

## 6. End-to-End 최소 실행 예시

```
사용자: "경기도농수산진흥원 SNS 운영 제안서 만들어줘. input/에 RFP PDF 있어."
  ↓
Claude STEP 1: input/ 스캔 → PDF 읽음, input/company/ 없음 확인
Claude STEP 2: references/prompts/rfp_analysis.txt 읽음 → rfp_analysis.json 생성
               proposal_type = marketing_pr, 평가기준 4개 추출, Brave 4건 검색
Claude STEP 2.5: 배점→비중 매핑. 총 100장 타겟, Phase 4 = 40장
Claude STEP 3: content_guidelines.txt + phase0~7 순차 읽음 → proposal_content.json
               Win Theme 3개 정의, 100장 작성, 평균 720자/장
               → [사용자에게 정지 + 검토 요청]
사용자: "진행"
  ↓
Claude STEP 4: svg_generator.py import → SVG_SYSTEM_PROMPT 원문 사용
               100장 × 검증 루프, 3장 폴백 적용
Claude STEP 5: convert_svgs.py 실행 → 제안서_svg.pptx
Claude STEP 6: 체크리스트 9개 모두 ✅ → 사용자에게 경로 전달
```

---

## 7. 절대 원칙 요약 (§0 재확인)

1. 웹앱 재구현 금지 — `references/` 원본 사용
2. 프롬프트 verbatim — 요약·축약·변형 금지
3. STEP 1→2→2.5→3→4→5→6 순서 고정, 사용자 확인 게이트 준수
4. 슬라이드 1장 = 프롬프트 1개 = 검증 1회
5. Claude가 LLM — 새 API 스크립트 작성 금지
6. 기계적 작업만 스크립트 — `scripts/convert_svgs.py` 하나뿐
7. 레거시 `run_*.py` 무시
