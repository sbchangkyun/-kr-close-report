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
    Gemini가 'JSON만 출력' 지시를 가끔 어기는 경우를 대비해,
    본문에서 가장 그럴듯한 JSON 객체를 찾아 파싱합니다.
    """
    text = _strip_code_fences(text)

    # 1) 바로 파싱 시도
    try:
        return json.loads(text)
    except Exception:
        pass

    # 2) 텍스트 중 첫 {...} 블록 추출
    m = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not m:
        raise ValueError("Gemini 응답에서 JSON 객체를 찾지 못했습니다.")
    return json.loads(m.group(0))


def gemini_generate_commentary(date_str: str) -> dict:
    """
    ✅ 이 스크립트는 '문장/코멘트 생성'만 Gemini로 처리합니다.
    숫자(지수/환율/수급 데이터 자동 수집)는 추후 붙이는 것이 안정적입니다.
    """
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("환경변수 GEMINI_API_KEY가 없습니다. GitHub Secrets 설정을 확인해 주세요.")

    genai.configure(api_key=api_key)

    # 무료/가벼운 모델 우선. 필요 시 Actions env로 GEMINI_MODEL을 바꿀 수 있게 해둠.
    # 무료/가벼운 모델 우선. 필요 시 Actions env로 GEMINI_MODEL을 바꿀 수 있게 해둠.
    # (주의) google-generativeai(v1beta)에서는 일부 모델명이 404(NotFound)로 실패할 수 있어, 안전한 후보를 순차 시도합니다.
    preferred_model = os.environ.get("GEMINI_MODEL", "gemini-1.5-flash-latest")
    candidates = [
        preferred_model,
        "gemini-1.5-flash-latest",
        "gemini-1.5-pro-latest",
        "gemini-1.0-pro",
    ]
    # 중복 제거(순서 유지)
    seen = set()
    model_candidates = []
    for name in candidates:
        if name and name not in seen:
            seen.add(name)
            model_candidates.append(name)

    last_err = None
    for model_name in model_candidates:
        try:
            print(f"[gemini] using model: {model_name}")
            model = genai.GenerativeModel(model_name)
            resp = model.generate_content(prompt)
            return _extract_json(resp.text or "")
        except Exception as e:
            last_err = e
            msg = str(e)
            # 모델명/버전 불일치(NotFound)면 다음 후보로 재시도
            if ("404" in msg) or ("NotFound" in msg) or ("is not found" in msg) or ("not supported" in msg):
                print(f"[gemini] model not available: {model_name} -> {msg}")
                continue
            raise

    raise RuntimeError(f"Gemini 모델 호출에 실패했습니다. 마지막 오류: {last_err}")


def _update_title_and_date(html: str, date_str: str) -> str:
    # <title> ... (YYYY-MM-DD)</title>
    html = re.sub(
        r"(<title>[^<]*\()\d{4}-\d{2}-\d{2}(\)</title>)",
        rf"\g<1>{date_str}\g<2>",
        html,
    )
    # 상단 날짜 <div class="date mono">YYYY-MM-DD</div>
    html = re.sub(
        r'(<div class="date mono">)\d{4}-\d{2}-\d{2}(</div>)',
        rf"\g<1>{date_str}\g<2>",
        html,
    )
    return html


def _update_details_block(html: str, stitle: str, updater) -> str:
    """
    <details class="card"> ... </details> 블록 단위로 먼저 분리한 뒤,
    그 안에 <span class="stitle">...</span>가 포함된 블록만 정확히 치환합니다.
    (여러 섹션이 한 번에 매칭되는 문제를 방지)
    """
    block_pat = r"<details class=\"card\">.*?</details>"
    for m in re.finditer(block_pat, html, flags=re.DOTALL):
        block = m.group(0)
        if re.search(rf"<span class=\"stitle\">\s*{re.escape(stitle)}\s*</span>", block):
            new_block = updater(block)
            return html[:m.start()] + new_block + html[m.end():]

    raise RuntimeError(f"index.html에서 섹션 '{stitle}' 블록을 찾지 못했습니다.")



def _replace_li(block: str, label_prefix: str, new_text: str) -> str:
    """
    예: <li>코스피 이슈: ...</li> 에서 '...'만 교체
    """
    pattern = rf"(<li>\s*{re.escape(label_prefix)}\s*)(.*?)(\s*</li>)"
    return re.sub(pattern, rf"\g<1>{new_text}\g<3>", block, count=1, flags=re.DOTALL)


def _update_index_section(block: str, c: dict) -> str:
    block = _replace_li(block, "코스피 이슈:", c["kospi_driver"])
    block = _replace_li(block, "코스닥 이슈:", c["kosdaq_driver"])
    return block


def _update_flow_section(block: str, c: dict) -> str:
    block = _replace_li(block, "코스피 이슈:", c["kospi_flow_comment"])
    block = _replace_li(block, "코스닥 이슈:", c["kosdaq_flow_comment"])
    return block


def _update_fx_section(block: str, c: dict) -> str:
    block = _replace_li(block, "환율 이슈:", c["fx_driver"])
    return block


def _update_dollar_section(block: str, c: dict) -> str:
    # 라운드 박스(행동 가이드 1줄)
    block = re.sub(
        r'(<div class="pill mono">)(.*?)(</div>)',
        rf"\g<1>{c['score_comment']}\g<3>",
        block,
        count=1,
        flags=re.DOTALL,
    )
    # 하단 4개 라벨 라인 교체(라벨은 유지, 내용만 교체)
    block = _replace_li(block, "달러 인덱스(DXY):", c["dxy_driver"])
    block = _replace_li(block, "미국 금리/달러 모멘텀:", c["us_rate_driver"])
    block = _replace_li(block, "외국인 수급:", c["flow_driver"])
    block = _replace_li(block, "무역수지/수급:", c["trade_driver"])
    return block


def _update_tomorrow_section(block: str, c: dict) -> str:
    # [해외]/[국내] 각각 2개 li를 "순서대로" 교체합니다.
    def replace_two_lis(ul_block: str, a: str, b: str) -> str:
        items = [a, b]
        i = {"n": 0}

        def _repl(_m):
            n = i["n"]
            if n >= len(items):
                return _m.group(0)
            i["n"] += 1
            return f"<li>{items[n]}</li>"

        return re.sub(r"<li>.*?</li>", _repl, ul_block, count=2, flags=re.DOTALL)

    def repl_overseas(m):
        return replace_two_lis(m.group(0), c["overseas1"], c["overseas2"])

    def repl_domestic(m):
        return replace_two_lis(m.group(0), c["domestic1"], c["domestic2"])

    # 해외 UL 블록(바로 다음 <ul class="bul">...</ul>)
    block = re.sub(
        r"(<div class=\"small\"><b>\[해외\]</b></div>\s*<ul class=\"bul\">.*?</ul>)",
        repl_overseas,
        block,
        count=1,
        flags=re.DOTALL,
    )
    # 국내 UL 블록
    block = re.sub(
        r"(<div class=\"small\"><b>\[국내\]</b></div>\s*<ul class=\"bul\">.*?</ul>)",
        repl_domestic,
        block,
        count=1,
        flags=re.DOTALL,
    )
    return block



def update_index_html(html: str, date_str: str, c: dict) -> str:
    # 0) 날짜 갱신
    html = _update_title_and_date(html, date_str)

    # 1) 섹션별로 안전하게 교체
    html = _update_details_block(html, "지수", lambda b: _update_index_section(b, c))
    html = _update_details_block(html, "외국인 수급", lambda b: _update_flow_section(b, c))
    html = _update_details_block(html, "환율", lambda b: _update_fx_section(b, c))
    html = _update_details_block(html, "달러 매수 포인트", lambda b: _update_dollar_section(b, c))
    html = _update_details_block(html, "내일 체크", lambda b: _update_tomorrow_section(b, c))

    return html


def main():
    date_str = kst_today()

    with open("index.html", "r", encoding="utf-8") as f:
        html = f.read()

    c = gemini_generate_commentary(date_str)
    new_html = update_index_html(html, date_str, c)

    with open("index.html", "w", encoding="utf-8") as f:
        f.write(new_html)


if __name__ == "__main__":
    main()
