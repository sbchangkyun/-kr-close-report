import os
import re
from datetime import datetime, timezone, timedelta

import google.generativeai as genai

KST = timezone(timedelta(hours=9))

def kst_today():
    return datetime.now(tz=KST).strftime("%Y-%m-%d")

def gemini_generate_commentary(date_str: str) -> dict:
    """
    ✅ 여기서는 우선 '문장 생성'만 Gemini로 처리합니다.
    (지수/환율/수급 숫자 자동 수집은 다음 단계에서 붙이는 게 안정적입니다.)
    """
    genai.configure(api_key=os.environ["GEMINI_API_KEY"])
    model = genai.GenerativeModel("gemini-1.5-flash")

    prompt = f"""
당신은 한국 주식 '장마감 숏 리포트' 문장 생성기입니다.
날짜는 {date_str} 입니다.
아래 항목을 한국어로 아주 짧게 1줄씩 생성하세요. 긴 문단 금지.

반드시 JSON만 출력:
{{
  "kospi_driver": "...",
  "kosdaq_driver": "...",
  "kospi_flow_comment": "...",
  "kosdaq_flow_comment": "...",
  "fx_driver": "주요원인: ...",
  "score_comment": "🟡 분할 매수—1,440원대 2~3회 레벨 분할, 급반등 추격 금지",
  "dxy_driver": "한줄 코멘트",
  "us_rate_driver": "한줄 코멘트",
  "flow_driver": "한줄 코멘트",
  "trade_driver": "한줄 코멘트",
  "overseas1": "...",
  "overseas2": "...",
  "domestic1": "...",
  "domestic2": "..."
}}

조건:
- 문장은 모두 짧게(한 줄)
- overseas/domestic은 '이슈 이름 + 영향 포인트' 형태로
"""

    resp = model.generate_content(prompt)
    text = resp.text.strip()

    # Gemini가 ```json ... ```로 감싸서 줄 때도 있어 제거
    text = re.sub(r"^```json\s*|\s*```$", "", text, flags=re.DOTALL).strip()

    import json
    return json.loads(text)

def update_index_html(html: str, date_str: str, c: dict) -> str:
    # 1) 제목 날짜 갱신
    html = re.sub(r"(🇰🇷 마감 숏 리포트 \()\d{4}-\d{2}-\d{2}(\))", rf"\1{date_str}\2", html)

    # 2) [1) 지수] / [2) 수급] / [3) 환율] 등 '한 줄 코멘트'만 우선 교체
    html = re.sub(r"(• 코스피 이슈:\s*).*", rf"\1{c['kospi_driver']}", html)
    html = re.sub(r"(• 코스닥 이슈:\s*).*", rf"\1{c['kosdaq_driver']}", html)

    html = re.sub(r"(• 코스피 이슈:\s*)(?!.*\[1\)\s지수\]).*", rf"• 코스피 이슈: {c['kospi_flow_comment']}", html, count=1)
    html = re.sub(r"(• 코스닥 이슈:\s*)(?!.*\[1\)\s지수\]).*", rf"• 코스닥 이슈: {c['kosdaq_flow_comment']}", html, count=1)

    html = re.sub(r"(• 환율 이슈:\s*).*", rf"\1{c['fx_driver']}", html)

    # 3) [4) 달러 매수 포인트] 라운드 박스 문구(이미 주인님 스타일로 구성되어 있다고 가정)
    html = re.sub(
        r'(<div class="pill mono">).*?(</div>)',
        rf"\1{c['score_comment']}\2",
        html,
        count=1,
        flags=re.DOTALL
    )

    # 4) 내일 체크 2개
    html = re.sub(r"(•\s*)(\{overseas_watch1\}|.*)", rf"• {c['overseas1']}", html, count=1)
    html = re.sub(r"(•\s*)(\{overseas_watch2\}|.*)", rf"• {c['overseas2']}", html, count=1)
    html = re.sub(r"(•\s*)(\{domestic_watch1\}|.*)", rf"• {c['domestic1']}", html, count=1)
    html = re.sub(r"(•\s*)(\{domestic_watch2\}|.*)", rf"• {c['domestic2']}", html, count=1)

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
