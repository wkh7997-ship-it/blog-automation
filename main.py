#!/usr/bin/env python3
"""해외 여행지 블로그 포스팅 자동화 (AI 키워드 자동 생성 + Playwright 웹 자동화 발행)

Claude(키워드 자동 생성 + 포스팅 작성) → Pixabay(이미지) → posts/*.md 저장 → Playwright 티스토리 자동 발행
"""
from __future__ import annotations

import argparse
import html
import json
import logging
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
from playwright.sync_api import sync_playwright

# ──────────────────────────────────────────────────────────────
# 상수
# ──────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent
POSTS_DIR = BASE_DIR / "posts"
IMAGES_DIR = BASE_DIR / "images"

DEFAULT_MODEL = "claude-sonnet-5-5"
PIXABAY_API_URL = "https://pixabay.com/api/"
HTTP_TIMEOUT = 30

log = logging.getLogger("travel-blog")


# ──────────────────────────────────────────────────────────────
# 설정
# ──────────────────────────────────────────────────────────────
@dataclass
class Config:
    anthropic_key: str
    pixabay_key: str
    tistory_email: str
    tistory_password: str
    tistory_blog_url: str
    model: str


def load_config(publish: bool) -> Config:
    load_dotenv(BASE_DIR / ".env")
    cfg = Config(
        anthropic_key=os.getenv("ANTHROPIC_API_KEY", "").strip(),
        pixabay_key=os.getenv("PIXABAY_API_KEY", "").strip(),
        tistory_email=os.getenv("TISTORY_EMAIL", "").strip(),
        tistory_password=os.getenv("TISTORY_PASSWORD", "").strip(),
        tistory_blog_url=os.getenv("TISTORY_BLOG_URL", "").strip().rstrip("/"),
        model=os.getenv("ANTHROPIC_MODEL", "").strip() or DEFAULT_MODEL,
    )

    required = {"ANTHROPIC_API_KEY": cfg.anthropic_key, "PIXABAY_API_KEY": cfg.pixabay_key}
    if publish:
        required["TISTORY_EMAIL"] = cfg.tistory_email
        required["TISTORY_PASSWORD"] = cfg.tistory_password
        required["TISTORY_BLOG_URL"] = cfg.tistory_blog_url
    
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise SystemExit(
            "❌ 환경 변수(또는 .env)에 다음 값이 비어 있습니다: " + ", ".join(missing)
        )
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
                raise RuntimeError(f"Pixabay API 오류 (HTTP {resp.status_code})")

            hits = [h for h in resp.json().get("hits", []) if h["id"] not in used_ids]
            if not hits:
                continue

            hit = hits[0]
            src = hit.get("largeImageURL") or hit.get("webformatURL")
            if not src:
                continue
            used_ids.add(hit["id"])

            img_resp = requests.get(src, timeout=60)
            img_resp.raise_for_status()

            path = IMAGES_DIR / f"{slug}_{index}{Path(urlparse(src).path).suffix.lower() or '.jpg'}"
            path.write_bytes(img_resp.content)

            user = re.sub(r"[\[\]()]", "", hit.get("user") or "Pixabay").strip()
            user_url = f"https://pixabay.com/users/{quote(user)}-{hit['user_id']}/" if hit.get("user_id") else "https://pixabay.com"
            images.append(
                Image(
                    placeholder=f"{{{{IMAGE_{index}}}}}",
                    query=query,
                    alt=re.sub(r"[\[\]()]", "", f"{keyword} - {query}").strip(),
                    local_path=path,
                    user=user,
                    user_url=user_url,
                    page_url=hit.get("pageURL") or "https://pixabay.com",
                )
            )
            log.info("        ✓ '%s' → %s", query, path.name)
        except Exception as exc:
            log.warning("        ! '%s' 이미지 처리 실패: %s", query, exc)
    return images


# ──────────────────────────────────────────────────────────────
# 3) 마크다운 조립
# ──────────────────────────────────────────────────────────────
def insert_images(body_md: str, images: list[Image]) -> str:
    for img in images:
        block = (
            f"![{img.alt}]({img.local_path.absolute()})\n\n"
            f"*Image by [{img.user}]({img.user_url}) on [Pixabay]({img.page_url})*"
        )
        if img.placeholder in body_md:
            body_md = body_md.replace(img.placeholder, block)
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


# ──────────────────────────────────────────────────────────────
# 4) Playwright: 티스토리 웹 자동화 발행
# ──────────────────────────────────────────────────────────────
def tistory_publish_playwright(cfg: Config, post: dict, body_md: str) -> str:
    log.info("  [4/4] Playwright로 티스토리 자동 발행 시도 중...")
    with sync_playwright() as p:
        # headless=True 로 설정하면 백그라운드에서 실행됨 (디버깅 시 False로 변경 가능)
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1280, "height": 800})
        page = context.new_page()

        try:
            # 1. 카카오 로그인 페이지 접속
            page.goto("https://accounts.kakao.com/login?continue=https%3A%2F%2Fwww.tistory.com%2Fauth%2Flogin")
            
            # 이메일 / 비밀번호 입력
            page.fill("input[name='loginId']", cfg.tistory_email)
            page.fill("input[name='password']", cfg.tistory_password)
            page.click("button[type='submit']")
            page.wait_for_load_state("networkidle")
            
            # 2. 티스토리 글쓰기 페이지로 이동
            write_url = f"{cfg.tistory_blog_url}/manage/newpost"
            page.goto(write_url)
            
            # 페이지가 완전히 로드될 때까지 최대 10초 대기
            page.wait_for_load_state("load")
            page.wait_for_selector("#post-title-inp", timeout=10000)

            # 3. 제목 입력
            page.fill("#post-title-inp", post["title"])

            # 4. 에디터를 HTML 모드로 전환하여 마크다운 본문 주입
            # (티스토리 에디터 하단의 기본 모드 -> HTML 모드 버튼 클릭)
            try:
                # 에디터 모드 변경 버튼 클릭 시도 (에디터 UI에 따라 다를 수 있음)
                page.click("button.btn-toggle-mode", timeout=5000)
                page.wait_for_timeout(1000)
                # 코드미러 또는 텍스트어레이어에 본문 입력
                page.fill("div.CodeMirror textarea", body_md)
            except Exception:
                # 기본 모드일 경우 클립보드나 자바스크립트로 주입
                page.evaluate(f"document.querySelector('.CodeMirror textarea').value = {json.dumps(body_md)}")

            # 5. 태그 입력
            if post["tags"]:
                tag_input = page.locator("input#tagText")
                for tag in post["tags"]:
                    tag_input.fill(tag)
                    tag_input.press("Enter")

            # 6. 완료/발행 버튼 클릭
            page.click("button#publish-layer-btn")  # 발행 버튼 열기
            page.wait_for_timeout(1000)
            page.click("button#publish-btn")        # 최종 발행 버튼
            page.wait_for_timeout(5000)

            published_url = page.url
            log.info("        ✓ Playwright 발행 성공: %s", published_url)
            browser.close()
            return published_url

        except Exception as exc:
            log.error("        ✗ Playwright 발행 실패: %s", exc)
            browser.close()
            raise RuntimeError(f"웹 자동화 발행 중 오류 발생: {exc}")


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


def process_keyword(keyword: str, cfg: Config, client: anthropic.Anthropic, publish: bool) -> str:
    slug = slugify(keyword)
    images = []
    try:
        post = generate_post(client, cfg.model, keyword)
        images = fetch_images(cfg, keyword, post["image_keywords"], slug)

        body_md = insert_images(post["body_markdown"], images)

        published_url = ""
        if publish:
            published_url = tistory_publish_playwright(cfg, post, body_md)

        saved_path = save_post(slug, build_markdown_file(keyword, post, body_md, published_url))
        log.info("  💾 마크다운 저장: %s", saved_path.relative_to(BASE_DIR))
        return "published" if published_url else "saved"
    finally:
        pass


# ──────────────────────────────────────────────────────────────
# 자동화 작업 실행 함수
# ──────────────────────────────────────────────────────────────
def run_job(no_publish: bool = False, limit: int = 1) -> None:
    log.info("🔄 예약된 블로그 자동화 작업 루프 시작")
    try:
        publish = not no_publish
        cfg = load_config(publish)
        
        POSTS_DIR.mkdir(exist_ok=True)
        IMAGES_DIR.mkdir(exist_ok=True)
        client = anthropic.Anthropic(api_key=cfg.anthropic_key)
        
        results = {"published": 0, "saved": 0, "failed": 0}
        for i in range(1, limit + 1):
            log.info("━━ [실행 %d/%d]", i, limit)
            try:
                keyword = generate_random_keyword(client, cfg.model)
                status = process_keyword(keyword, cfg, client, publish)
                results[status] += 1
            except Exception as exc:
                results["failed"] += 1
                log.error("  ✗ 처리 실패 (%s): %s", type(exc).__name__, exc)

            if i < limit:
                time.sleep(3.0)

        log.info("작업 완료 — 발행 %d / 저장 %d / 실패 %d", results["published"], results["saved"], results["failed"])
    except Exception as exc:
        log.error("✗ 작업 실행 중 치명적 오류 발생: %s", exc)  # 들여쓰기 4칸 적용


# ──────────────────────────────────────────────────────────────
# 엔트리포인트
# ──────────────────────────────────────────────────────────────
def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="해외 여행지 블로그 포스팅 자동화 (Playwright 버전)")
    parser.add_argument("--once", action="store_true", help="1회만 즉시 실행 후 종료")
    parser.add_argument("--no-publish", action="store_true", help="발행 없이 마크다운만 저장")
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

    if args.once:
        run_job(args.no_publish, args.limit)
        return 0

    schedule.every().day.at("09:00").do(run_job, no_publish=args.no_publish, limit=args.limit)
    schedule.every().day.at("21:00").do(run_job, no_publish=args.no_publish, limit=args.limit)

    log.info("🤖 Playwright 웹 자동화 블로그 스케줄러가 시작되었습니다.")
    while True:
        try:
            schedule.run_pending()
            time.sleep(60)
        except KeyboardInterrupt:
            break

    return 0


if __name__ == "__main__":
    sys.exit(main())