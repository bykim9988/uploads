# multiturn_chatbot_gradio.py
# A-4 `multiturn_chatbot.py`(터미널 버전)를 Gradio 웹 UI로 옮긴 버전.
#
# 화면 구성 (좌 : 우 = 7 : 3)
#   - 좌측: 멀티턴 대화 (위: 대화 결과 / 아래: 사용자 입력창)
#   - 우측: 도구 호출 과정 로그 (어떤 도구가 어떤 인자로 불렸고 결과가 무엇인지)
#
# 사전 준비: 세션 2의 패키지 + `uv add gradio`
# 실행:      uv run python multiturn_chatbot_gradio.py  →  브라우저에서 http://localhost:7860
import os
from dotenv import load_dotenv
from datetime import datetime
from zoneinfo import ZoneInfo

import gradio as gr
from langchain.chat_models import init_chat_model
from langchain_core.tools import tool
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_community.tools import DuckDuckGoSearchResults
from langchain_community.utilities import DuckDuckGoSearchAPIWrapper
from langchain_community.document_loaders import YoutubeLoader
from youtube_search import YoutubeSearch

load_dotenv()


# ── 도구 4개: multiturn_chatbot.py와 동일 ──────────────────────────────────

@tool
def get_word_count(text: str) -> str:
    """텍스트의 단어 수와 글자 수를 센다."""
    return f"단어 수: {len(text.split())}, 글자 수: {len(text)}"


@tool
def get_current_time_with_timezone(timezone: str) -> str:
    """IANA 시간대 이름(예: 'Asia/Seoul', 'America/New_York')을 받아
    해당 지역의 현재 시간을 알려준다."""
    try:
        now = datetime.now(ZoneInfo(timezone))
        return now.strftime("%Y-%m-%d %H:%M:%S %Z")
    except Exception as e:
        return f"시간대 오류: {e} (예: 'Asia/Seoul', 'America/New_York' 형식으로 입력)"


@tool
def get_web_search(query: str, search_period: str = "m") -> str:
    """웹에서 최신 정보를 검색한다.

    Args:
        query: 검색어
        search_period: 검색 기간 ('d': 하루, 'w': 일주일, 'm': 한 달, 'y': 일 년)
    """
    wrapper = DuckDuckGoSearchAPIWrapper(region="kr-kr", time=search_period)
    search = DuckDuckGoSearchResults(api_wrapper=wrapper, results_separator=";\n")
    return search.invoke(query)


@tool
def get_youtube_search(query: str) -> str:
    """유튜브에서 영상을 검색하고, 각 영상의 자막 내용까지 가져온다.
    영상 추천·비교·내용 요약 요청에 사용한다.

    Args:
        query: 검색어
    """
    videos = YoutubeSearch(query, max_results=3).to_dict()
    videos = [v for v in videos if len(v["duration"].split(":")) < 3]  # 1시간 이상 제외

    results = []
    for v in videos:
        video_url = "https://youtube.com" + v["url_suffix"].split("&")[0]
        try:
            docs = YoutubeLoader.from_youtube_url(video_url, language=["ko", "en"]).load()
            # 토큰 절약: 자막은 영상당 앞 2,000자만 전달한다
            content = " ".join(d.page_content for d in docs)[:2000]
        except Exception as error:
            content = f"(자막을 가져오지 못했습니다: {error})"
        results.append(
            f"제목: {v['title']}\n채널: {v['channel']} | 길이: {v['duration']}\n"
            f"URL: {video_url}\n자막(일부): {content}"
        )
    return "\n\n---\n\n".join(results) if results else "검색 결과가 없습니다."


tools = [
    get_word_count,
    get_current_time_with_timezone,
    get_web_search,
    get_youtube_search,
]
tool_map = {t.name: t for t in tools}

MODEL = "anthropic:claude-sonnet-4-6"  # 다른 모델: "google_genai:gemini-3-flash", "openai:gpt-4.1-mini"
model = init_chat_model(MODEL, temperature=0)
model_with_tools = model.bind_tools(tools)

SYSTEM_PROMPT = (
    "너는 사용자의 질문에 가장 적합한 도구를 골라 호출해 답하는 AI 조교다. "
    "도구 결과에 없는 내용은 추측하지 마라."
)


# ── 에이전트 루프: 도구 호출 과정을 log_lines에 기록한다 ──────────────────

def get_ai_response(messages: list, log_lines: list) -> list:
    """모델을 호출하고, 도구 호출이 있으면 실행까지 마친 뒤
    최종 답변까지 포함된 전체 messages를 반환한다.
    도구 호출 한 건마다 이름·인자·결과 미리보기를 log_lines에 남긴다."""
    ai_message = model_with_tools.invoke(messages)
    messages.append(ai_message)

    while ai_message.tool_calls:
        for call in ai_message.tool_calls:
            log_lines.append(f"🔧 {call['name']}")
            log_lines.append(f"   ├ 인자: {call['args']}")
            result = tool_map[call["name"]].invoke(call["args"])
            preview = str(result).replace("\n", " ")
            if len(preview) > 200:
                preview = preview[:200] + " …"
            log_lines.append(f"   └ 결과: {preview}")
            messages.append(ToolMessage(content=str(result), tool_call_id=call["id"]))
        ai_message = model_with_tools.invoke(messages)
        messages.append(ai_message)

    return messages


def message_text(message) -> str:
    """AIMessage.content가 문자열이 아니라 블록 리스트로 올 때도 텍스트만 뽑아낸다."""
    content = message.content
    if isinstance(content, list):
        return "".join(
            block.get("text", "") for block in content if isinstance(block, dict)
        )
    return content


# ── Gradio 이벤트 핸들러 ──────────────────────────────────────────────────

def respond(user_input, chat_history, lc_messages, tool_log):
    """입력 1턴 처리: 대화(좌측)와 도구 호출 로그(우측)를 함께 갱신한다."""
    user_input = (user_input or "").strip()
    if not user_input:
        return "", chat_history, lc_messages, tool_log, tool_log

    if not lc_messages:  # 첫 턴이면 System Prompt로 대화를 시작한다
        lc_messages = [SystemMessage(content=SYSTEM_PROMPT)]

    turn = sum(1 for m in chat_history if m["role"] == "user") + 1
    title = user_input if len(user_input) <= 30 else user_input[:30] + "…"
    log_lines = [f"━━━ 턴 {turn}: {title}"]

    lc_messages.append(HumanMessage(content=user_input))
    try:
        lc_messages = get_ai_response(lc_messages, log_lines)
        answer = message_text(lc_messages[-1])
    except Exception as e:
        answer = f"오류가 발생했습니다: {e}"
        log_lines.append(f"⚠️ 오류: {e}")

    if len(log_lines) == 1:  # 도구를 안 쓰고 바로 답한 턴
        log_lines.append("· 도구 호출 없음 (모델이 직접 답변)")

    chat_history = chat_history + [
        {"role": "user", "content": user_input},
        {"role": "assistant", "content": answer},
    ]
    tool_log = (tool_log + "\n\n" if tool_log else "") + "\n".join(log_lines)
    return "", chat_history, lc_messages, tool_log, tool_log


def reset():
    """대화·도구 로그·내부 메시지 상태를 모두 초기화한다."""
    return "", [], [], "", ""


# ── 화면 구성: 좌(대화) 7 : 우(도구 호출 과정) 3 ──────────────────────────

with gr.Blocks(title="도구 4개를 실은 멀티턴 챗봇") as demo:
    gr.Markdown("## 🤖 도구 4개를 실은 멀티턴 챗봇 (Gradio)")

    lc_state = gr.State([])   # LangChain 메시지 리스트 (System/Human/AI/Tool 누적)
    log_state = gr.State("")  # 우측 패널에 표시할 도구 호출 로그 전문

    with gr.Row():
        with gr.Column(scale=7):
            chatbot = gr.Chatbot(type="messages", label="대화", height=520)
            with gr.Row():
                user_box = gr.Textbox(
                    placeholder="메시지를 입력하고 Enter (예: 지금 서울이랑 뉴욕 몇 시야?)",
                    show_label=False,
                    scale=9,
                )
                send_btn = gr.Button("보내기", variant="primary", scale=1)
            clear_btn = gr.Button("대화 초기화")
        with gr.Column(scale=3):
            tool_panel = gr.Textbox(
                label="도구 호출 과정",
                value="아직 도구 호출이 없습니다.",
                lines=26,
                max_lines=26,
                interactive=False,
                autoscroll=True,
            )

    inputs = [user_box, chatbot, lc_state, log_state]
    outputs = [user_box, chatbot, lc_state, log_state, tool_panel]
    user_box.submit(respond, inputs, outputs)
    send_btn.click(respond, inputs, outputs)
    clear_btn.click(reset, None, [user_box, chatbot, lc_state, log_state, tool_panel])


if __name__ == "__main__":
    demo.launch()
