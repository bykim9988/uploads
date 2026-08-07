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
    - 표·코드블록·```mermaid 다이어그램·수식($$...$$, $...$, \\(...\\), \\[...\\])을 모두 렌더링한다.
      mermaid는 Gradio 패키지에 동봉된 것을 쓰므로 인터넷 없이도 그려진다
      (없는 버전이면 CDN에서 받는다).
    - 왼쪽 파일 목록은 [☰ 파일 목록] 버튼이나 사이드바 화살표로 접었다 펼 수 있고,
      목록 위의 슬라이더로 폭을 조절한다.
    - share=True 가 기본이라 실행할 때마다 공개 주소(*.gradio.live)가 만들어진다.
      링크를 아는 사람은 누구나 이 폴더의 마크다운을 볼 수 있으니, 혼자 볼 때는 --no-share 를 쓴다.
      시연이 끝나면 Ctrl+C 로 닫는다.
"""

from __future__ import annotations

import argparse
import json
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


# 사이드바 여백만 손본다.
# 주의: #file-sidebar 에 overflow/resize 를 주면 안 된다. 사이드바를 여닫는 화살표 버튼은
# 사이드바 상자 '바깥'(left:100%)에 붙어 있어서, overflow 를 주는 순간 잘려 사라진다.
# 그러면 한 번 닫은 목록을 다시 열 수 없다. 폭 조절은 아래 슬라이더로 한다.
CSS = """
#file-sidebar { padding: 8px 10px 8px 8px; }
#file-sidebar .form { border: none; background: transparent; }
#file-list label { padding: 3px 6px; }   /* 파일이 많아도 한눈에 들어오게 */
"""

# 수식 구분자. Gradio 기본값은 $$...$$ 하나뿐이라 인라인 $...$ 가 그냥 글자로 나온다.
# 넷 다 등록해 블록·인라인 수식을 모두 렌더링한다. ($$ 를 $ 보다 먼저 두어야 한다.)
# 코드블록·<pre> 안의 $ 는 KaTeX가 원래 건드리지 않으므로 코드 예시는 안전하다.
LATEX_DELIMITERS = [
    {"left": "$$", "right": "$$", "display": True},
    {"left": "\\[", "right": "\\]", "display": True},
    {"left": "\\(", "right": "\\)", "display": False},
    {"left": "$", "right": "$", "display": False},
]

def bundled_mermaid_url() -> str:
    """Gradio 패키지에 함께 들어 있는 mermaid 번들의 주소. 없으면 빈 문자열."""
    assets = Path(gr.__file__).parent / "templates" / "frontend" / "assets"
    for pattern in ("mermaid.core-*.js", "mermaid-*.js"):
        hits = sorted(p for p in assets.glob(pattern) if "parser" not in p.name)
        if hits:
            return f"assets/{hits[0].name}"
    return ""


# mermaid 다이어그램. Gradio는 다이어그램을 화면에 처음 그릴 때만 mermaid를 돌리고, 그나마
# 라벨 안의 <br/> 가 섞이면 문법이 깨져 소스가 그대로 남는다. 화면이 바뀔 때마다 아직 안 그려진
# 다이어그램을 찾아 직접 그린다(낮은 버전에서 ```mermaid 코드 블록으로 남는 경우도 함께 처리).
MERMAID_JS = """
() => {
    const LOCAL = __MERMAID_LOCAL__;   // Gradio에 동봉된 mermaid (인터넷 불필요)
    const CDN = 'https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs';
    let queued = false;
    let failures = 0;          // mermaid를 계속 못 받아오면 그만 시도한다(오프라인 등).

    // mermaid 라이브러리 확보. 동봉본을 먼저 쓰고, 없을 때만 CDN에서 받는다.
    const loadMermaid = async () => {
        if (window.__mdViewerMermaid) return window.__mdViewerMermaid;
        const sources = LOCAL ? [new URL(LOCAL, document.baseURI).href, CDN] : [CDN];
        for (const src of sources) {
            try {
                const mod = await import(src);
                const lib = mod.default || mod;
                lib.initialize({ startOnLoad: false, securityLevel: 'antiscript' });
                window.__mdViewerMermaid = lib;
                return lib;
            } catch (err) {
                console.warn('mermaid 로드 실패:', src, err);
            }
        }
        return null;
    };

    // Gradio는 다이어그램 안의 <br/> 를 '진짜 줄바꿈 태그'로 바꿔 버린다. 그러면 따옴표로 묶인
    // 라벨 중간에 줄바꿈이 들어가 mermaid 문법이 깨진다. 원래 글자로 되돌린다.
    const sourceOf = (el) => {
        const html = el.innerHTML.replace(/<br\\s*\\/?>/gi, '&lt;br/&gt;');
        const decoder = document.createElement('textarea');
        decoder.innerHTML = html;
        return decoder.value;
    };

    const render = async () => {
        queued = false;
        if (failures > 5) return;

        // 낮은 Gradio 버전은 ```mermaid 를 코드 블록으로 남긴다. 먼저 컨테이너로 바꾼다.
        document.querySelectorAll('pre code.language-mermaid').forEach((code) => {
            const box = document.createElement('div');
            box.className = 'mermaid';
            box.textContent = code.textContent;
            code.closest('pre').replaceWith(box);
        });

        const pending = [...document.querySelectorAll('.mermaid:not([data-processed])')];
        if (!pending.length) return;                 // 이미 다 그려졌다.

        const mermaid = await loadMermaid();
        if (!mermaid) { failures += 1; return; }

        pending.forEach((el) => { el.textContent = sourceOf(el); });
        try {
            await mermaid.run({ nodes: pending, suppressErrors: true });
        } catch (err) {
            console.warn('mermaid 렌더링 실패:', err);
        }
    };

    const schedule = () => {
        if (queued) return;
        queued = true;
        setTimeout(render, 150);
    };
    new MutationObserver(schedule).observe(document.body, { childList: true, subtree: true });
    schedule();
}
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
        sidebar_open = gr.State(True)

        # ---- 왼쪽 사이드바: 파일 선택 (접기/펴기 + 폭 조절) ----
        with gr.Sidebar(open=True, width=320, position="left", elem_id="file-sidebar") as sidebar:
            gr.Markdown("### 파일")
            width_slider = gr.Slider(
                200, 640, value=320, step=20, label="목록 폭(px)", container=True
            )
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
        with gr.Row():
            # 사이드바를 닫아도 이 버튼은 항상 보이므로 언제든 다시 열 수 있다.
            toggle = gr.Button("☰ 파일 목록", size="sm", scale=0, min_width=130)
            gr.Markdown(f"### 마크다운 뷰어  ·  `{BASE_DIR}`")
        file_info = gr.Markdown(info)
        with gr.Tabs():
            with gr.Tab("보기"):
                rendered = gr.Markdown(body, latex_delimiters=LATEX_DELIMITERS)
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

        # 목록 열기/닫기 — 버튼과 사이드바 화살표 중 어느 쪽을 눌러도 상태가 어긋나지 않게 한다.
        toggle.click(
            lambda is_open: (gr.update(open=not is_open), not is_open),
            inputs=sidebar_open,
            outputs=[sidebar, sidebar_open],
        )
        sidebar.expand(lambda: True, outputs=sidebar_open)
        sidebar.collapse(lambda: False, outputs=sidebar_open)
        # 목록 폭 조절. 사이드바는 본문 위에 겹쳐 떠 있고, 본문을 오른쪽으로 밀어내는 여백
        # (--overlap-amount)은 Gradio가 화면을 처음 그릴 때 한 번만 계산한다. 폭을 바꾼 뒤
        # 그대로 두면 본문이 목록 아래로 파고들어 글자가 가려지므로 직접 다시 계산해 준다.
        # (폭이 부드럽게 늘어나는 동안의 중간값을 잡지 않도록 잠시 뒤 두 번 더 계산한다.)
        width_slider.change(
            lambda width: gr.update(width=int(width)), inputs=width_slider, outputs=sidebar
        ).then(
            fn=None,
            js="""() => {
                const sidebar = document.querySelector('#file-sidebar');
                const wrap = sidebar && sidebar.closest('.wrap');
                if (!wrap) return;
                const apply = () => {
                    const width = sidebar.getBoundingClientRect().width;
                    const left = wrap.getBoundingClientRect().left;
                    document.documentElement.style.setProperty(
                        '--overlap-amount', Math.max(0, width - left + 30) + 'px');
                };
                apply(); setTimeout(apply, 150); setTimeout(apply, 400);
            }""",
        )

        # mermaid 감시 시작 (화면이 처음 뜰 때 한 번만 걸어 두면 이후 문서 전환은 알아서 따라온다).
        demo.load(
            fn=None,
            js=MERMAID_JS.replace("__MERMAID_LOCAL__", json.dumps(bundled_mermaid_url())),
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
