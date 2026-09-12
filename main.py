"""What's in my 냉장고? — Streamlit refrigerator inventory manager.

Run locally: streamlit run app.py
For Streamlit Community Cloud, add OPENAI_API_KEY to app secrets to enable
generative recipe suggestions. The app still works without a key.
"""
from __future__ import annotations

import base64
import json
import os
import re
import sqlite3
from datetime import date, datetime, timedelta
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import streamlit as st

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:  # Optional: only needed for web recipe import.
    requests = None
    BeautifulSoup = None


APP_DIR = Path(__file__).parent
DB_PATH = APP_DIR / "fridge.db"
UNITS = ["개", "g", "kg", "ml", "L", "팩", "봉", "통", "장", "인분"]
DEFAULT_SHELF_LIFE = {
    "계란": 21, "우유": 7, "두부": 5, "양파": 30, "감자": 30,
    "당근": 21, "대파": 14, "버섯": 7, "닭고기": 3, "소고기": 3,
    "돼지고기": 3, "생선": 2, "치킨": 2, "밥": 2, "김치": 30,
    "요거트": 10, "토마토": 7, "사과": 14, "바나나": 5,
}


def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con


def setup_database() -> None:
    con = db()
    con.executescript("""
    CREATE TABLE IF NOT EXISTS households (
        code TEXT PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS inventory (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        household_code TEXT NOT NULL, name TEXT NOT NULL, quantity REAL NOT NULL,
        original_quantity REAL NOT NULL, unit TEXT NOT NULL, expiry_date TEXT NOT NULL,
        price REAL NOT NULL DEFAULT 0, image_b64 TEXT, is_ai_expiry INTEGER NOT NULL DEFAULT 0,
        is_leftover INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active'
    );
    CREATE TABLE IF NOT EXISTS recipes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        household_code TEXT NOT NULL, name TEXT NOT NULL, ingredients_json TEXT NOT NULL,
        instructions TEXT, image_b64 TEXT, source_url TEXT, created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS money_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, household_code TEXT NOT NULL,
        inventory_id INTEGER, kind TEXT NOT NULL, amount REAL NOT NULL,
        note TEXT NOT NULL, created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS expiry_alerts (
        inventory_id INTEGER PRIMARY KEY, household_code TEXT NOT NULL,
        last_days_left INTEGER NOT NULL, notified_at TEXT NOT NULL
    );
    """)
    con.commit()
    con.close()


def run(sql: str, values: tuple = ()) -> None:
    con = db(); con.execute(sql, values); con.commit(); con.close()


def rows(sql: str, values: tuple = ()) -> list[sqlite3.Row]:
    con = db(); result = con.execute(sql, values).fetchall(); con.close(); return result


def one(sql: str, values: tuple = ()) -> sqlite3.Row | None:
    result = rows(sql, values); return result[0] if result else None


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def household_code() -> str:
    if "household_code" not in st.session_state:
        code = "우리집-냉장고"
        run("INSERT OR IGNORE INTO households VALUES (?, ?, ?)", (code, "우리집 냉장고", now_iso()))
        st.session_state.household_code = code
    return st.session_state.household_code


def active_inventory() -> list[sqlite3.Row]:
    return rows("""SELECT * FROM inventory WHERE household_code=? AND status='active'
                   AND quantity > 0 ORDER BY expiry_date, name""", (household_code(),))


def days_left(item: sqlite3.Row) -> int:
    return (date.fromisoformat(item["expiry_date"]) - date.today()).days


def money(value: float) -> str:
    return f"₩{value:,.0f}"


def num(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def image_data(uploaded: Any) -> str | None:
    if not uploaded:
        return None
    return base64.b64encode(uploaded.getvalue()).decode("ascii")


def ingredient_matches(recipe_ingredient: dict[str, Any], stock: list[sqlite3.Row]) -> tuple[float, str]:
    """Return available quantity and unit for a case-insensitive item name."""
    name = recipe_ingredient["name"].strip().lower()
    unit = recipe_ingredient["unit"]
    candidates = [x for x in stock if x["name"].strip().lower() == name and x["unit"] == unit]
    return sum(float(x["quantity"]) for x in candidates), unit


def recipe_shortages(recipe: sqlite3.Row, stock: list[sqlite3.Row]) -> list[str]:
    shortages = []
    for ing in json.loads(recipe["ingredients_json"]):
        available, unit = ingredient_matches(ing, stock)
        needed = float(ing["quantity"])
        if available + 1e-9 < needed:
            shortages.append(f"{ing['name']} {num(needed - available)}{unit}")
    return shortages


def expiry_style(d: int) -> tuple[str, str]:
    if d <= 3:
        label = "D-DAY" if d == 0 else (f"D-{d}" if d > 0 else f"D+{abs(d)}")
        return "urgent", label
    if d <= 7: return "warning", f"D-{d}"
    return "soon", f"D-{d}"


def predicted_expiry(name: str) -> date:
    key = next((k for k in DEFAULT_SHELF_LIFE if k in name), None)
    return date.today() + timedelta(days=DEFAULT_SHELF_LIFE.get(key, 7))


def add_inventory(name: str, quantity: float, unit: str, expiry: date, price: float,
                  photo: str | None = None, ai_expiry: bool = False, leftover: bool = False) -> None:
    run("""INSERT INTO inventory (household_code,name,quantity,original_quantity,unit,expiry_date,price,image_b64,
           is_ai_expiry,is_leftover,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (household_code(), name.strip(), quantity, quantity, unit, expiry.isoformat(), price, photo,
         int(ai_expiry), int(leftover), now_iso()))


def change_quantity(item_id: int, new_quantity: float) -> None:
    run("UPDATE inventory SET quantity=? WHERE id=? AND household_code=?", (max(new_quantity, 0), item_id, household_code()))


def record_event(item: sqlite3.Row, kind: str, used_quantity: float, note: str) -> float:
    """Return value credited to a saved/wasted action, prorated by amount."""
    unit_price = float(item["price"]) / max(float(item["original_quantity"]), 0.00001)
    amount = unit_price * used_quantity
    run("INSERT INTO money_events (household_code,inventory_id,kind,amount,note,created_at) VALUES (?,?,?,?,?,?)",
        (household_code(), item["id"], kind, amount, note, now_iso()))
    return amount


def consume_item(item: sqlite3.Row, amount: float, reason: str = "manual") -> float:
    amount = min(amount, float(item["quantity"]))
    new_quantity = float(item["quantity"]) - amount
    change_quantity(item["id"], new_quantity)
    saved = 0.0
    if days_left(item) <= 14:
        saved = record_event(item, "saved", amount, f"{item['name']} 사용 ({reason})")
    return saved


def use_recipe(recipe: sqlite3.Row) -> tuple[list[str], list[dict[str, Any]], float]:
    """Consume FEFO stock for recipe. Caller must have checked shortages."""
    stock = active_inventory()
    messages, undo, saved_total = [], [], 0.0
    for ing in json.loads(recipe["ingredients_json"]):
        remaining = float(ing["quantity"])
        matches = [x for x in stock if x["name"].strip().lower() == ing["name"].strip().lower() and x["unit"] == ing["unit"]]
        for item in matches:
            take = min(remaining, float(item["quantity"]))
            if take > 0:
                saved_total += consume_item(item, take, f"{recipe['name']} 요리")
                undo.append({"id": item["id"], "quantity": take})
                remaining -= take
            if remaining <= 0: break
        messages.append(f"{ing['name']} {num(ing['quantity'])}{ing['unit']}")
    return messages, undo, saved_total


def undo_recipe(action: dict[str, Any]) -> None:
    for change in action["changes"]:
        item = one("SELECT quantity FROM inventory WHERE id=? AND household_code=?", (change["id"], household_code()))
        if item:
            run("UPDATE inventory SET quantity=quantity+? WHERE id=? AND household_code=?",
                (change["quantity"], change["id"], household_code()))
    event_ids = action.get("event_ids", [])
    if event_ids:
        placeholders = ",".join("?" for _ in event_ids)
        run(f"DELETE FROM money_events WHERE household_code=? AND id IN ({placeholders})",
            tuple([household_code(), *event_ids]))


def expiry_alerts_due() -> list[sqlite3.Row]:
    """Create in-app notices at first sighting, then roughly every three days.

    A Streamlit app cannot wake itself to deliver operating-system push messages;
    this makes alerts persistent and visible whenever a family member opens it.
    """
    due = []
    for item in [x for x in active_inventory() if 0 <= days_left(x) <= 14]:
        previous = one("SELECT last_days_left FROM expiry_alerts WHERE inventory_id=?", (item["id"],))
        if previous is None or int(previous["last_days_left"]) - days_left(item) >= 3:
            run("""INSERT INTO expiry_alerts (inventory_id,household_code,last_days_left,notified_at)
                   VALUES (?,?,?,?) ON CONFLICT(inventory_id) DO UPDATE SET
                   last_days_left=excluded.last_days_left, notified_at=excluded.notified_at""",
                (item["id"], household_code(), days_left(item), now_iso()))
            due.append(item)
    return due


def recipe_from_url(url: str) -> dict[str, Any]:
    if not requests or not BeautifulSoup:
        raise ValueError("웹 레시피 가져오기 기능을 사용하려면 requirements.txt의 패키지를 설치해 주세요.")
    parsed = urlparse(url)
    if parsed.scheme not in {"https", "http"}:
        raise ValueError("http 또는 https 주소를 입력해 주세요.")
    try:
        response = requests.get(url, timeout=15, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
            "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.7",
        })
        response.raise_for_status()
    except requests.RequestException:
        return {
            "name": f"{parsed.netloc}에서 가져온 레시피", "ingredients": [], "instructions": "",
            "notice": "이 블로그는 자동 읽기를 제한하고 있어요. 아래 입력칸에 재료 부분을 복사해 붙여 넣어 주세요.",
        }
    soup = BeautifulSoup(response.text, "html.parser")
    candidates = []
    for tag in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(tag.get_text())
            candidates.extend(payload if isinstance(payload, list) else [payload])
        except json.JSONDecodeError:
            pass
    def find_recipe(node: Any) -> dict[str, Any] | None:
        if isinstance(node, dict):
            typ = node.get("@type", [])
            if "Recipe" in (typ if isinstance(typ, list) else [typ]): return node
            for value in node.values():
                found = find_recipe(value)
                if found: return found
        elif isinstance(node, list):
            for value in node:
                found = find_recipe(value)
                if found: return found
        return None
    recipe = find_recipe(candidates)
    if recipe:
        return {"name": recipe.get("name", "가져온 레시피"),
                "ingredients": recipe.get("recipeIngredient", []),
                "instructions": recipe.get("recipeInstructions", ""), "notice": ""}
    # Many Korean blog platforms omit Recipe JSON-LD. Use the title and visible
    # text instead, then give the user a reviewable editable recipe form.
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    title_tag = soup.select_one('meta[property="og:title"]') or soup.title
    title = title_tag.get("content", "") if title_tag and title_tag.name == "meta" else (title_tag.get_text(" ", strip=True) if title_tag else "")
    text = "\n".join(line.strip() for line in soup.get_text("\n").splitlines() if line.strip())
    extracted = recipe_from_blog_text(text, url)
    extracted["name"] = title or extracted["name"]
    extracted["notice"] = "블로그의 일반 본문에서 재료를 추출했어요. 저장 전 수량과 단위를 확인해 주세요."
    return extracted


def parse_web_ingredients(raw: list[str]) -> list[dict[str, Any]]:
    parsed = []
    unit_re = "|".join(re.escape(u) for u in UNITS)
    for line in raw:
        text = re.sub(r"\s+", " ", unescape(str(line))).strip()
        match = re.match(rf"(?:[-•*]\s*)?([0-9]+(?:/[0-9]+|\.[0-9]+)?)\s*({unit_re})?\s*(.+)", text)
        if match:
            qty, unit, name = match.groups()
            quantity = _as_quantity(qty)
            if quantity:
                parsed.append({"name": name.strip(" :,-"), "quantity": quantity, "unit": unit or "개"})
            continue
        # Korean blogs commonly write “양파 1개” rather than “1개 양파”.
        reverse = re.match(rf"(?:[-•*]\s*)?(.{{1,35}}?)\s*[:：,-]?\s*([0-9]+(?:/[0-9]+|\.[0-9]+)?)\s*({unit_re})\b", text)
        if reverse:
            name, qty, unit = reverse.groups()
            quantity = _as_quantity(qty)
            if quantity and len(name.strip()) > 1:
                parsed.append({"name": name.strip(" :,-"), "quantity": quantity, "unit": unit})
    unique: dict[tuple[str, str], dict[str, Any]] = {}
    for ingredient in parsed:
        unique.setdefault((ingredient["name"], ingredient["unit"]), ingredient)
    return list(unique.values())[:6]


def _as_quantity(value: str) -> float | None:
    try:
        if "/" in value:
            top, bottom = value.split("/", 1)
            return float(top) / float(bottom)
        return float(value)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def recipe_from_blog_text(text: str, source_url: str = "") -> dict[str, Any]:
    lines = [line.strip() for line in re.split(r"[\n\r]+", text) if line.strip()]
    ingredients = parse_web_ingredients(lines)
    title = lines[0][:70] if lines else "가져온 블로그 레시피"
    return {"name": title, "ingredients": ingredients, "instructions": "\n".join(lines[:80]),
            "source_url": source_url, "notice": ""}


def smart_recipe_suggestions(items: list[sqlite3.Row]) -> list[dict[str, str]]:
    """Use an OpenAI key when available; preserve a useful local fallback."""
    item_text = ", ".join(f"{x['name']} {num(x['quantity'])}{x['unit']} (D{days_left(x)})" for x in items)
    api_key = st.secrets.get("OPENAI_API_KEY", os.getenv("OPENAI_API_KEY", ""))
    if api_key:
        try:
            from openai import OpenAI
            prompt = ("당신은 알뜰한 한식 가정 요리 도우미입니다. 다음 냉장고 재료, 특히 유통기한이 가까운 것을 "
                      "우선 소진하는 맛있는 요리 3가지를 한국어로 제안하세요. 남은 음식이 있으면 재탄생 아이디어도 포함하세요. "
                      "각 항목은 '제목 | 핵심 재료 | 1문장 조리법' 형식 한 줄로 답하세요. 재료: " + item_text)
            model = st.secrets.get("OPENAI_MODEL", os.getenv("OPENAI_MODEL", "gpt-5-mini"))
            result = OpenAI(api_key=api_key).responses.create(model=model, input=prompt, store=False)
            lines = [x.strip(" -•") for x in result.output_text.splitlines() if "|" in x]
            return [{"title": p[0].strip(), "detail": " · ".join(p[1:]).strip()} for line in lines[:3] if len((p := line.split("|"))) >= 2]
        except Exception:
            pass
    leftovers = [x["name"] for x in items if x["is_leftover"]]
    closest = [x["name"] for x in sorted(items, key=days_left)[:3]]
    main = ", ".join(closest) or "냉장고 재료"
    ideas = [
        {"title": "냉장고 털이 볶음밥", "detail": f"{main}을 잘게 썰어 밥과 함께 볶고 계란으로 마무리해 보세요."},
        {"title": "따뜻한 한 그릇 전골", "detail": f"{main}을 육수 또는 물에 넣고 끓여 빠르게 소진해 보세요."},
    ]
    if leftovers:
        ideas.insert(0, {"title": f"남은 {leftovers[0]} 재탄생 샌드위치", "detail": "잘게 찢어 채소·소스와 섞고 빵 또는 또띠아에 넣어 드세요."})
    return ideas[:3]


def set_page(page: str) -> None:
    st.session_state.page = page
    if page == "home":
        st.query_params.clear()
    else:
        st.query_params["page"] = page


def top_bar(back: bool = True) -> None:
    left, _, right = st.columns([2, 5, 2])
    with left:
        st.caption(f"📅 {date.today().strftime('%Y년 %m월 %d일')}")
        if back: st.button("← 처음으로", on_click=set_page, args=("home",), key="go_home")
    with right:
        linked = household_code() != "우리집-냉장고"
        st.button("가족 연동됨" if linked else "가족 연동", key="family_top",
                  type="secondary" if linked else "primary", on_click=lambda: set_page("family"))


def page_home() -> None:
    st.markdown("<div class='home-date'>📅 " + date.today().strftime("%Y년 %m월 %d일") + "</div>", unsafe_allow_html=True)
    st.markdown("<h1 class='app-title'>What's in my 냉장고?</h1>", unsafe_allow_html=True)
    st.markdown("<p class='home-subtitle'>냉장고 속 식재료를 끝까지 맛있게 관리해요.</p>", unsafe_allow_html=True)
    alerts = expiry_alerts_due()
    if alerts:
        names = ", ".join(f"{x['name']} (D-{days_left(x)})" for x in alerts)
        st.warning(f"유통기한 알림: {names}. 임박 재료를 먼저 사용해 보세요.")
    st.markdown("""
      <div class="fridge" aria-label="냉장고 메뉴">
        <a class="fridge-door top-left" href="?page=inventory"><span class="handle"></span><span class="door-icon">🥬</span><span class="door-name">식재료 / 음식</span></a>
        <a class="fridge-door top-right" href="?page=recipes"><span class="handle"></span><span class="door-icon">🍳</span><span class="door-name">레시피</span></a>
        <a class="fridge-door bottom-left" href="?page=savings"><span class="handle"></span><span class="door-icon">💰</span><span class="door-name">이번달 식비 절약</span></a>
        <a class="fridge-door bottom-right" href="?page=expiry"><span class="handle"></span><span class="door-icon">⏰</span><span class="door-name">유통기한 임박</span></a>
      </div>
    """, unsafe_allow_html=True)
    linked = household_code() != "우리집-냉장고"
    if linked:
        st.markdown("<style>button[data-testid='stBaseButton-primary'] {background:#8a8a8a !important; border-color:#8a8a8a !important;}</style>", unsafe_allow_html=True)
    _, family_col = st.columns([7, 2])
    with family_col:
        st.button("가족 연동됨" if linked else "가족 연동", key="family_home",
                  type="primary", on_click=set_page, args=("family",), use_container_width=True)


def item_card(item: sqlite3.Row) -> None:
    d = days_left(item)
    with st.container(border=True):
        a, b = st.columns([1, 2])
        with a:
            if item["image_b64"]:
                st.image(base64.b64decode(item["image_b64"]), use_container_width=True)
            else:
                st.markdown("<div class='food-image'>" + ("🍗" if item["is_leftover"] else "🥕") + "</div>", unsafe_allow_html=True)
        with b:
            st.subheader(item["name"])
            st.write(f"**{num(item['quantity'])}{item['unit']}** 남음")
            tag = " · 🤖 AI 예측" if item["is_ai_expiry"] else " · 라벨 확인"
            st.caption(f"유통기한 {item['expiry_date']} ({'D-DAY' if d == 0 else f'D{d}'}){tag}")
            if item["price"]: st.caption(f"구매가 {money(item['price'])}")
            if item["is_leftover"]: st.caption("♻️ 남은 음식")
            if st.button("수정 / 사용 / 삭제", key=f"detail_{item['id']}"):
                st.session_state.selected_item = item["id"]
                st.session_state.item_editor_open = True
    if st.session_state.get("item_editor_open") and st.session_state.get("selected_item") == item["id"]:
        item_editor(item)


def item_editor(item: sqlite3.Row) -> None:
    st.markdown("#### " + item["name"] + " 관리")
    with st.form(f"edit_{item['id']}"):
        amount = st.number_input("사용량", min_value=0.0, max_value=float(item["quantity"]), value=0.0, step=0.5,
                                 help="레시피 정량과 다르게 사용했을 때 직접 입력하세요.")
        c1, c2, c3 = st.columns(3)
        use = c1.form_submit_button("사용 처리")
        delete = c2.form_submit_button("완전 소진")
        close = c3.form_submit_button("닫기")
        if use and amount > 0:
            saved = consume_item(item, amount, "직접 사용")
            st.success(f"{item['name']} {num(amount)}{item['unit']}을 차감했어요." + (f" {money(saved)} 절약!" if saved else ""))
            st.session_state.item_editor_open = False; st.rerun()
        if delete:
            change_quantity(item["id"], 0)
            st.success("완전 소진으로 처리했어요."); st.session_state.item_editor_open = False; st.rerun()
        if close: st.session_state.item_editor_open = False; st.rerun()
    if st.button("🗑️ 폐기 처리", key=f"trash_{item['id']}"):
        record_event(item, "wasted", float(item["quantity"]), f"{item['name']} 폐기")
        change_quantity(item["id"], 0)
        st.warning(f"{item['name']}을 폐기 처리했어요. {money(float(item['price']) * float(item['quantity']) / max(float(item['original_quantity']), .001))}이 낭비 금액에 반영됐습니다.")
        st.session_state.item_editor_open = False; st.rerun()


def add_item_form() -> None:
    st.markdown("### 식재료 / 음식 추가")
    with st.form("add_item", clear_on_submit=True):
        name = st.text_input("음식 / 재료명 *", placeholder="예: 양파, 남은 치킨")
        qcol, ucol = st.columns([2, 1])
        quantity = qcol.number_input("개수 / 양 *", min_value=0.1, value=1.0, step=0.5)
        unit = ucol.selectbox("단위 *", UNITS)
        expiry_known = st.toggle("유통기한을 알고 있어요", value=True)
        expiry = st.date_input("유통기한 *", value=date.today() + timedelta(days=7), disabled=not expiry_known)
        price = st.number_input("구매 가격 (선택)", min_value=0, value=0, step=500,
                                help="절약/낭비 금액 계산에 사용됩니다.")
        leftover = st.checkbox("남은 음식이에요 (재탄생 레시피 추천)")
        photo = st.file_uploader("사진 (선택)", type=["jpg", "jpeg", "png", "webp"], key="add_photo")
        if st.form_submit_button("냉장고에 추가", type="primary"):
            if not name.strip(): st.error("음식 또는 재료명을 입력해 주세요.")
            else:
                final_expiry = expiry if expiry_known else predicted_expiry(name)
                captured = photo or st.session_state.get("camera_photo")
                add_inventory(name, quantity, unit, final_expiry, price, image_data(captured), not expiry_known, leftover)
                st.success(f"{name}을(를) 등록했어요." + (" 🤖 예상 유통기한을 표시했어요." if not expiry_known else ""))


def page_inventory() -> None:
    top_bar(); st.title("🥬 식재료 / 음식")
    st.caption("카드를 눌러 부분 사용량을 직접 입력하거나, 완전 소진·폐기를 기록하세요.")
    action1, action2 = st.columns(2)
    if action1.button("📷 카메라", use_container_width=True): st.session_state.camera_open = not st.session_state.get("camera_open", False)
    if action2.button("＋ 추가", use_container_width=True, type="primary"): st.session_state.add_item_open = not st.session_state.get("add_item_open", False)
    if st.session_state.get("camera_open"):
        st.info("사진을 찍은 뒤, 아래 추가 양식에서 이름·수량·유통기한을 입력해 등록하세요.")
        camera_photo = st.camera_input("식재료 사진 찍기")
        if camera_photo: st.session_state.camera_photo = camera_photo
    if st.session_state.get("add_item_open"):
        add_item_form()
    items = active_inventory()
    if not items: st.info("아직 냉장고가 비어 있어요. ‘추가’로 첫 식재료를 등록해 보세요.")
    for pair_start in range(0, len(items), 2):
        cols = st.columns(2)
        for col, item in zip(cols, items[pair_start:pair_start + 2]):
            with col: item_card(item)


def add_recipe_form(prefill: dict[str, Any] | None = None) -> None:
    st.markdown("### 레시피 등록")
    name_default = prefill.get("name", "") if prefill else ""
    ingredients_default = prefill.get("ingredients", []) if prefill else []
    with st.form("recipe_form", clear_on_submit=True):
        name = st.text_input("레시피 이름 *", value=name_default)
        source = st.text_input("출처 URL (선택)", value=prefill.get("source_url", "") if prefill else "")
        instruction = st.text_area("조리 방법 (선택)", value=str(prefill.get("instructions", "")) if prefill else "")
        photo = st.file_uploader("레시피 사진 (선택)", type=["jpg", "jpeg", "png", "webp"], key="recipe_photo")
        st.caption("재료와 필요한 양을 입력하세요. 단위가 냉장고 재고와 같아야 자동 차감됩니다.")
        ingredient_list: list[dict[str, Any]] = []
        for idx in range(6):
            default = ingredients_default[idx] if idx < len(ingredients_default) else {}
            c1, c2, c3 = st.columns([3, 1, 1])
            ing_name = c1.text_input("재료명", value=default.get("name", ""), key=f"rname_{idx}", label_visibility="collapsed", placeholder="재료명")
            ing_qty = c2.number_input("양", min_value=0.0, value=float(default.get("quantity", 0.0)), step=0.5, key=f"rqty_{idx}", label_visibility="collapsed")
            unit_index = UNITS.index(default["unit"]) if default.get("unit") in UNITS else 0
            ing_unit = c3.selectbox("단위", UNITS, index=unit_index, key=f"runit_{idx}", label_visibility="collapsed")
            if ing_name.strip() and ing_qty > 0: ingredient_list.append({"name": ing_name.strip(), "quantity": ing_qty, "unit": ing_unit})
        if st.form_submit_button("레시피 저장", type="primary"):
            if not name.strip() or not ingredient_list: st.error("레시피 이름과 재료를 한 가지 이상 입력해 주세요.")
            else:
                run("INSERT INTO recipes (household_code,name,ingredients_json,instructions,image_b64,source_url,created_at) VALUES (?,?,?,?,?,?,?)",
                    (household_code(), name.strip(), json.dumps(ingredient_list, ensure_ascii=False), instruction, image_data(photo), source, now_iso()))
                st.success("레시피를 저장했어요."); st.session_state.add_recipe_open = False; st.rerun()


def page_recipes() -> None:
    top_bar(); st.title("🍳 레시피")
    c1, c2 = st.columns(2)
    if c1.button("＋ 직접 추가", type="primary", use_container_width=True): st.session_state.add_recipe_open = True
    if c2.button("🌐 인터넷에서 가져오기", use_container_width=True): st.session_state.import_recipe_open = not st.session_state.get("import_recipe_open", False)
    if st.session_state.get("import_recipe_open"):
        url = st.text_input("레시피 페이지 URL", placeholder="https://...")
        pasted = st.text_area("블로그 재료/본문 붙여넣기 (선택)",
                              placeholder="접근이 제한된 블로그라면 ‘재료’ 부분을 복사해 붙여 넣으세요. 예: 양파 1개\n계란 2개")
        if st.button("가져오기") and (url or pasted):
            try:
                extracted = recipe_from_blog_text(pasted, url) if pasted.strip() else recipe_from_url(url)
                raw_ingredients = extracted["ingredients"]
                parsed = raw_ingredients if raw_ingredients and isinstance(raw_ingredients[0], dict) else parse_web_ingredients(raw_ingredients)
                if not parsed: st.warning("재료의 수량/단위를 자동 해석하지 못했어요. 아래에서 직접 입력해 주세요.")
                if extracted.get("notice"): st.info(extracted["notice"])
                st.session_state.recipe_prefill = {"name": extracted["name"], "ingredients": parsed,
                                                    "instructions": str(extracted["instructions"]), "source_url": url}
                st.session_state.add_recipe_open = True
            except Exception as exc: st.error(f"가져오지 못했어요: {exc}")
    if st.session_state.get("add_recipe_open"):
        add_recipe_form(st.session_state.pop("recipe_prefill", None))
    stock = active_inventory()
    recipes = rows("SELECT * FROM recipes WHERE household_code=? ORDER BY created_at DESC", (household_code(),))
    if not recipes: st.info("자주 만드는 요리를 등록해 두면 한 번의 확인으로 재료를 차감할 수 있어요.")
    for recipe in recipes:
        ingredients = json.loads(recipe["ingredients_json"])
        shortages = recipe_shortages(recipe, stock)
        with st.container(border=True):
            image_col, text_col = st.columns([1, 2])
            with image_col:
                if recipe["image_b64"]: st.image(base64.b64decode(recipe["image_b64"]), use_container_width=True)
                else: st.markdown("<div class='food-image'>🍳</div>", unsafe_allow_html=True)
            with text_col:
                st.subheader(recipe["name"])
                st.write(" · ".join(f"{x['name']} {num(x['quantity'])}{x['unit']}" for x in ingredients))
                if shortages: st.error("부족한 재료: " + ", ".join(shortages))
                else: st.success("현재 재고로 요리할 수 있어요.")
                if st.button("요리하기", key=f"cook_{recipe['id']}", disabled=bool(shortages), type="primary"):
                    st.session_state.confirm_recipe = recipe["id"]
        if st.session_state.get("confirm_recipe") == recipe["id"]:
            st.warning("정말로 요리하시겠습니까? 재료가 차감됩니다.")
            yes, no = st.columns(2)
            if yes.button("네, 요리했어요", key=f"confirm_yes_{recipe['id']}", type="primary"):
                current = one("SELECT * FROM recipes WHERE id=?", (recipe["id"],))
                shortages_now = recipe_shortages(current, active_inventory())
                if shortages_now: st.error("재고가 변경되어 다시 확인이 필요해요: " + ", ".join(shortages_now))
                else:
                    started_at = now_iso()
                    messages, changes, saved = use_recipe(current)
                    event_ids = [x["id"] for x in rows(
                        "SELECT id FROM money_events WHERE household_code=? AND kind='saved' AND created_at>=? AND note=?",
                        (household_code(), started_at, f"{current['name']} 요리"))]
                    st.session_state.last_recipe_action = {"changes": changes, "event_ids": event_ids}
                    st.session_state.confirm_recipe = None
                    st.success(" · ".join(messages) + "가 차감되었습니다.")
                    if saved: st.info(f"임박 재료 사용으로 {money(saved)}을 절약했어요.")
                    st.rerun()
            if no.button("취소", key=f"confirm_no_{recipe['id']}"):
                st.session_state.confirm_recipe = None; st.rerun()
    action = st.session_state.get("last_recipe_action")
    if action:
        st.info("방금 레시피 사용으로 재료를 자동 차감했습니다. 잘못 눌렀다면 실행 취소할 수 있어요.")
        if st.button("↩ 실행 취소 (Undo)"):
            undo_recipe(action); del st.session_state.last_recipe_action
            st.success("재료와 절약 금액을 되돌렸어요."); st.rerun()


def finish_expiring(item: sqlite3.Row, wasted: bool) -> None:
    quantity = float(item["quantity"])
    kind = "wasted" if wasted else "saved"
    label = "폐기" if wasted else "다 먹음"
    credited = record_event(item, kind, quantity, f"{item['name']} {label}")
    change_quantity(item["id"], 0)
    st.session_state.expiry_message = f"{item['name']}을(를) {label} 처리했어요. {money(credited)} 반영!"


def page_expiry() -> None:
    top_bar(); st.title("⏰ 유통기한 임박")
    st.caption("D-14 이내 재료를 남은 날짜가 짧은 순서로 보여드려요. 빨강(D-3) · 주황(D-7) · 노랑(D-14)")
    if message := st.session_state.pop("expiry_message", None): st.success(message)
    items = [x for x in active_inventory() if days_left(x) <= 14]
    if not items: st.success("앞으로 2주 안에 임박한 식재료가 없어요. 훌륭해요!")
    for item in items:
        d = days_left(item); style, label = expiry_style(d)
        with st.container(border=True):
            st.markdown(f"<div class='expiry-card {style}'><b>{label}</b> · {item['name']} · {num(item['quantity'])}{item['unit']}<br><span>유통기한 {item['expiry_date']}</span></div>", unsafe_allow_html=True)
            use, waste = st.columns(2)
            if use.button("다 먹음", key=f"done_{item['id']}", use_container_width=True):
                finish_expiring(item, False); st.rerun()
            if waste.button("버림", key=f"waste_{item['id']}", use_container_width=True):
                finish_expiring(item, True); st.rerun()
    st.divider(); st.subheader("🤖 임박 재료로 만드는 오늘의 메뉴")
    if items:
        for idea in smart_recipe_suggestions(items):
            st.markdown(f"<div class='suggestion'><b>{idea['title']}</b><br>{idea['detail']}</div>", unsafe_allow_html=True)


def page_savings() -> None:
    top_bar(); st.title("💰 이번달 식비 절약")
    month_prefix = date.today().strftime("%Y-%m")
    events = rows("SELECT * FROM money_events WHERE household_code=? AND substr(created_at,1,7)=?", (household_code(), month_prefix))
    saved = sum(float(x["amount"]) for x in events if x["kind"] == "saved")
    wasted = sum(float(x["amount"]) for x in events if x["kind"] == "wasted")
    st.markdown(f"<div class='saving-hero'>이번달에 절약한 식비!<strong>{money(saved)}</strong><span>유통기한 임박 식재료를 사용하며 아낀 금액이에요.</span></div>", unsafe_allow_html=True)
    a, b = st.columns(2)
    a.metric("절약", money(saved))
    b.metric("음식물 쓰레기로 버린 돈", money(wasted), delta=None, delta_color="inverse")
    st.subheader("이번 달 처리 이력")
    if not events: st.caption("임박 식재료를 사용하거나 폐기하면 이곳에 금액이 기록됩니다.")
    for event in reversed(events):
        icon = "✅" if event["kind"] == "saved" else "🗑️"
        st.write(f"{icon} {event['note']} — **{money(float(event['amount']))}**")


def page_family() -> None:
    top_bar(); st.title("👨‍👩‍👧 가족 연동")
    st.write("같은 가족 코드를 입력한 구성원은 하나의 냉장고 재고, 레시피, 절약 이력을 함께 봅니다.")
    current = household_code()
    st.info(f"현재 가족 코드: **{current}**")
    with st.form("family_form"):
        code = st.text_input("가족 코드", placeholder="예: 우리집-냉장고")
        name = st.text_input("냉장고 이름", placeholder="예: 엄마네 냉장고")
        if st.form_submit_button("연동하기", type="primary"):
            clean = re.sub(r"[^0-9A-Za-z가-힣_-]", "", code).strip()
            if len(clean) < 2: st.error("두 글자 이상 가족 코드를 입력해 주세요.")
            else:
                run("INSERT OR IGNORE INTO households VALUES (?, ?, ?)", (clean, name.strip() or "가족 냉장고", now_iso()))
                st.session_state.household_code = clean
                st.success("가족 냉장고에 연동했어요!")
    if current != "우리집-냉장고" and st.button("내 기본 냉장고로 돌아가기"):
        st.session_state.household_code = "우리집-냉장고"; st.rerun()


def inject_css() -> None:
    st.markdown("""
    <style>
      @import url('https://fonts.googleapis.com/css2?family=Nanum+Myeongjo:wght@400;700&family=Pretendard&display=swap');
      .stApp { background: #fffaf5; color: #171717; }
      .app-title { text-align:center; margin: 1.2rem 0 .2rem; font-family:'Nanum Myeongjo','HCR Batang',serif; font-size:clamp(1.9rem,7vw,4rem); color:#121212; }
      .home-subtitle { text-align:center; color:#6d625b; margin-bottom:1rem; }
      .home-date { position:absolute; top:1.2rem; left:1.4rem; color:#544a44; font-size:.9rem; }
      .fridge { max-width:720px; margin:0 auto 1.2rem; padding:clamp(9px,2vw,18px); display:grid; grid-template-columns:1fr 1fr; gap:8px; background:linear-gradient(135deg,#e6ecef,#aebac0); border:9px solid #c5d0d5; border-radius:28px; box-shadow:11px 13px 0 #99a6ad; }
      .fridge-door { position:relative; min-height:clamp(138px,22vw,245px); display:flex; flex-direction:column; align-items:center; justify-content:center; gap:.55rem; overflow:hidden; text-decoration:none !important; color:#191919 !important; background:linear-gradient(135deg,#ffffff 0%,#f3f6f7 68%,#dbe3e6 100%); border:1px solid #b8c4c9; border-radius:12px; box-shadow:inset -10px -8px 16px rgba(115,137,145,.17), inset 4px 4px 8px rgba(255,255,255,.9); transition:transform .26s ease, filter .26s ease; }
      .fridge-door:hover, .fridge-door:focus { transform:perspective(780px) rotateY(-9deg) translateY(-2px); filter:brightness(1.03); }
      .fridge-door .handle { position:absolute; width:7px; height:40%; right:13px; top:30%; border-radius:7px; background:linear-gradient(90deg,#aebbc1,#eff4f5,#93a2a8); box-shadow:1px 1px 2px #7f8b90; }
      .fridge-door.top-right .handle, .fridge-door.bottom-right .handle { left:13px; right:auto; }
      .door-icon { font-size:clamp(2rem,5vw,3.6rem); line-height:1; transition:transform .25s ease; }
      .fridge-door:hover .door-icon { transform:scale(1.12); }
      .door-name { max-width:80%; text-align:center; font-family:'Nanum Myeongjo','HCR Batang',serif; font-size:clamp(.88rem,2.5vw,1.4rem); font-weight:700; line-height:1.35; }
      .food-image { min-height:94px; display:grid; place-items:center; font-size:4rem; background:#fff7e8; border-radius:10px; }
      .expiry-card { padding: .85rem 1rem; border-radius:10px; font-size:1.1rem; color:#21110c; }
      .expiry-card span { font-size:.85rem; } .expiry-card.urgent {background:#f8b2ad;} .expiry-card.warning {background:#ffd198;} .expiry-card.soon {background:#ffefad;}
      .saving-hero { text-align:center; font-family:'Nanum Myeongjo',serif; font-size:clamp(1.6rem,5vw,2.8rem); font-weight:700; padding:1.5rem 1rem; background:#fff; border-radius:18px; }
      .saving-hero strong { display:block; color:#1e7a47; font-size:clamp(2.2rem,8vw,4.5rem); margin:.4rem; } .saving-hero span { display:block; font: .9rem sans-serif; color:#6b635f; }
      .suggestion { padding:.85rem 1rem; margin:.5rem 0; background:#fff; border-left:6px solid #ef9c4e; border-radius:8px; }
      @media(max-width: 480px) { .home-date {position:static; margin:.7rem 0 0;} .fridge {border-width:5px; border-radius:18px; box-shadow:5px 6px 0 #99a6ad; gap:5px; padding:7px;} .fridge-door {border-radius:8px;} .fridge-door .handle {right:7px; width:5px;} .fridge-door.top-right .handle,.fridge-door.bottom-right .handle {left:7px;} .block-container {padding-top:.5rem;} }
    </style>
    """, unsafe_allow_html=True)


def main() -> None:
    st.set_page_config(page_title="What's in my 냉장고?", page_icon="🧊", layout="centered")
    setup_database(); household_code(); inject_css()
    requested_page = st.query_params.get("page")
    if requested_page in {"home", "inventory", "recipes", "expiry", "savings", "family"}:
        st.session_state.page = requested_page
    page = st.session_state.get("page", "home")
    {"home": page_home, "inventory": page_inventory, "recipes": page_recipes,
     "expiry": page_expiry, "savings": page_savings, "family": page_family}.get(page, page_home)()


if __name__ == "__main__":
    main()
