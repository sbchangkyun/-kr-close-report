import os
import re
import json
from datetime import datetime, timezone, timedelta

import google.generativeai as genai

# --- Timezone ---
KST = timezone(timedelta(hours=9))


def kst_today() -> str:
    return datetime.now(tz=KST).strftime("%Y-%m-%d")


def _strip_code_fences(text: str) -> str:
    # Gemini가 ```json ... ``` 형태로 감싸는 경우가 있어 제거
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _extract_json(text: str) -> dict:
    """
    Gemini가 JSON만 출력하라 지시를 가끔 어기는 경우를 대비해,
    본문에서 가장 그럴듯한 JSON 객체를 찾아 파싱합니다.
    """
    text = _strip_code_fences(text)

    # 1) 바로 파싱 시도
    try:
        return json.loads(text)
    except Exception:
        pass

    # 2) 본문에서 JSON 객체 후보 탐색
    # 가장 바깥 { ... } 를 잡아본다
    candidates = re.findall(r"\{(?:[^{}]|(?R))*\}", text, flags=re.DOTALL)
    if candidates:
        # 가장 긴 후보가 실제 JSON일 확률이 높음
        candidates.sort(key=len, reverse=True)
        for c in candidates:
            try:
                return json.loads(c)
            except Exception:
                continue

    raise ValueError("Gemini 응답에서 JSON을 추출하지 못했습니다.")


# -----------------------------
# Gemini commentary generation
# -----------------------------
def _normalize_model_name(name: str) -> str:
    """
    GitHub Actions env에서 들어오는 모델명을 안전한 형태로 정규화합니다.
    - gemini-1.5-flash  -> gemini-1.5-flash-latest
    - gemini-1.5-pro    -> gemini-1.5-pro-latest
    - models/ 접두어는 제거 (google-generativeai는 보통 접두어 없이 사용)
    """
    if not name:
        return ""
    name = name.strip()
    if name.startswith("models/"):
        name = name[len("models/"):]
    # 자주 실수하는 케이스 보정
    if name == "gemini-1.5-flash":
        return "gemini-1.5-flash-latest"
    if name == "gemini-1.5-pro":
        return "gemini-1.5-pro-latest"
    return name


def _build_prompt(date_str: str) -> str:
    """
    index.html 템플릿에 꽂아 넣을 '문장/코멘트'만 생성하도록 지시합니다.
    (숫자/데이터 수집은 나중에 붙이기 쉽도록, 지금은 텍스트만)
    """
    return f"""
당신은 한국 주식/환율 마감 코멘터리 작성자입니다.
오늘 날짜는 {date_str} (KST) 입니다.

아래 JSON 스키마를 **정확히** 지켜서, **JSON만** 출력하세요.
- Markdown/코드블록/설명문/주석 금지
- 키 추가/삭제 금지
- 모든 값은 문자열(string)
- 너무 길게 쓰지 말고, 모바일에서 한 번에 읽히도록 **짧고 직관적**으로 작성
- 불확실하면 단정 대신 가능성 표현(예: "~흐름", "~가능성") 사용

[작성 톤/규칙]
- 코스피/코스닥은 각각 한 문장(최대 50자 내외)
- '주요원인:'은 1문장(최대 60자 내외)
- 'dxy_driver', 'us_rate_driver', 'flow_driver', 'trade_driver'는 각 1문장(최대 55자 내외)
- 해외/국내 이벤트는 **짧은 구문**(최대 35자 내외) + 필요시 괄호 1회
- 이모지는 score_comment에만 사용(🟢🟡🔴 중 1개)
- 다른 필드는 이모지 사용 금지

[score_comment 형식(중요)]
- 반드시 아래 형태로 1줄:
  "달러 매수 포인트 XX/100 🟢|🟡|🔴 - (행동가이드 한 문장)"
- XX는 0~100 정수
- 신호등은: 🟢(80~100), 🟡(40~79), 🔴(0~39)

[출력 JSON 스키마]
{{
  "kospi_driver": "...",
  "kosdaq_driver": "...",
  "kospi_flow_comment": "...",
  "kosdaq_flow_comment": "...",
  "fx_driver": "...",
  "score_comment": "...",
  "dxy_driver": "...",
  "us_rate_driver": "...",
  "flow_driver": "...",
  "trade_driver": "...",
  "overseas1": "...",
  "overseas2": "...",
  "domestic1": "...",
  "domestic2": "..."
}}

지금 바로 JSON을 출력하세요.
""".strip()


def gemini_generate_commentary(date_str: str) -> dict:
    """
    Gemini로 코멘트 JSON을 생성합니다.
    - 모델은 env(GEMINI_MODEL)로 강제 가능
    - 기본은 gemini-1.5-flash-latest
    - 간혹 특정 모델이 404(NotFound) 나는 환경이 있어, 후보를 순차 시도합니다.
    """
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("환경변수 GEMINI_API_KEY가 없습니다. GitHub Secrets 설정을 확인해 주세요.")

    genai.configure(api_key=api_key)

    # 1) 모델 후보 준비 (env 우선 + 안전 후보)
    preferred = _normalize_model_name(os.environ.get("GEMINI_MODEL", ""))
    if not preferred:
        preferred = "gemini-1.5-flash-latest"

    # ⚠️ 'gemini-1.5-flash' (non-latest)는 일부 환경에서 404가 나므로 후보에서 제외
    candidates = [
        preferred,
        "gemini-1.5-flash-latest",
        "gemini-1.5-pro-latest",
        "gemini-1.0-pro",
        "gemini-pro",
    ]
    # 중복 제거(순서 유지)
    seen = set()
    model_candidates = []
    for n in candidates:
        n = _normalize_model_name(n)
        if n and n not in seen:
            seen.add(n)
            model_candidates.append(n)

    # 2) 가능하면 list_models로 실제 존재 모델만 남기기 (안 되면 그냥 후보대로 시도)
    try:
        available = set()
        for m in genai.list_models():
            # m.name 예: "models/gemini-1.5-flash-latest"
            name = getattr(m, "name", "") or ""
            if name.startswith("models/"):
                name = name[len("models/"):]
            available.add(name)
        filtered = [m for m in model_candidates if m in available]
        if filtered:
            model_candidates = filtered
    except Exception:
        pass

    prompt = _build_prompt(date_str)

    last_err = None
    for model_name in model_candidates:
        try:
            print(f"[gemini] using model: {model_name}")
            model = genai.GenerativeModel(model_name=model_name)
            resp = model.generate_content(prompt)
            text = getattr(resp, "text", "") or ""
            data = _extract_json(text)

            # 필수 키가 모두 있는지 최소 검증
            required_keys = [
                "kospi_driver", "kosdaq_driver",
                "kospi_flow_comment", "kosdaq_flow_comment",
                "fx_driver", "score_comment",
                "dxy_driver", "us_rate_driver",
                "flow_driver", "trade_driver",
                "overseas1", "overseas2", "domestic1", "domestic2",
            ]
            missing = [k for k in required_keys if k not in data]
            if missing:
                raise ValueError(f"Gemini JSON에 필수 키가 누락되었습니다: {missing}")

            return data
        except Exception as e:
            last_err = e
            print(f"[gemini] model failed: {model_name} -> {type(e).__name__}: {e}")

    raise RuntimeError(f"Gemini 호출 실패: {last_err}")


# -----------------------------
# HTML update helpers
# -----------------------------
INDEX_PATH = "index.html"


def _read_index_html() -> str:
    with open(INDEX_PATH, "r", encoding="utf-8") as f:
        return f.read()


def _write_index_html(html: str):
    with open(INDEX_PATH, "w", encoding="utf-8") as f:
        f.write(html)


def _replace_between(html: str, start_marker: str, end_marker: str, new_content: str) -> str:
    pattern = re.compile(
        re.escape(start_marker) + r"(.*?)" + re.escape(end_marker),
        flags=re.DOTALL,
    )
    m = pattern.search(html)
    if not m:
        raise ValueError(f"Marker not found: {start_marker} ... {end_marker}")
    return html[: m.start(1)] + "\n" + new_content + "\n" + html[m.end(1) :]


def _update_title_and_date(html: str, date_str: str) -> str:
    # <div class="title">🇰🇷 마감 숏 리포트</div>
    # <div class="date">2026-01-26 (월) · KST 16:10</div>
    # 날짜는 여기서 최소만 갱신(요일 계산은 생략해도 됨)
    html = re.sub(
        r'(<div class="date">)(.*?)(</div>)',
        rf"\1{date_str} · KST 16:10\3",
        html,
        flags=re.DOTALL,
    )
    return html


def _update_index_section(html: str, data: dict) -> str:
    # [1) 지수]
    html = _replace_between(
        html,
        "<!--IDX_START-->",
        "<!--IDX_END-->",
        f"""<li>코스피: {data.get("kospi_driver","")}</li>
<li>코스닥: {data.get("kosdaq_driver","")}</li>""",
    )

    # [2) 외국인 수급]
    html = _replace_between(
        html,
        "<!--FLOW_START-->",
        "<!--FLOW_END-->",
        f"""<li>코스피: {data.get("kospi_flow_comment","")}</li>
<li>코스닥: {data.get("kosdaq_flow_comment","")}</li>""",
    )

    # [3) 환율]
    html = _replace_between(
        html,
        "<!--FX_START-->",
        "<!--FX_END-->",
        f"""<li>주요원인: {data.get("fx_driver","")}</li>""",
    )

    # [4) 달러 매수 포인트]
    # pill
    html = re.sub(
        r'(<div class="pill mono">)(.*?)(</div>)',
        rf"\1{data.get('score_comment','')}\3",
        html,
        flags=re.DOTALL,
    )
    # 3줄 가이드
    html = _replace_between(
        html,
        "<!--DOLLAR_GUIDE_START-->",
        "<!--DOLLAR_GUIDE_END-->",
        f"""<li>달러 인덱스(DXY): {data.get("dxy_driver","")}</li>
<li>미국 금리(10Y): {data.get("us_rate_driver","")}</li>
<li>외국인 수급: {data.get("flow_driver","")}</li>
<li>무역수지/수급: {data.get("trade_driver","")}</li>""",
    )

    # [5) 내일 체크 2개]
    html = _replace_between(
        html,
        "<!--CHK_OVERSEAS_START-->",
        "<!--CHK_OVERSEAS_END-->",
        f"""<li>{data.get("overseas1","")}</li>
<li>{data.get("overseas2","")}</li>""",
    )
    html = _replace_between(
        html,
        "<!--CHK_DOMESTIC_START-->",
        "<!--CHK_DOMESTIC_END-->",
        f"""<li>{data.get("domestic1","")}</li>
<li>{data.get("domestic2","")}</li>""",
    )

    return html


def main():
    date_str = kst_today()

    # 1) Gemini 코멘트 생성
    data = gemini_generate_commentary(date_str)

    # 2) index.html 업데이트
    html = _read_index_html()
    html = _update_title_and_date(html, date_str)
    html = _update_index_section(html, data)
    _write_index_html(html)

    print("[ok] index.html updated")


if __name__ == "__main__":
    main()
