#!/usr/bin/env python3
"""해외 여행지 블로그 포스팅 자동화 (AI 키워드 자동 생성 버전)

Claude(키워드 자동 생성 + 포스팅 작성) → Pixabay(이미지) → posts/*.md 저장 → 티스토리 발행

사용법 (로컬 테스트 시):
    python main.py             # 상시 대기하며 스케줄에 따라 실행
    python main.py --once      # 1회만 즉시 실행 후 종료
    python main.py --no-publish    # 마크다운만 저장 (티스토리 발행 생략)
"""
from __future__ import annotations

import argparse
import html
import json
import logging
import mimetypes
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlparse

import anthropic
import requests
from dotenv import load_dotenv
import schedule

# ──────────────────────────────────────────────────────────────
# 상수
# ──────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent
POSTS_DIR = BASE_DIR / "posts"
IMAGES_DIR = BASE_DIR / "images"

DEFAULT_MODEL = "claude-sonnet-5-5"

PIXABAY_API_URL = "https://pixabay.com/api/"
TISTORY_WRITE_URL = "https://www.tistory.com/apis/post/write"
TISTORY_ATTACH_URL = "https://www.tistory.com/apis/post/attach"
HTTP_TIMEOUT = 30

log = logging.getLogger("travel-blog")


# ──────────────────────────────────────────────────────────────
# 설정
# ──────────────────────────────────────────────────────────────
@dataclass
class Config:
    anthropic_key: str
    pixabay_key: str
    tistory_token: str
    tistory_blog: str
    model: str
    visibility: str  # 0: 비공개(초안 용도), 1: 보호, 3: 발행
    category_id: str


def load_config(publish: bool) -> Config:
    load_dotenv(BASE_DIR / ".env")
    cfg = Config(
        anthropic_key=os.getenv("ANTHROPIC_API_KEY", "").strip(),
        pixabay_key=os.getenv("PIXABAY_API_KEY", "").strip(),
        tistory_token=os.getenv("TISTORY_ACCESS_TOKEN", "").strip(),
        tistory_blog=os.getenv("TISTORY_BLOG_NAME", "").strip(),
        model=os.getenv("ANTHROPIC_MODEL", "").strip() or DEFAULT_MODEL,
        visibility=os.getenv("TISTORY_VISIBILITY", "").strip() or "0",
        category_id=os.getenv("TISTORY_CATEGORY_ID", "").strip(),
    )

    required = {"ANTHROPIC_API_KEY": cfg.anthropic_key, "PIXABAY_API_KEY": cfg.pixabay_key}
    if publish:
        required["TISTORY_ACCESS_TOKEN"] = cfg.tistory_token
        required["TISTORY_BLOG_NAME"] = cfg.tistory_blog
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise SystemExit(
            "❌ 환경 변수(또는 .env)에 다음 값이 비어 있습니다: " + ", ".join(missing)
        )
    if cfg.visibility not in {"0", "1", "3"}:
        raise SystemExit("❌ TISTORY_VISIBILITY 는 0(비공개), 1(보호), 3(발행) 중 하나여야 합니다.")
    return cfg


# ──────────────────────────────────────────────────────────────
# 1) Claude: 키워드 자동 생성 및 포스팅 생성
# ──────────────────────────────────────────────────────────────
def generate_random_keyword(client: anthropic.Anthropic, model: str) -> str:
    log.info("  [0/4] AI가 무작위 여행지/맛집 키워드를 생성하는 중...")
    prompt = """
    전 세계의 인기 있거나 매력적인 여행지, 관광명소, 맛집, 도시 관련 블로그 포스팅 키워드를 딱 1개만 생성해줘.
    유럽, 아시아, 미주, 남미, 오세아니아, 국내 등 다양한 지역을 골고루 포함해줘.
    
    [응답 조건]
    - 오직 키워드 단어 또는 문구만 출력할 것 (예: "파리 3박 4일 추천 코스", "다낭 가성비 해산물 맛집 모음", "바르셀로나 가볼 만한 곳 TOP 5")
    - 따옴표, 불필요한 설명, 인사말 절대 금지
    """
    response = client.messages.create(
        model=model,
        max_tokens=100,
        messages=[{"role": "user", "content": prompt}],
    )
    keyword = "".join(b.text for b in response.content if b.type == "text").strip()
    # 혹시 따옴표가 포함되어 있다면 제거
    keyword = keyword.strip('"\'')
    log.info("        ✓ 생성된 키워드: %s", keyword)
    return keyword


SYSTEM_PROMPT = """\
당신은 해외 여행 전문 블로거이자 SEO 에디터입니다. 한국어 독자를 위해 검색 노출에 유리하고 \
실제로 도움이 되는 여행 포스팅을 작성합니다.

작성 규칙:
- 제목: 타겟 키워드를 앞쪽에 넣고 40자 이내로 작성합니다. 낚시성 표현은 피합니다.
- 메타 설명: 120~150자, 키워드를 자연스럽게 포함합니다.
- 본문은 마크다운으로 작성합니다. H1(#)은 쓰지 말고 ## 와 ### 만 사용합니다. 표는 쓰지 않습니다.
- 구성: 도입부(키워드 포함, 독자가 얻는 것) → 핵심 내용(소제목 4~6개) → 자주 묻는 질문(FAQ 3개) → 마무리.
- 분량: 공백 포함 약 2,500~3,500자.
- 일정/맛집/팁은 구체적이고 실용적으로 쓰되, 가격·운영시간·교통편처럼 바뀔 수 있는 정보는 \
확신이 없으면 '약', '대략'을 붙이고 본문 마지막에 '방문 전 최신 정보를 꼭 확인하세요' 안내를 넣습니다.
- 존재 여부가 불확실한 가게나 장소의 이름을 지어내지 않습니다. 확실하지 않으면 지역·음식 종류 단위로 안내합니다.
- '지도' 같은 키워드는 실제 지도를 만들 수 없으므로, 지역(구역)별로 묶어 동선을 설명하는 방식으로 풀어 씁니다.
- 이미지 검색 키워드(영문 2~3개): Pixabay 에서 검색할 짧은 영어 구문(예: "Tokyo Shibuya crossing night").
- 본문에서 이미지가 들어갈 자리에 {{IMAGE_1}}, {{IMAGE_2}}, {{IMAGE_3}} 형태의 자리표시자를 \
각각 한 줄에 단독으로 넣습니다. 자리표시자 개수는 이미지 검색 키워드 개수와 같아야 합니다.
"""

POST_TOOL = {
    "name": "submit_post",
    "description": "완성된 블로그 포스팅을 제출합니다.",
    "input_schema": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "SEO 최적화된 포스팅 제목"},
            "meta_description": {"type": "string", "description": "120~150자 메타 설명"},
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "티스토리 태그 5~8개 (한국어)",
            },
            "body_markdown": {
                "type": "string",
                "description": "마크다운 본문. 이미지 자리에 {{IMAGE_1}} 형태의 자리표시자 포함",
            },
            "image_keywords": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Pixabay 검색용 영문 키워드 2~3개",
            },
        },
        "required": ["title", "meta_description", "tags", "body_markdown", "image_keywords"],
    },
}


def _extract_post(response) -> dict | None:
    for block in response.content:
        if block.type == "tool_use" and block.name == POST_TOOL["name"]:
            return block.input
    text = "".join(b.text for b in response.content if b.type == "text")
    first, last = text.find("{"), text.rfind("}")
    if first != -1 and last > first:
        try:
            data = json.loads(text[first : last + 1])
            if isinstance(data, dict) and "title" in data and "body_markdown" in data:
                return data
        except ValueError:
            pass
    return None


def generate_post(client: anthropic.Anthropic, model: str, keyword: str) -> dict:
    log.info("  [1/4] Claude 로 포스팅 생성 중... (model=%s)", model)
    started = time.time()
    prompt = (
        f"타겟 키워드: {keyword}\n"
        "이 키워드로 SEO 최적화된 여행 포스팅을 작성해 주세요.\n"
        f"작성이 끝나면 반드시 {POST_TOOL['name']} 도구를 호출해서 결과를 제출하세요. "
        "도구 호출 외에 다른 설명은 쓰지 마세요."
    )

    post = None
    for attempt in (1, 2):
        response = client.messages.create(
            model=model,
            max_tokens=8000,
            system=SYSTEM_PROMPT,
            tools=[POST_TOOL],
            messages=[{"role": "user", "content": prompt}],
        )
        if response.stop_reason == "max_tokens":
            raise RuntimeError("응답이 max_tokens 에서 잘렸습니다. 다시 시도해 주세요.")
        post = _extract_post(response)
        if post:
            break
        log.warning("        ! 도구 호출 결과를 받지 못했습니다 (시도 %d/2)", attempt)
    if not post:
        raise RuntimeError("Claude 응답에서 포스팅 데이터를 찾지 못했습니다.")

    for field in ("title", "body_markdown"):
        if not str(post.get(field, "")).strip():
            raise RuntimeError(f"생성 결과에 '{field}' 가 비어 있습니다.")
    post["image_keywords"] = [k.strip() for k in post.get("image_keywords", []) if k.strip()][:3]
    post["tags"] = [t.strip().lstrip("#") for t in post.get("tags", []) if t.strip()]

    log.info(
        "        ✓ 생성 완료 (%.1f초, 제목: %s, 본문 %d자, 이미지 키워드 %s)",
        time.time() - started, post["title"], len(post["body_markdown"]), post["image_keywords"],
    )
    return post


# ──────────────────────────────────────────────────────────────
# 2) Pixabay: 이미지 검색 및 다운로드
# ──────────────────────────────────────────────────────────────
@dataclass
class Image:
    placeholder: str
    query: str
    alt: str
    local_path: Path
    user: str
    user_url: str
    page_url: str
    tistory_url: str = ""


def _mask(text: str, secret: str) -> str:
    return text.replace(secret, "***") if secret else text


def fetch_images(cfg: Config, keyword: str, queries: list[str], slug: str) -> list[Image]:
    log.info("  [2/4] Pixabay 이미지 검색/다운로드 중... (%d개)", len(queries))
    IMAGES_DIR.mkdir(exist_ok=True)
    images: list[Image] = []
    used_ids: set[int] = set()

    for index, query in enumerate(queries, start=1):
        try:
            resp = requests.get(
                PIXABAY_API_URL,
                params={
                    "key": cfg.pixabay_key,
                    "q": query[:100],
                    "image_type": "photo",
                    "orientation": "horizontal",
                    "safesearch": "true",
                    "per_page": 10,
                },
                timeout=HTTP_TIMEOUT,
            )
            if resp.status_code != 200:
                raise RuntimeError(f"Pixabay API 오류 (HTTP {resp.status_code}): {resp.text[:100]!r}")

            hits = [h for h in resp.json().get("hits", []) if h["id"] not in used_ids]
            if not hits:
                log.warning("        ! '%s' 검색 결과 없음 → 건너뜀", query)
                continue

            hit = hits[0]
            src = hit.get("largeImageURL") or hit.get("webformatURL")
            if not src:
                log.warning("        ! '%s' 결과에 이미지 URL 이 없음 → 건너뜀", query)
                continue
            used_ids.add(hit["id"])

            img_resp = requests.get(src, timeout=60)
            img_resp.raise_for_status()

            path = IMAGES_DIR / f"{slug}_{index}{Path(urlparse(src).path).suffix.lower() or '.jpg'}"
            path.write_bytes(img_resp.content)

            user = _clean_alt(hit.get("user") or "Pixabay")
            user_url = f"https://pixabay.com/users/{quote(user)}-{hit['user_id']}/" if hit.get("user_id") else "https://pixabay.com"
            images.append(
                Image(
                    placeholder=f"{{{{IMAGE_{index}}}}}",
                    query=query,
                    alt=_clean_alt(f"{keyword} - {query}"),
                    local_path=path,
                    user=user,
                    user_url=user_url,
                    page_url=hit.get("pageURL") or "https://pixabay.com",
                )
            )
            log.info("        ✓ '%s' → %s (%.0fKB, by %s)", query, path.name, len(img_resp.content) / 1024, user)
        except (requests.RequestException, RuntimeError, KeyError, ValueError) as exc:
            log.warning("        ! '%s' 이미지 처리 실패: %s", query, _mask(str(exc), cfg.pixabay_key))
    return images


def _clean_alt(text: str) -> str:
    return re.sub(r"[\[\]()]", "", text).strip()


# ──────────────────────────────────────────────────────────────
# 3) 마크다운 조립 및 HTML 변환
# ──────────────────────────────────────────────────────────────
def insert_images(body_md: str, images: list[Image], for_publish: bool) -> str:
    for img in images:
        if img.tistory_url:
            url = img.tistory_url
        elif not for_publish:
            url = f"../images/{img.local_path.name}"
        else:
            log.warning("        ! %s 는 티스토리에 업로드되지 않아 발행 본문에서 제외합니다.", img.local_path.name)
            continue

        block = (
            f"![{img.alt}]({url})\n\n"
            f"*Image by [{img.user}]({img.user_url}) on [Pixabay]({img.page_url})*"
        )
        if img.placeholder in body_md:
            body_md = body_md.replace(img.placeholder, block)
        elif not for_publish:
            log.warning("        ! 본문에 %s 자리표시자가 없어 이미지를 넣지 못했습니다.", img.placeholder)
    body_md = re.sub(r"[ \t]*\{\{IMAGE_\d+\}\}[ \t]*\n?", "", body_md)
    return re.sub(r"\n{3,}", "\n\n", body_md)


def build_markdown_file(keyword: str, post: dict, body_md: str, published_url: str) -> str:
    def q(value: str) -> str:
        return json.dumps(value, ensure_ascii=False)

    front = [
        "---",
        f"title: {q(post['title'])}",
        f"description: {q(post.get('meta_description', ''))}",
        f"keyword: {q(keyword)}",
        f"tags: [{', '.join(q(t) for t in post['tags'])}]",
        f"date: {datetime.now():%Y-%m-%d %H:%M:%S}",
        f"published: {'true' if published_url else 'false'}",
    ]
    if published_url:
        front.append(f"tistory_url: {q(published_url)}")
    front.append("---")
    return "\n".join(front) + f"\n\n# {post['title']}\n\n{body_md.strip()}\n"


_INLINE = re.compile(
    r"!\[([^\]]*)\]\(([^)\s]+)\)"
    r"|\[([^\]]+)\]\(([^)\s]+)\)"
    r"|\*\*(.+?)\*\*"
    r"|\*(.+?)\*"
    r"|`([^`]+)`"
)


def _esc(text: str) -> str:
    return html.escape(text, quote=True)


def _inline(text: str) -> str:
    out, pos = [], 0
    for m in _INLINE.finditer(text):
        out.append(_esc(text[pos:m.start()]))
        if m.group(2) is not None:
            out.append(f'<img src="{_esc(m.group(2))}" alt="{_esc(m.group(1))}" style="max-width:100%;" />')
        elif m.group(4) is not None:
            out.append(f'<a href="{_esc(m.group(4))}" target="_blank" rel="noopener">{_esc(m.group(3))}</a>')
        elif m.group(5) is not None:
            out.append(f"<strong>{_inline(m.group(5))}</strong>")
        elif m.group(6) is not None:
            out.append(f"<em>{_inline(m.group(6))}</em>")
        else:
            out.append(f"<code>{_esc(m.group(7))}</code>")
        pos = m.end()
    out.append(_esc(text[pos:]))
    return "".join(out)


def markdown_to_html(md: str) -> str:
    html_parts: list[str] = []
    paragraph: list[str] = []
    list_type: str | None = None

    def flush_paragraph() -> None:
        if paragraph:
            html_parts.append(f"<p>{_inline(' '.join(paragraph))}</p>")
            paragraph.clear()

    def close_list() -> None:
        nonlocal list_type
        if list_type:
            html_parts.append(f"</{list_type}>")
            list_type = None

    for raw in md.splitlines():
        line = raw.strip()
        if not line:
            flush_paragraph()
            close_list()
            continue

        heading = re.match(r"^(#{1,6})\s+(.*)$", line)
        bullet = re.match(r"^[-*]\s+(.*)$", line)
        numbered = re.match(r"^\d+[.)]\s+(.*)$", line)
        quote = re.match(r"^>\s?(.*)$", line)

        if heading:
            flush_paragraph(); close_list()
            level = min(len(heading.group(1)) + (1 if len(heading.group(1)) == 1 else 0), 6)
            html_parts.append(f"<h{level}>{_inline(heading.group(2))}</h{level}>")
        elif re.match(r"^(-{3,}|\*{3,})$", line):
            flush_paragraph(); close_list()
            html_parts.append("<hr />")
        elif bullet or numbered:
            flush_paragraph()
            wanted = "ul" if bullet else "ol"
            if list_type != wanted:
                close_list()
                html_parts.append(f"<{wanted}>")
                list_type = wanted
            html_parts.append(f"<li>{_inline((bullet or numbered).group(1))}</li>")
        elif quote:
            flush_paragraph(); close_list()
            html_parts.append(f"<blockquote>{_inline(quote.group(1))}</blockquote>")
        else:
            close_list()
            paragraph.append(line)

    flush_paragraph()
    close_list()
    return "\n".join(html_parts)


# ──────────────────────────────────────────────────────────────
# 4) 티스토리 발행
# ──────────────────────────────────────────────────────────────
TISTORY_HINT = (
    "티스토리 Open API 는 서비스가 중단되었다는 보고가 있어, 토큰이 맞더라도 호출이 실패할 수 있습니다. "
    "이 경우 posts/ 의 마크다운은 정상 저장되며, 내용을 티스토리 에디터에 직접 붙여넣어 발행할 수 있습니다."
)


def _tistory_body(resp: requests.Response) -> dict:
    try:
        payload = resp.json()
    except ValueError:
        raise RuntimeError(f"티스토리 응답이 JSON 이 아닙니다 (HTTP {resp.status_code}): {resp.text[:150]!r}")
    body = payload.get("tistory", payload)
    status = str(body.get("status", resp.status_code))
    if resp.status_code != 200 or status != "200":
        message = body.get("error_message") or resp.text[:150]
        raise RuntimeError(f"티스토리 API 오류 (HTTP {resp.status_code}, status={status}): {message}")
    return body


def tistory_upload_image(cfg: Config, path: Path) -> str:
    with path.open("rb") as fh:
        resp = requests.post(
            TISTORY_ATTACH_URL,
            data={"access_token": cfg.tistory_token, "blogName": cfg.tistory_blog, "output": "json"},
            files={"uploadedfile": (path.name, fh, mimetypes.guess_type(path.name)[0] or "image/jpeg")},
            timeout=60,
        )
    url = _tistory_body(resp).get("url")
    if not url:
        raise RuntimeError("티스토리 이미지 업로드 응답에 url 이 없습니다.")
    return url


def tistory_publish(cfg: Config, title: str, content_html: str, tags: list[str]) -> str:
    data = {
        "access_token": cfg.tistory_token,
        "output": "json",
        "blogName": cfg.tistory_blog,
        "title": title,
        "content": content_html,
        "visibility": cfg.visibility,
        "tag": ",".join(tags),
    }
    if cfg.category_id:
        data["category"] = cfg.category_id
    resp = requests.post(TISTORY_WRITE_URL, data=data, timeout=HTTP_TIMEOUT)
    body = _tistory_body(resp)
    return body.get("url") or f"https://{cfg.tistory_blog}.tistory.com/{body.get('postId', '')}"


# ──────────────────────────────────────────────────────────────
# 키워드 1개 처리
# ──────────────────────────────────────────────────────────────
def slugify(text: str) -> str:
    return re.sub(r"[^\w가-힣]+", "-", text).strip("-")[:60] or "post"


def save_post(slug: str, content: str) -> Path:
    POSTS_DIR.mkdir(exist_ok=True)
    path = POSTS_DIR / f"{datetime.now():%Y-%m-%d}_{slug}.md"
    if path.exists():
        path = POSTS_DIR / f"{datetime.now():%Y-%m-%d_%H%M%S}_{slug}.md"
    path.write_text(content, encoding="utf-8")
    return path


def process_keyword(keyword: str, cfg: Config, client: anthropic.Anthropic, publish: bool, keep_images: bool) -> str:
    slug = slugify(keyword)
    images: list[Image] = []
    try:
        post = generate_post(client, cfg.model, keyword)
        images = fetch_images(cfg, keyword, post["image_keywords"], slug)

        if publish and images:
            log.info("  [3/4] 티스토리에 이미지 업로드 중...")
            for img in images:
                try:
                    img.tistory_url = tistory_upload_image(cfg, img.local_path)
                    log.info("        ✓ %s 업로드 완료", img.local_path.name)
                except (requests.RequestException, RuntimeError) as exc:
                    log.warning("        ! %s 업로드 실패: %s", img.local_path.name, exc)
        else:
            log.info("  [3/4] 이미지 업로드 생략")

        published_url = ""
        if publish:
            log.info("  [4/4] 티스토리 발행 중... (visibility=%s)", cfg.visibility)
            try:
                publish_md = insert_images(post["body_markdown"], images, for_publish=True)
                published_url = tistory_publish(cfg, post["title"], markdown_to_html(publish_md), post["tags"])
                log.info("        ✓ 발행 성공: %s", published_url)
            except (requests.RequestException, RuntimeError) as exc:
                log.error("        ✗ 티스토리 발행 실패: %s", exc)
                log.error("          %s", TISTORY_HINT)
        else:
            log.info("  [4/4] 티스토리 발행 생략 (--no-publish)")

        body_md = insert_images(post["body_markdown"], images, for_publish=False)
        saved_path = save_post(slug, build_markdown_file(keyword, post, body_md, published_url))
        log.info("  💾 마크다운 저장: %s", saved_path.relative_to(BASE_DIR))
        return "published" if published_url else "saved"
    finally:
        if not keep_images:
            for img in images:
                if img.tistory_url:
                    img.local_path.unlink(missing_ok=True)


# ──────────────────────────────────────────────────────────────
# 자동화 작업 실행 함수 (스케줄러 연동용)
# ──────────────────────────────────────────────────────────────
def run_job(no_publish: bool = False, keep_images: bool = False, limit: int = 1) -> None:
    log.info("🔄 예약된 블로그 자동화 작업 루프 시작")
    try:
        publish = not no_publish
        cfg = load_config(publish)
        
        POSTS_DIR.mkdir(exist_ok=True)
        IMAGES_DIR.mkdir(exist_ok=True)
        client = anthropic.Anthropic(api_key=cfg.anthropic_key)
        
        log.info("AI 키워드 자동 생성 및 포스팅 발행 시작 (지정 횟수: %d개)", limit)

        results = {"published": 0, "saved": 0, "failed": 0}
        for i in range(1, limit + 1):
            log.info("━━ [실행 %d/%d]", i, limit)
            try:
                # 실행될 때마다 AI가 새로운 여행지 키워드 생성
                keyword = generate_random_keyword(client, cfg.model)
                
                status = process_keyword(keyword, cfg, client, publish, keep_images)
                results[status] += 1
            except KeyboardInterrupt:
                log.warning("사용자 중단")
                break
            except anthropic.APIStatusError as exc:
                results["failed"] += 1
                log.error("  ✗ Anthropic API 오류 (HTTP %s): %s", exc.status_code, exc.message)
            except anthropic.APIConnectionError as exc:
                results["failed"] += 1
                log.error("  ✗ Anthropic 연결 실패: %s", exc)
            except Exception as exc:
                results["failed"] += 1
                log.error("  ✗ 처리 실패 (%s): %s", type(exc).__name__, exc)

            if i < limit:
                time.sleep(3.0)

        log.info(
            "작업 완료 — 발행 %d / 마크다운만 저장 %d / 실패 %d",
            results["published"], results["saved"], results["failed"],
        )
    except Exception as exc:
        log.error("✗ 작업 실행 중 치명적 오류 발생: %s", exc)


# ──────────────────────────────────────────────────────────────
# 엔트리포인트
# ──────────────────────────────────────────────────────────────
def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="해외 여행지 블로그 포스팅 자동화 (AI 키워드 자동 생성)")
    parser.add_argument("--once", action="store_true", help="스케줄러 대기 없이 즉시 1회 실행 후 종료")
    parser.add_argument("--no-publish", action="store_true", help="티스토리 발행 없이 마크다운만 저장")
    parser.add_argument("--keep-images", action="store_true", help="티스토리에 업로드한 이미지도 삭제하지 않음")
    parser.add_argument("--limit", type=int, default=1, help="한 번 실행 시 생성할 포스팅 개수")
    parser.add_argument("-v", "--verbose", action="store_true", help="디버그 로그 출력")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    if args.once:
        run_job(args.no_publish, args.keep_images, args.limit)
        return 0

    # 24시간 백그라운드 서버 모드 (스케줄러 설정)
    schedule.every().day.at("09:00").do(run_job, no_publish=args.no_publish, keep_images=args.keep_images, limit=args.limit)
    schedule.every().day.at("21:00").do(run_job, no_publish=args.no_publish, keep_images=args.keep_images, limit=args.limit)

    log.info("🤖 AI 자동 키워드 블로그 스케줄러가 시작되었습니다. (매일 09:00, 21:00 자동 실행 대기 중)")
    log.info("   (즉시 1회 테스트하려면 'python main.py --once' 명령어를 사용하세요)")

    while True:
        try:
            schedule.run_pending()
            time.sleep(60)
        except KeyboardInterrupt:
            log.info("사용자에 의해 스케줄러가 종료되었습니다.")
            break

    return 0


if __name__ == "__main__":
    sys.exit(main())