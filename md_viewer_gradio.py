"""로컬 마크다운 뷰어 — 현재 폴더의 .md 파일을 읽어 웹 화면으로 띄운다.

실습 교재·회의록처럼 폴더에 쌓여 있는 마크다운을 편집기 없이 브라우저에서 읽기 위한 도구다.
파일을 새로 저장했다면 [목록 새로고침]을 누르면 바로 반영된다.

사용 예:
    uv run python md_viewer_gradio.py                    # 현재 폴더, 8080 포트 + 공유 링크
    uv run python md_viewer_gradio.py --dir ./docs       # 다른 폴더를 읽기
    uv run python md_viewer_gradio.py --port 7860        # 포트 변경
    uv run python md_viewer_gradio.py --no-share         # 내 PC에서만 (공유 링크 없음)
    uv run python md_viewer_gradio.py --no-recursive     # 하위 폴더는 제외

설치:
    uv add gradio

참고:
    - 표·코드블록·수식은 그대로 렌더링되지만 ```mermaid 다이어그램은 코드 블록으로 보인다.
      (Gradio 기본 마크다운에 mermaid 렌더러가 없다.)
    - 왼쪽 파일 목록은 사이드바 화살표로 접었다 펼 수 있고, 오른쪽 모서리를 끌면 폭이 조절된다.
    - share=True 가 기본이라 실행할 때마다 공개 주소(*.gradio.live)가 만들어진다.
      링크를 아는 사람은 누구나 이 폴더의 마크다운을 볼 수 있으니, 혼자 볼 때는 --no-share 를 쓴다.
      시연이 끝나면 Ctrl+C 로 닫는다.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import gradio as gr

# 읽을 확장자와 건너뛸 폴더.
EXTENSIONS = (".md", ".markdown")
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".idea", ".vscode", ".ipynb_checkpoints"}

# --dir 로 지정한 기준 폴더. 이 밖의 파일은 읽지 않는다(경로 탈출 방지).
BASE_DIR = Path.cwd()
RECURSIVE = True


# ---------------------------------------------------------------------------
# 파일 찾기 / 읽기
# ---------------------------------------------------------------------------


def find_markdown_files() -> list[str]:
    """기준 폴더의 마크다운 파일을 상대경로 문자열 목록으로 돌려준다."""
    pattern = "**/*" if RECURSIVE else "*"
    found: list[str] = []
    for path in BASE_DIR.glob(pattern):
        if not path.is_file() or path.suffix.lower() not in EXTENSIONS:
            continue
        relative = path.relative_to(BASE_DIR)
        if any(part in SKIP_DIRS for part in relative.parts):
            continue
        found.append(str(relative))
    # 폴더 깊이 → 이름 순으로 정렬해 같은 폴더 파일이 붙어 보이게 한다.
    return sorted(found, key=lambda name: (name.count("/") + name.count("\\"), name.lower()))


def resolve_safely(name: str) -> Path | None:
    """상대경로 문자열을 실제 경로로 바꾼다. 기준 폴더 밖이면 None."""
    if not name:
        return None
    path = (BASE_DIR / name).resolve()
    try:
        path.relative_to(BASE_DIR.resolve())
    except ValueError:  # ../.. 같은 경로로 폴더 밖을 가리키는 경우
        return None
    return path if path.is_file() else None


def read_markdown(name: str) -> tuple[str, str, str]:
    """(렌더링용 본문, 원본 텍스트, 파일 정보) 를 돌려준다."""
    path = resolve_safely(name)
    if path is None:
        message = "파일을 찾을 수 없습니다. [목록 새로고침]을 눌러 주세요."
        return f"> {message}", "", ""

    text = path.read_text(encoding="utf-8", errors="replace")
    size_kb = path.stat().st_size / 1024
    lines = text.count("\n") + 1
    info = f"`{name}`  ·  {size_kb:,.1f} KB  ·  {lines:,}줄"
    return text, text, info


def filter_files(keyword: str, files: list[str]) -> list[str]:
    """파일명 또는 본문에 키워드가 들어간 파일만 남긴다."""
    keyword = keyword.strip().lower()
    if not keyword:
        return files

    matched: list[str] = []
    for name in files:
        if keyword in name.lower():
            matched.append(name)
            continue
        path = resolve_safely(name)
        if path is None:
            continue
        try:
            if keyword in path.read_text(encoding="utf-8", errors="replace").lower():
                matched.append(name)
        except OSError:
            continue
    return matched


# ---------------------------------------------------------------------------
# Gradio 이벤트 핸들러
# ---------------------------------------------------------------------------


def on_refresh(keyword: str, current: str):
    """목록을 다시 읽고, 보고 있던 파일이 남아 있으면 선택을 유지한다."""
    files = find_markdown_files()
    visible = filter_files(keyword, files)
    selected = current if current in visible else (visible[0] if visible else None)
    body, raw, info = read_markdown(selected) if selected else ("> 표시할 마크다운 파일이 없습니다.", "", "")
    status = f"{len(visible)}개 파일" + (f" (전체 {len(files)}개 중)" if len(visible) != len(files) else "")
    return gr.update(choices=visible, value=selected), body, raw, info, status


def on_select(name: str):
    return read_markdown(name)


# ---------------------------------------------------------------------------
# 화면 구성
# ---------------------------------------------------------------------------


# 사이드바를 화면 왼쪽 끝에 붙이고, 오른쪽 모서리를 끌어 폭을 조절할 수 있게 한다.
# (열림/닫힘은 gr.Sidebar가 제공하는 화살표 버튼으로 한다.)
CSS = """
#file-sidebar {
    padding: 8px 10px 8px 8px;
    resize: horizontal;          /* 오른쪽 모서리를 드래그해 폭 조절 */
    overflow: auto;
    min-width: 180px;
    max-width: 70vw;
}
#file-sidebar .form { border: none; background: transparent; }
#file-list label { padding: 3px 6px; }   /* 파일이 많아도 한눈에 들어오게 */
"""

# Gradio 6.0부터 css 인자가 Blocks() 에서 launch() 로 옮겨졌다. 두 버전 모두에서 돌아가게 한다.
_MAJOR = int(gr.__version__.split(".")[0])
BLOCKS_KW: dict = {} if _MAJOR >= 6 else {"css": CSS}
LAUNCH_KW: dict = {"css": CSS} if _MAJOR >= 6 else {}


def build_ui() -> gr.Blocks:
    files = find_markdown_files()
    first = files[0] if files else None
    body, raw, info = read_markdown(first) if first else ("> 표시할 마크다운 파일이 없습니다.", "", "")

    with gr.Blocks(title="마크다운 뷰어", fill_height=True, **BLOCKS_KW) as demo:
        # ---- 왼쪽 사이드바: 파일 선택 (접기/펴기 + 폭 조절) ----
        with gr.Sidebar(open=True, width=320, position="left", elem_id="file-sidebar"):
            gr.Markdown("### 파일")
            search = gr.Textbox(
                label="검색", placeholder="파일명 또는 본문 내용", container=True
            )
            file_list = gr.Radio(
                choices=files, value=first, label=None, container=False,
                interactive=True, elem_id="file-list",
            )
            refresh = gr.Button("목록 새로고침", variant="secondary", size="sm")
            status = gr.Markdown(f"{len(files)}개 파일")

        # ---- 본문 ----
        gr.Markdown(f"## 마크다운 뷰어\n기준 폴더: `{BASE_DIR}`")
        file_info = gr.Markdown(info)
        with gr.Tabs():
            with gr.Tab("보기"):
                rendered = gr.Markdown(body)
            with gr.Tab("원본"):
                source = gr.Code(raw, language="markdown", lines=30)

        # 파일을 고르면 본문을 갱신한다.
        file_list.change(on_select, inputs=file_list, outputs=[rendered, source, file_info])
        # 검색어를 바꾸거나 새로고침을 누르면 목록부터 다시 만든다.
        for event in (search.submit, refresh.click):
            event(
                on_refresh,
                inputs=[search, file_list],
                outputs=[file_list, rendered, source, file_info, status],
            )

    return demo


def main() -> None:
    parser = argparse.ArgumentParser(description="현재 폴더의 마크다운 파일을 웹으로 띄운다.")
    parser.add_argument("--dir", default=".", help="읽을 폴더 (기본: 현재 폴더)")
    parser.add_argument("--port", type=int, default=8080, help="포트 (기본: 8080)")
    parser.add_argument("--host", default="0.0.0.0", help="바인딩 주소 (기본: 0.0.0.0)")
    parser.add_argument("--no-share", action="store_true", help="외부 공유 링크를 만들지 않는다")
    parser.add_argument("--no-recursive", action="store_true", help="하위 폴더는 검색하지 않는다")
    parser.add_argument("--no-browser", action="store_true", help="브라우저를 자동으로 열지 않는다")
    args = parser.parse_args()

    global BASE_DIR, RECURSIVE
    BASE_DIR = Path(args.dir).expanduser().resolve()
    RECURSIVE = not args.no_recursive
    if not BASE_DIR.is_dir():
        raise SystemExit(f"폴더가 아닙니다: {BASE_DIR}")

    print(f"기준 폴더: {BASE_DIR}")
    print(f"마크다운 {len(find_markdown_files())}개 발견")

    build_ui().launch(
        server_name=args.host,          # 외부 기기 접속 허용
        server_port=args.port,          # 포트 지정
        share=not args.no_share,        # 72시간 제한 외부 공유 링크 생성
        inbrowser=not args.no_browser,  # 브라우저 자동 열기
        allowed_paths=[str(BASE_DIR)],  # 문서 안의 로컬 이미지 접근 허용
        **LAUNCH_KW,
    )


if __name__ == "__main__":
    main()
