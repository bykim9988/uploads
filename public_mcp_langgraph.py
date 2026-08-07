"""공개 MCP 서버 + LangGraph 에이전트 (Node.js / API Key 불필요 구성).

7주차 실습 노트북(public_mcp_langgraph_step_by_step.ipynb)을 하나의 스크립트로 합친 것이다.
등록된 서버는 다음 두 조건을 모두 만족하는 것만 남겼다.

  1. Node.js가 필요 없다  -> uvx(Python) 실행 또는 원격 HTTP 연결
  2. 인증키가 필요 없다   -> .env 에 넣을 MCP 전용 키가 없다 (LLM 키는 별개)

  uvx  : fetch, time, arxiv, youtube, ddg, wikipedia
  원격 : deepwiki, context7

제외한 것: weather / finance / filesystem / memory / playwright / seq (npx = Node.js 필요),
Alpha Vantage / Firecrawl / Tavily / Apify / Brave (API Key 필요).

스크립트로 실행하면 asyncio.run()이 Windows에서 ProactorEventLoop를 쓰므로
노트북에서 필요했던 이벤트 루프 / stderr 우회 패치가 전혀 필요 없다.

사용 예:
    uv run python public_mcp_langgraph.py --list-tools all      # LLM 없이 전 서버 연결 점검
    uv run python public_mcp_langgraph.py --test-all            # 전 서버 대표 질문 실행 + 요약
    uv run python public_mcp_langgraph.py --test-all ddg wikipedia
    uv run python public_mcp_langgraph.py --test-all --preview 0   # 결과를 자르지 않고 전문 출력
    uv run python public_mcp_langgraph.py --servers fetch,time --ask "서울 현재 시각 알려줘"
    uv run python public_mcp_langgraph.py --servers all --demo --preview 0 --verbose

환경 변수:
    MCP_ACTIVE_SERVERS   활성 서버 목록 (쉼표 구분). --servers 옵션이 우선한다.
    MCP_MODEL            LLM 지정. 기본값은 아래 DEFAULT_MODEL.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from dataclasses import dataclass
from typing import Any

from dotenv import load_dotenv
from langchain.chat_models import init_chat_model
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.graph import START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition

# 한글 출력이 깨지는 콘솔(cp949 등)에서도 동작하도록.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

# langchain-google-genai는 MCP Tool 스키마를 Gemini 형식으로 바꾸면서 지원하지 않는 키
# (additionalProperties, $schema 등)를 버릴 때마다 경고를 찍는다. Tool이 수십 개면
# 화면이 이 경고로 덮여 정작 실행 결과가 묻힌다. 동작에는 영향이 없으므로 ERROR 이상만 남긴다.
logging.getLogger("langchain_google_genai").setLevel(logging.ERROR)


# ---------------------------------------------------------------------------
# 셀 1 - 모듈과 모델 설정
# ---------------------------------------------------------------------------

load_dotenv()

# 기본 모델. MCP_MODEL 환경변수로 교체 가능.
#   예: "anthropic:claude-sonnet-4-6" (ANTHROPIC_API_KEY), "openai:gpt-4.1-mini" (OPENAI_API_KEY)
DEFAULT_MODEL = "google_genai:gemini-3.5-flash-lite"  # GOOGLE_API_KEY 필요

# temperature 를 거부하거나(claude 최신 모델은 400) 무시하는(gemini-3 계열은 경고) 모델.
# 이 목록에 걸리면 temperature 를 아예 넘기지 않는다.
MODELS_WITHOUT_TEMPERATURE = (
    "claude-opus-5",
    "claude-sonnet-5",
    "claude-fable-5",
    "claude-opus-4-7",
    "claude-opus-4-8",
    "gemini-3",
)


# ---------------------------------------------------------------------------
# 셀 2~10 - MCP 서버 등록 (Node.js 불필요 · 인증키 불필요)
#
# 연결 방식은 두 가지뿐이다.
#   stdio          : uvx 가 Python 서버를 자식 프로세스로 띄운다. args 가 그대로 CLI 인자다.
#   streamable_http: 원격 서버에 URL 로 붙는다. 로컬 프로세스가 없으니 실행기도 필요 없다.
# ---------------------------------------------------------------------------

CONNECTIONS: dict[str, dict[str, Any]] = {
    # -- uvx (Python 서버, stdio) --------------------------------------------
    "fetch": {
        "transport": "stdio",
        "command": "uvx",
        "args": ["--with", "mcp<1.20", "mcp-server-fetch"],  # 버전 핀 필수
    },
    "time": {
        "transport": "stdio",
        "command": "uvx",
        "args": ["--with", "mcp<1.20", "mcp-server-time"],  # 버전 핀 필수
    },
    "arxiv": {
        "transport": "stdio",
        "command": "uvx",
        "args": ["arxiv-mcp-server"],
    },
    "youtube": {
        "transport": "stdio",
        "command": "uvx",
        "args": ["--with", "mcp<2", "mcp-youtube-transcript"],  # 버전 핀 필수
    },
    "ddg": {
        "transport": "stdio",
        "command": "uvx",
        "args": ["duckduckgo-mcp-server"],
    },
    "wikipedia": {
        "transport": "stdio",
        "command": "uvx",
        "args": ["wikipedia-mcp", "--language", "ko"],
    },
    # -- 원격 HTTP -----------------------------------------------------------
    "deepwiki": {  # 무인증
        "transport": "streamable_http",
        "url": "https://mcp.deepwiki.com/mcp",
    },
    "context7": {  # 키 없이 가능, 429 나면 무료 키 발급
        "transport": "streamable_http",
        "url": "https://mcp.context7.com/mcp",
    },
}

# 셀 11 - 최소 경로. Tool 수가 많으면 선택 정확도가 떨어지므로 좁게 시작한다.
DEFAULT_ACTIVE_SERVERS = ("fetch", "time")


# ---------------------------------------------------------------------------
# 셀 3 - 서버 한 개만 열어 Tool 목록 점검
# ---------------------------------------------------------------------------


async def inspect_server(server_name: str) -> list[Any]:
    """서버 하나만 연결해 제공 Tool 목록을 출력한다."""
    temp_client = MultiServerMCPClient(
        {server_name: CONNECTIONS[server_name]},
        tool_name_prefix=True,
        handle_tool_errors=True,
    )
    server_tools = await temp_client.get_tools()
    for tool in sorted(server_tools, key=lambda item: item.name):
        description = (tool.description or "").strip().splitlines()
        summary = description[0] if description else ""
        print(f"- {tool.name}: {summary}")
    return server_tools


async def inspect_servers(server_names: list[str]) -> dict[str, list[Any]]:
    """여러 서버를 차례로 점검한다. 실패한 서버는 건너뛰고 계속 진행한다."""
    results: dict[str, list[Any]] = {}
    for name in server_names:
        print(f"\n=== {name} ===")
        try:
            results[name] = await inspect_server(name)
        except Exception as exc:  # 공개 서버는 버전 충돌로 죽는 경우가 흔하다
            print(f"  !! 연결 실패: {type(exc).__name__}: {exc}")
    return results


# ---------------------------------------------------------------------------
# 셀 12~13 - 전체 Tool 수집과 모델 바인딩
# ---------------------------------------------------------------------------


def resolve_active_servers(raw: str | None) -> list[str]:
    """--servers / MCP_ACTIVE_SERVERS / 기본값 순으로 활성 서버를 정한다."""
    source = raw or os.getenv("MCP_ACTIVE_SERVERS")
    if not source:
        return list(DEFAULT_ACTIVE_SERVERS)
    if source.strip() == "all":
        return list(CONNECTIONS)
    names = [item.strip() for item in source.split(",") if item.strip()]
    unknown = [name for name in names if name not in CONNECTIONS]
    if unknown:
        raise SystemExit(
            f"등록되지 않은 서버: {', '.join(unknown)}\n"
            f"사용 가능: {', '.join(sorted(CONNECTIONS))}"
        )
    return names


def build_model(tools: list[Any]):
    """LLM을 만들고 전체 Tool을 바인딩한다."""
    model_name = os.getenv("MCP_MODEL", DEFAULT_MODEL)
    kwargs: dict[str, Any] = {"max_tokens": 1024}
    if not any(marker in model_name for marker in MODELS_WITHOUT_TEMPERATURE):
        kwargs["temperature"] = 0
    model = init_chat_model(model_name, **kwargs)
    print(f"model: {model_name}  |  tools: {len(tools)}")
    return model.bind_tools(tools)


# ---------------------------------------------------------------------------
# 셀 14~16 - System Prompt, Agent Node, StateGraph
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """
사용자 질문과 등록된 Tool 설명을 비교하여 필요한 Tool만 선택하라.
- URL 본문은 Fetch MCP
- 현재 시각·시간대 변환은 Time MCP
- 논문 검색·요약은 arXiv MCP
- 유튜브 영상 자막은 YouTube Transcript MCP
- 일반 웹 검색은 DuckDuckGo MCP
- 백과 지식·정의는 Wikipedia MCP
- GitHub 공개 저장소 질문은 DeepWiki MCP
- 라이브러리 최신 문서·코드 예시는 Context7
상태 변경 작업은 사용자가 명시적으로 요청했을 때만 실행한다.
Tool 오류는 결과 없음을 숨기지 말고 추측하지 않는다.
""".strip()


def build_graph(tools: list[Any]):
    """agent <-> tools 루프를 갖는 StateGraph 하나를 만든다."""
    model_with_tools = build_model(tools)

    async def call_model(state: MessagesState) -> dict[str, Any]:
        response = await model_with_tools.ainvoke(
            [SystemMessage(content=SYSTEM_PROMPT), *state["messages"]]
        )
        return {"messages": [response]}

    builder = StateGraph(MessagesState)
    builder.add_node("agent", call_model)
    builder.add_node("tools", ToolNode(tools))
    builder.add_edge(START, "agent")
    builder.add_conditional_edges("agent", tools_condition)
    builder.add_edge("tools", "agent")
    return builder.compile()


# ---------------------------------------------------------------------------
# 셀 17 - 실행 추적: 무엇을 어떤 인자로 부르고 무엇이 돌아왔는지
# ---------------------------------------------------------------------------


def as_text(content: Any) -> str:
    """LangChain 메시지 content를 사람이 읽을 텍스트로 편다.

    모델·서버에 따라 content가 str이거나 [{'type': 'text', 'text': ...}] 형태의
    블록 리스트다. Gemini는 여기에 서명(signature) 같은 내부 필드까지 섞어 보내므로
    그대로 print하면 결과 대신 원시 dict가 보인다.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                if "text" in block:
                    parts.append(str(block["text"]))
                else:  # 이미지 등 텍스트가 아닌 블록은 타입만 표시
                    parts.append(f"<{block.get('type', 'block')}>")
        return "\n".join(part for part in parts if part)
    return str(content)


def shorten(text: str, limit: int) -> str:
    """limit 이하면 그대로, 넘으면 잘라서 남은 글자 수를 알려준다. limit<=0 이면 전체."""
    text = text.strip()
    if limit <= 0 or len(text) <= limit:
        return text
    return f"{text[:limit]} ... (+{len(text) - limit}자)"


def looks_empty(text: str) -> bool:
    """빈 문자열뿐 아니라 [] / {} / null 같은 '내용 없는 결과'도 잡아낸다."""
    return text.strip() in ("", "[]", "{}", "null", "none", "None")


@dataclass
class ToolCallRecord:
    """Tool 한 번의 호출: 무엇을 어떤 인자로 부르고 무엇이 돌아왔는지."""

    tool: str
    args: dict[str, Any]
    result: str = ""


@dataclass
class AgentRun:
    """질문 한 건의 실행 기록."""

    query: str
    calls: list[ToolCallRecord]
    answer: str


async def run_agent(graph, query: str, preview: int = 600, verbose: bool = True) -> AgentRun:
    """질문 하나를 실행한다.

    verbose=True 면 Tool 선택 -> 인자 -> 결과 -> 답변을 단계별로 출력한다(--test-all 용).
    verbose=False 면 아무것도 출력하지 않고 기록만 돌려준다. 호출한 쪽에서
    print_report() 로 요약 블록만 찍을 때 쓴다(--demo / --ask 기본 동작).
    """
    if verbose:
        print(f"\n>> 질문: {query}")
    result = await graph.ainvoke(
        {"messages": [HumanMessage(content=query)]},
        {"recursion_limit": 18},
    )

    calls: list[ToolCallRecord] = []
    pending: dict[str, ToolCallRecord] = {}  # tool_call_id -> 기록
    for message in result["messages"]:
        if isinstance(message, AIMessage) and message.tool_calls:
            for call in message.tool_calls:
                record = ToolCallRecord(tool=call["name"], args=call.get("args", {}) or {})
                calls.append(record)
                if call.get("id"):
                    pending[call["id"]] = record
                if verbose:
                    print(f"\n   [Tool] {record.tool}")
                    print(f"   [인자] {json.dumps(record.args, ensure_ascii=False)}")
        elif isinstance(message, ToolMessage):
            text = as_text(message.content)
            record = pending.get(message.tool_call_id)
            if record is not None:
                record.result = text
            elif calls:  # id 매칭이 안 되면 마지막 호출에 붙인다
                calls[-1].result = text
            if verbose:
                status = " (오류)" if getattr(message, "status", None) == "error" else ""
                print(f"   [결과]{status} {shorten(text, preview)}")

    answer = as_text(result["messages"][-1].content)
    if verbose:
        print(f"\n   [답변] {shorten(answer, preview)}")
    return AgentRun(query=query, calls=calls, answer=answer)


REPORT_RULE = "-" * 78


def print_report(
    run: AgentRun, preview: int = 600, index: int | None = None, total: int | None = None
) -> None:
    """질문 한 건의 결과를 고정 블록 형식으로 출력한다.

    ---------------------------------------------------------------
    * 테스트 MCP 도구이름:
    * 질문:
    * 결과:
    ---------------------------------------------------------------
    """
    tools = ", ".join(dict.fromkeys(call.tool for call in run.calls)) or "(호출된 Tool 없음)"
    counter = f"  [{index}/{total}]" if index and total else ""
    # 서버 로그(stderr)와 섞이지 않도록 구분선 앞에 빈 줄을 둔다.
    print(f"\n\n{REPORT_RULE}{counter}")
    print(f"* 테스트 MCP 도구이름: {tools}")
    print(f"* 질문: {run.query}")
    print(f"* 결과: {shorten(run.answer, preview)}")
    print(REPORT_RULE)


# ---------------------------------------------------------------------------
# 셀 18~26 - 서버별 대표 질문
# ---------------------------------------------------------------------------

# 서버 하나당 대표 질문 하나. --test-all 이 이 표를 그대로 순회한다.
SMOKE_QUERIES: dict[str, str] = {
    "fetch": "https://modelcontextprotocol.io/introduction 페이지를 읽고 핵심 내용을 5개로 요약해줘.",
    "time": "현재 서울 시각과 같은 시각의 뉴욕 시각을 함께 표시해줘.",
    "arxiv": "retrieval augmented generation 관련 최신 논문 2편을 arXiv에서 찾아 제목과 핵심을 요약해줘.",
    "youtube": "https://www.youtube.com/watch?v=jNQXAC9IVRw 영상의 자막을 가져와 핵심을 3줄로 정리해줘.",
    "ddg": "Model Context Protocol의 최근 동향을 웹에서 검색해 요약해줘.",
    "wikipedia": "위키백과에서 '전이 학습'을 찾아 세 문장으로 설명해줘.",
    "deepwiki": "GitHub 저장소 modelcontextprotocol/servers 는 어떤 구조로 되어 있는지 알려줘.",
    "context7": "langgraph 라이브러리의 최신 문서를 찾아 StateGraph 사용법을 요약해줘.",
}

# 서로 다른 MCP를 연달아 선택해야 하는 질문. --demo 에서 조건이 맞으면 실행된다.
COMBO_QUERIES: list[tuple[tuple[str, ...], str]] = [
    (
        ("wikipedia", "time"),
        "위키백과에서 '기계 학습'의 정의를 찾아 두 문장으로 알려주고, 지금 서울 시각도 함께 표시해줘.",
    ),
    (
        ("ddg", "fetch"),
        "DuckDuckGo로 Model Context Protocol 공식 사이트를 검색한 뒤, 찾은 URL의 본문을 읽어 세 줄로 요약해줘.",
    ),
]


def demo_queries_for(active: list[str]) -> list[str]:
    """활성 서버로 실제 답변 가능한 데모 질문만 고른다."""
    active_set = set(active)
    queries = [SMOKE_QUERIES[name] for name in active if name in SMOKE_QUERIES]
    queries.extend(query for needed, query in COMBO_QUERIES if active_set.issuperset(needed))
    return queries


async def test_all_servers(server_names: list[str], preview: int = 600) -> int:
    """서버를 하나씩만 활성화해 대표 질문을 돌린다.

    여러 서버를 한 번에 바인딩하면 Tool이 수십 개가 되어 선택 정확도가 떨어지므로,
    서버별로 격리해서 '연결 -> Tool 선택 -> 인자 -> 실제 결과 -> 답변'까지 확인한다.
    """
    results: list[tuple[str, str, str, AgentRun | None]] = []  # (서버, 판정, 비고, 실행기록)

    for index, name in enumerate(server_names, start=1):
        query = SMOKE_QUERIES.get(name)
        print(f"\n{'=' * 78}\n[{index}/{len(server_names)}] {name}\n{'=' * 78}")
        if query is None:
            print("  -- 대표 질문이 없어 건너뛴다.")
            results.append((name, "SKIP", "대표 질문 미정의", None))
            continue
        try:
            client = MultiServerMCPClient(
                {name: CONNECTIONS[name]},
                tool_name_prefix=True,
                handle_tool_errors=True,
            )
            tools = await client.get_tools()
            print(f"   Tool {len(tools)}개 로드")
            graph = build_graph(tools)
            run = await run_agent(graph, query, preview=preview)
        except Exception as exc:
            print(f"  !! 실패: {type(exc).__name__}: {exc}")
            results.append((name, "FAIL", f"{type(exc).__name__}: {exc}", None))
            continue

        empty = [call.tool for call in run.calls if looks_empty(call.result)]
        if not run.calls:
            results.append((name, "NO-TOOL", "모델이 Tool을 호출하지 않음", run))
        elif empty:
            results.append((name, "EMPTY", f"결과가 비어 있음: {', '.join(empty)}", run))
        else:
            results.append((name, "OK", f"{len(run.calls)}회 호출", run))

    # ---- 요약: 서버별로 무엇을 보내 무엇이 돌아왔는지까지 다시 보여준다 ----
    print(f"\n\n{'=' * 78}\n테스트 요약\n{'=' * 78}")
    for name, verdict, note, run in results:
        print(f"\n[{verdict}] {name} — {note}")
        if run is None:
            continue
        print(f"    질문: {shorten(run.query, 100)}")
        for call in run.calls:
            args = json.dumps(call.args, ensure_ascii=False)
            print(f"    호출: {call.tool}({shorten(args, 100)})")
            print(f"    결과: {shorten(call.result, 160) or '(비어 있음)'}")
        print(f"    답변: {shorten(run.answer, 160)}")

    print(f"\n{'-' * 78}")
    for name, verdict, note, _ in results:
        print(f"{verdict:<8} {name:<12} {note}")
    passed = sum(1 for _, verdict, _, _ in results if verdict == "OK")
    print(f"\n{passed}/{len(results)} 서버 통과")
    return 0 if passed == len(results) else 1


# ---------------------------------------------------------------------------
# 진입점
# ---------------------------------------------------------------------------


def expand_server_names(names: list[str], fallback: list[str]) -> list[str]:
    """'all' 키워드를 등록된 전체 서버로 펼치고 이름을 검증한다."""
    if not names:
        return fallback
    if len(names) == 1 and names[0] == "all":
        return list(CONNECTIONS)
    unknown = [name for name in names if name not in CONNECTIONS]
    if unknown:
        raise SystemExit(f"등록되지 않은 서버: {', '.join(unknown)}")
    return names


async def amain(args: argparse.Namespace) -> int:
    if args.test_all is not None:
        targets = expand_server_names(args.test_all, list(CONNECTIONS))
        print(f"테스트 대상 {len(targets)}개: {', '.join(targets)}")
        return await test_all_servers(targets, preview=args.preview)

    active = resolve_active_servers(args.servers)
    print(f"활성 서버: {', '.join(active)}")

    if args.list_tools is not None:
        targets = expand_server_names(args.list_tools, active)
        found = await inspect_servers(targets)
        total = sum(len(items) for items in found.values())
        print(f"\n{len(found)}/{len(targets)}개 서버 연결, Tool {total}개")
        return 0 if len(found) == len(targets) else 1

    queries: list[str] = list(args.ask or [])
    if args.demo:
        queries.extend(demo_queries_for(active))
    if not queries:
        print("실행할 질문이 없다. --ask 또는 --demo 를 지정한다. (--list-tools 로 점검만도 가능)")
        return 1

    client = MultiServerMCPClient(
        {name: CONNECTIONS[name] for name in active},
        tool_name_prefix=True,
        handle_tool_errors=True,
    )
    tools = await client.get_tools()
    graph = build_graph(tools)

    for index, query in enumerate(queries, start=1):
        run = await run_agent(graph, query, preview=args.preview, verbose=args.verbose)
        print_report(run, preview=args.preview, index=index, total=len(queries))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Node.js·API Key 없이 쓰는 공개 MCP 서버를 하나의 LangGraph Agent에 연결한다.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"등록된 서버: {', '.join(CONNECTIONS)}",
    )
    parser.add_argument(
        "--servers",
        help="활성 서버 목록 (쉼표 구분, 'all' 가능). 기본값: " + ",".join(DEFAULT_ACTIVE_SERVERS),
    )
    parser.add_argument(
        "--list-tools",
        nargs="*",
        metavar="SERVER",
        help="LLM 호출 없이 서버별 Tool 목록만 점검한다. 생략하면 활성 서버, 'all'이면 전체.",
    )
    parser.add_argument(
        "--test-all",
        nargs="*",
        metavar="SERVER",
        help="서버를 하나씩만 활성화해 대표 질문을 실행하고 성공/실패를 요약한다. "
        "서버명을 생략하면 등록된 전체(LLM 호출이 서버 수만큼 발생).",
    )
    parser.add_argument(
        "--preview",
        type=int,
        default=600,
        metavar="N",
        help="Tool 결과·답변을 몇 글자까지 출력할지. 0이면 자르지 않는다. 기본 600",
    )
    parser.add_argument("--ask", action="append", metavar="QUERY", help="실행할 질문 (반복 가능)")
    parser.add_argument("--demo", action="store_true", help="활성 서버에 맞는 데모 질문을 실행한다.")
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="--demo/--ask 실행 시 요약 블록 앞에 Tool 인자·중간 결과까지 함께 출력한다.",
    )
    args = parser.parse_args()

    return asyncio.run(amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
