"""
Final robust Streamlit app with Supabase persistence (safe upsert) and SQLite fallback.
- Supabase preferred (upsert + non-destructive save). If Supabase unavailable or save fails, falls back to local SQLite.
- Auto-migrate old locations_db.json -> Supabase (preferred) or SQLite.
- Geocoding: success-only cache, address_changed clears stale coords and rejects very short inputs.
- Form/session_state initialization to avoid stale coordinates.
- Save operations show clear success/failure UI, and a JSON download backup is available.
- Schedule generation logic preserved (greedy + fallback + route ordering).
Notes:
- On Streamlit Cloud: set SUPABASE_URL and SUPABASE_KEY in app secrets.
- Make sure a Supabase table `locations(id integer primary key, data jsonb not null)` exists.
"""
from __future__ import annotations
import streamlit as st
import pandas as pd
import json
import os
import logging
from datetime import datetime, timedelta
import urllib.parse
import urllib.request
import geopy.distance
import tempfile
import time
from pathlib import Path
import shutil
import sqlite3

# Optional Supabase client
try:
    from supabase import create_client
except Exception:
    create_client = None

# Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# Paths / constants
BASE_DIR = Path(__file__).resolve().parent
JSON_OLD_PATH = BASE_DIR / "locations_db.json"
SQLITE_DB = BASE_DIR / "locations.db"
SQLITE_BAK = BASE_DIR / "locations.db.bak"
UNK_DISTANCE = 999.0

# -------------------------
# Supabase client helper
# -------------------------
def get_supabase_client():
    if create_client is None:
        return None
    url = None
    key = None
    try:
        if isinstance(st.secrets, dict):
            url = st.secrets.get("SUPABASE_URL") or url
            key = st.secrets.get("SUPABASE_KEY") or key
    except Exception:
        pass
    url = url or os.getenv("SUPABASE_URL")
    key = key or os.getenv("SUPABASE_KEY")
    if not url or not key:
        return None
    try:
        client = create_client(url, key)
        return client
    except Exception as e:
        logging.error(f"Supabase client init failed: {e}")
        return None

SUPABASE = get_supabase_client()

# -------------------------
# Rule normalization
# -------------------------
def normalize_rule_type(user_label: str) -> str:
    if not user_label:
        return "none"
    mapping = {
        "特になし": "none", "特なし": "none", "none": "none",
        "○日まで": "until", "until": "until",
        "○日〜○日の間": "range", "range": "range",
        "○日ぴったり": "exact", "exact": "exact",
    }
    return mapping.get(user_label, "none")

def denormalize_rule_type(internal_key: str) -> str:
    inv = {"none": "特になし", "until": "○日まで", "range": "○日〜○日の間", "exact": "○日ぴったり"}
    return inv.get(internal_key, "特になし")

# -------------------------
# SQLite helpers
# -------------------------
def init_sqlite(db_path: Path):
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS locations (
            id INTEGER PRIMARY KEY,
            data TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()

def load_data_sqlite(db_path: Path | None = None):
    path = db_path or SQLITE_DB
    try:
        if not path.exists():
            init_sqlite(path)
            return []
        init_sqlite(path)
        conn = sqlite3.connect(path)
        cur = conn.cursor()
        cur.execute("SELECT data FROM locations ORDER BY id")
        rows = cur.fetchall()
        conn.close()
        items = []
        for (d,) in rows:
            try:
                obj = json.loads(d)
            except Exception:
                continue
            obj.setdefault("rules", [])
            obj.setdefault("intervals", [])
            obj["lat"] = float(obj.get("lat", 0.0) or 0.0)
            obj["lon"] = float(obj.get("lon", 0.0) or 0.0)
            for r in obj["rules"]:
                r["type"] = normalize_rule_type(r.get("type", "none"))
            items.append(obj)
        logging.info(f"Loaded {len(items)} locations from sqlite {path}")
        return items
    except Exception as e:
        logging.error(f"SQLite load error: {e}")
        return []

def save_data_sqlite(locations: list, db_path: Path | None = None) -> bool:
    path = db_path or SQLITE_DB
    try:
        if path.exists():
            try:
                shutil.copy2(path, SQLITE_BAK)
            except Exception:
                pass
        tmp_db = Path(str(path.parent)) / f"{path.stem}_{int(time.time())}.tmp.db"
        init_sqlite(tmp_db)
        conn = sqlite3.connect(tmp_db)
        cur = conn.cursor()
        for loc in locations:
            lid = int(loc.get("id", 0)) if loc.get("id") is not None else None
            cur.execute("INSERT INTO locations (id, data) VALUES (?, ?)", (lid, json.dumps(loc, ensure_ascii=False)))
        conn.commit()
        conn.close()
        os.replace(tmp_db, path)
        logging.info(f"Saved {len(locations)} locations to sqlite {path}")
        return True
    except Exception as e:
        logging.error(f"SQLite save error: {e}")
        return False

# -------------------------
# Supabase helpers (safe upsert)
# -------------------------
def load_data_supabase():
    if SUPABASE is None:
        return None
    try:
        res = SUPABASE.table("locations").select("id,data").order("id", {"ascending": True}).execute()
        data = getattr(res, "data", None) or (res.get("data") if isinstance(res, dict) else None)
        if not data:
            return []
        items = []
        for row in data:
            obj = row.get("data") if isinstance(row, dict) and "data" in row else row
            if isinstance(obj, str):
                try:
                    obj = json.loads(obj)
                except Exception:
                    continue
            if not isinstance(obj, dict):
                continue
            obj.setdefault("rules", [])
            obj.setdefault("intervals", [])
            obj["lat"] = float(obj.get("lat", 0.0) or 0.0)
            obj["lon"] = float(obj.get("lon", 0.0) or 0.0)
            for r in obj["rules"]:
                r["type"] = normalize_rule_type(r.get("type", "none"))
            items.append(obj)
        logging.info(f"Loaded {len(items)} locations from Supabase")
        return items
    except Exception as e:
        logging.error(f"Supabase load error: {e}")
        return None

def save_data_supabase_safe(locations: list) -> bool | None:
    """
    Upsert then delete stale IDs. Return True on success, False on failure.
    Do not perform destructive global delete before inserts.
    """
    if SUPABASE is None:
        return None
    try:
        # fetch existing ids
        res = SUPABASE.table("locations").select("id").execute()
        existing_rows = getattr(res, "data", None) or (res.get("data") if isinstance(res, dict) else None) or []
        existing_ids = {int(r["id"]) for r in existing_rows if isinstance(r, dict) and "id" in r}
        to_upsert = []
        new_ids = set()
        for loc in locations:
            lid = int(loc.get("id", 0))
            new_ids.add(lid)
            to_upsert.append({"id": lid, "data": loc})
        CHUNK = 100
        for i in range(0, len(to_upsert), CHUNK):
            chunk = to_upsert[i:i+CHUNK]
            try:
                # prefer upsert
                SUPABASE.table("locations").upsert(chunk).execute()
            except Exception:
                # fallback to insert (may error on conflict)
                SUPABASE.table("locations").insert(chunk).execute()
        # delete stale
        ids_to_delete = list(existing_ids - new_ids)
        if ids_to_delete:
            for i in range(0, len(ids_to_delete), CHUNK):
                chunk = ids_to_delete[i:i+CHUNK]
                SUPABASE.table("locations").delete().in_("id", chunk).execute()
        logging.info(f"Supabase safe save completed: upserted {len(to_upsert)}, deleted {len(ids_to_delete)}")
        return True
    except Exception as e:
        logging.error(f"Supabase safe save error: {e}")
        return False

# -------------------------
# High-level load/save
# -------------------------
def load_data():
    sup = load_data_supabase()
    if sup is not None:
        return sup
    return load_data_sqlite()

def save_data(locations: list | None = None) -> bool:
    locs = locations if locations is not None else getattr(st.session_state, "locations", [])
    if SUPABASE is not None:
        ok = save_data_supabase_safe(locs)
        if ok:
            return True
        logging.warning("Supabase save failed; falling back to SQLite")
    return save_data_sqlite(locs)

# -------------------------
# JSON -> backend migration
# -------------------------
def migrate_json_on_start():
    if not JSON_OLD_PATH.exists():
        return
    try:
        with open(JSON_OLD_PATH, "r", encoding="utf-8") as f:
            items = json.load(f)
    except Exception as e:
        logging.warning(f"Failed to read old JSON: {e}")
        return
    if SUPABASE is not None:
        try:
            to_upsert = [{"id": int(loc.get("id", 0)), "data": loc} for loc in items]
            CHUNK = 100
            for i in range(0, len(to_upsert), CHUNK):
                SUPABASE.table("locations").upsert(to_upsert[i:i+CHUNK]).execute()
            JSON_OLD_PATH.rename(JSON_OLD_PATH.with_suffix(".json.migrated"))
            logging.info("Migrated old JSON to Supabase")
            return
        except Exception as e:
            logging.warning(f"Migration to Supabase failed: {e}")
    try:
        save_data_sqlite(items)
        JSON_OLD_PATH.rename(JSON_OLD_PATH.with_suffix(".json.migrated"))
        logging.info("Migrated old JSON to SQLite")
    except Exception as e:
        logging.warning(f"Migration to SQLite failed: {e}")

migrate_json_on_start()

# -------------------------
# Geocoding
# -------------------------
GEO_CACHE: dict = {}  # address -> (lat, lon, ts)

def clear_geo_cache():
    GEO_CACHE.clear()

def _get_lat_lon_ai(address: str) -> tuple[float, float]:
    if not address:
        return 0.0, 0.0
    try:
        encoded = urllib.parse.quote(address)
        url = f"https://msearch.gsi.go.jp/address-search/AddressSearch?q={encoded}"
        req = urllib.request.Request(url, headers={"User-Agent": "MoneyCollectionScheduler_v3"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data and len(data) > 0:
                lon, lat = data[0]["geometry"]["coordinates"]
                return float(lat), float(lon)
    except Exception as e:
        logging.debug(f"Geocode error: {e}")
    return 0.0, 0.0

def get_lat_lon_ai_cached(address: str) -> tuple[float, float]:
    if not address:
        return 0.0, 0.0
    cached = GEO_CACHE.get(address)
    if cached:
        return cached[0], cached[1]
    lat, lon = _get_lat_lon_ai(address)
    if lat != 0.0 or lon != 0.0:
        GEO_CACHE[address] = (lat, lon, time.time())
    return lat, lon

# -------------------------
# Distance & rules
# -------------------------
def calculate_geopy_distance(p1, p2) -> float:
    if p1 == (0.0, 0.0) or p2 == (0.0, 0.0):
        return UNK_DISTANCE
    return geopy.distance.geodesic(p1, p2).km

def check_date_rule(rule, day_num) -> bool:
    if not rule or not rule.get("val"):
        return True
    rtype = normalize_rule_type(rule.get("type", "none"))
    val = rule.get("val", "")
    try:
        if rtype == "range":
            s, e = map(int, val.split("-"))
            return s <= day_num <= e
        elif rtype == "until":
            return day_num <= int(val)
        elif rtype == "exact":
            return day_num == int(val)
    except Exception as e:
        logging.warning(f"Date rule parse error: {e}")
        return True
    return True

def check_interval_rule(task, day, history_days) -> bool:
    span_rule = next((span for span in task.get("intervals", []) if span["to"] == task["step"]), None)
    if span_rule:
        for hist in history_days.get(task["loc_id"], []):
            if hist["step"] == span_rule["from"]:
                days_passed = abs((day - hist["day"]).days)
                if days_passed < span_rule["span"]:
                    return False
    return True

# -------------------------
# choose_fallback_day (safe)
# -------------------------
def choose_fallback_day(all_days, holiday_set, task, current_schedule=None, history_days=None):
    if not all_days:
        raise ValueError("all_days is empty")
    def is_same_loc_scheduled(day):
        if not current_schedule:
            return False
        ds = day.strftime("%Y-%m-%d")
        for t in current_schedule.get(ds, []):
            if t.get("loc_id") == task.get("loc_id"):
                return True
        return False
    for day in all_days:
        day_str = day.strftime("%Y-%m-%d")
        if day_str in holiday_set:
            continue
        w = day.weekday()
        if w == 5 and not task.get("sat", True):
            continue
        if w == 6 and not task.get("sun", False):
            continue
        if not check_date_rule(task.get("rule", {}), day.day):
            continue
        if is_same_loc_scheduled(day):
            continue
        if history_days is not None and not check_interval_rule(task, day, history_days):
            continue
        return day, False
    for day in all_days:
        day_str = day.strftime("%Y-%m-%d")
        if day_str in holiday_set:
            continue
        if is_same_loc_scheduled(day):
            continue
        return day, False
    logging.warning(f"Forced fallback for loc_id={task.get('loc_id')}, step={task.get('step')}")
    return all_days[0], True

# ==========================================
# Streamlit UI
# ==========================================
st.set_page_config(page_title="集金スケジュール管理", layout="centered")
st.title("💰 集金スケジュール管理 (Supabase-safe)")

# load initial state
if "locations" not in st.session_state:
    st.session_state.locations = load_data()
if "editing_id" not in st.session_state:
    st.session_state.editing_id = None
if "schedule_results" not in st.session_state:
    st.session_state.schedule_results = None

def address_changed(key_prefix: str):
    addr = st.session_state.get(f"{key_prefix}_address", "").strip()
    # clear if empty
    if not addr:
        st.session_state[f"{key_prefix}_lat"] = 0.0
        st.session_state[f"{key_prefix}_lon"] = 0.0
        st.session_state[f"{key_prefix}_geocoded"] = False
        return
    # reject very short inputs to avoid false matches
    if len(addr) < 3:
        st.session_state[f"{key_prefix}_lat"] = 0.0
        st.session_state[f"{key_prefix}_lon"] = 0.0
        st.session_state[f"{key_prefix}_geocoded"] = False
        return
    lat, lon = get_lat_lon_ai_cached(addr)
    if lat != 0.0 or lon != 0.0:
        st.session_state[f"{key_prefix}_lat"] = float(lat)
        st.session_state[f"{key_prefix}_lon"] = float(lon)
        st.session_state[f"{key_prefix}_geocoded"] = True
    else:
        # clear stale values unless user manually filled non-zero
        if st.session_state.get(f"{key_prefix}_lat", 0.0) == 0.0 and st.session_state.get(f"{key_prefix}_lon", 0.0) == 0.0:
            st.session_state[f"{key_prefix}_lat"] = 0.0
            st.session_state[f"{key_prefix}_lon"] = 0.0
        st.session_state[f"{key_prefix}_geocoded"] = False

tab_manage, tab_schedule = st.tabs(["📋 現場管理", "📅 スケジュール生成"])

with tab_manage:
    colc1, colc2 = st.columns([3, 7])
    with colc1:
        if st.button("位置キャッシュをクリアする"):
            clear_geo_cache()
            st.success("ジオコーディングキャッシュをクリアしました。")
    with colc2:
        if SUPABASE:
            st.caption("データは Supabase に保存されます（Secrets: SUPABASE_URL / SUPABASE_KEY）。")
        else:
            st.caption("Supabase 未設定のためローカル SQLite に保存します（locations.db）。")

    # Backup / download button
    if st.button("📥 現在データをJSONでバックアップ"):
        payload = json.dumps(st.session_state.locations, ensure_ascii=False, indent=2)
        st.download_button("ダウンロード: locations_backup.json", payload, file_name="locations_backup.json", mime="application/json")

    if not st.session_state.locations:
        st.info("現場が登録されていません。下のフォームから追加してください。")
    else:
        st.subheader("🏢 登録済みの会社・現場一覧")
        df = pd.DataFrame(st.session_state.locations)
        try:
            companies = df["company"].unique()
        except Exception:
            companies = []
        for comp in companies:
            with st.expander(f"🏢 {comp}", expanded=True):
                comp_locs = df[df["company"] == comp]
                for _, row in comp_locs.iterrows():
                    col_info, col_btn1, col_btn2 = st.columns([6,2,2])
                    with col_info:
                        has_gps = "📡 位置測定済" if row.get("lat", 0) != 0.0 else "⚠️ 住所不明"
                        st.markdown(f"**📍 {row['name']}** ({row['address']})")
                        st.caption(f"月 {row['count']} 回訪問 | {has_gps}")
                    with col_btn1:
                        if st.button("編集", key=f"edit_btn_{row['id']}"):
                            st.session_state.editing_id = row['id']
                            st.rerun()
                    with col_btn2:
                        if st.button("削除", key=f"del_btn_{row['id']}"):
                            # delete: perform safe save attempt without mutating existing state until success
                            new_locations = [l for l in st.session_state.locations if l["id"] != row["id"]]
                            ok = save_data(new_locations)
                            if ok:
                                st.session_state.locations = new_locations
                                st.toast("🗑️ 現場を削除しました。")
                            else:
                                st.error("削除の保存に失敗しました。ネットワーク/保存先を確認してください。")
                            st.rerun()

    st.divider()

    # Edit/new form
    current_data = None
    if st.session_state.editing_id is not None:
        found = [l for l in st.session_state.locations if l["id"] == st.session_state.editing_id]
        if found:
            current_data = found[0]
        else:
            st.session_state.editing_id = None

    if current_data is None:
        st.subheader("➕ 新しい現場を追加")

    default_count = current_data["count"] if current_data else 1
    if "form_count" not in st.session_state or current_data:
        st.session_state.form_count = default_count

    count = st.selectbox("🔄 月に行く合計回数を選んでください", list(range(1,11)),
                        index=int(st.session_state.form_count - 1), key="count_selector")
    st.session_state.form_count = count

    key_prefix = f"edit_{st.session_state.editing_id}" if st.session_state.editing_id is not None else "new_form"

    # initialize per-form session keys
    st.session_state.setdefault(f"{key_prefix}_address", current_data["address"] if current_data else "")
    st.session_state.setdefault(f"{key_prefix}_lat", float(current_data["lat"]) if current_data else 0.0)
    st.session_state.setdefault(f"{key_prefix}_lon", float(current_data["lon"]) if current_data else 0.0)
    st.session_state.setdefault(f"{key_prefix}_geocoded", (st.session_state[f"{key_prefix}_lat"] != 0.0 or st.session_state[f"{key_prefix}_lon"] != 0.0))

    # address input outside form
    address = st.text_input(
        "🗺️ 住所（正確な住所を入れると自動で緯度経度を取得）",
        value=st.session_state.get(f"{key_prefix}_address", ""),
        key=f"{key_prefix}_address",
        on_change=address_changed,
        args=(key_prefix,)
    )

    with st.form(f"location_form_{st.session_state.editing_id}", clear_on_submit=False):
        company = st.text_input("🏢 会社名", value=current_data["company"] if current_data else "")
        name = st.text_input("📍 現場名", value=current_data["name"] if current_data else "", key=f"{key_prefix}_name")

        st.markdown("##### 🌐 位置情報の微調整（自動入力後に必要ならここで編集）")
        col_lat, col_lon = st.columns(2)
        with col_lat:
            form_lat = st.number_input("緯度", value=st.session_state.get(f"{key_prefix}_lat", 0.0), format="%.6f", key=f"{key_prefix}_lat")
        with col_lon:
            form_lon = st.number_input("経度", value=st.session_state.get(f"{key_prefix}_lon", 0.0), format="%.6f", key=f"{key_prefix}_lon")

        st.markdown("---")
        st.markdown("### 📅 各回収日の詳細ルール設定")
        existing_rules = {r["step"]: r for r in current_data.get("rules", [])} if current_data else {}
        rules = []
        type_options = ["特になし","○日まで","○日〜○日の間","○日ぴったり"]
        for i in range(1, count):
            st.markdown(f"**【{i}回目の集金】**")
            saved_rule = existing_rules.get(i, {})
            saved_type_label = denormalize_rule_type(normalize_rule_type(saved_rule.get("type","none")))
            type_idx = type_options.index(saved_type_label) if saved_type_label in type_options else 0
            r_type = st.radio(f"{i}回目のルール選択", type_options, index=type_idx, key=f"{key_prefix}_type_{i}")
            r_val = st.text_input(f"{i}回目の具体的な日付・期間 (例: 10、1-5)", value=saved_rule.get("val",""), key=f"{key_prefix}_val_{i}")
            rules.append({"step": i, "type": normalize_rule_type(r_type), "val": r_val, "is_last": False})

        st.markdown(f"**🏁【最終集金日（{count}回目）】**")
        saved_last_rule = existing_rules.get(count, {})
        saved_last_label = denormalize_rule_type(normalize_rule_type(saved_last_rule.get("type","none")))
        last_type_idx = type_options.index(saved_last_label) if saved_last_label in type_options else 0
        last_r_type = st.radio("最終集金のルール選択", type_options, index=last_type_idx, key=f"{key_prefix}_type_last")
        last_r_val = st.text_input("最終集金の具体的な日付・期間 (例: 25、20-25)", value=saved_last_rule.get("val",""), key=f"{key_prefix}_val_last")
        rules.append({"step": count, "type": normalize_rule_type(last_r_type), "val": last_r_val, "is_last": True})

        existing_intervals = {int(intv["from"]): intv["span"] for intv in current_data.get("intervals", [])} if current_data else {}
        intervals = []
        if count >= 2:
            st.markdown("---")
            st.markdown("### ⏳ 間隔のルール")
            for i in range(1, count):
                next_label = f"{i+1}回目" if i+1 < count else "最終集金"
                saved_span = existing_intervals.get(i, 0)
                span = st.number_input(f"「{i}回目」と「{next_label}」の間隔（日）", min_value=0, max_value=365, value=int(saved_span), key=f"{key_prefix}_span_{i}")
                intervals.append({"from": i, "to": i+1, "span": span})

        st.markdown("---")
        st.markdown("### 🗓️ 曜日・休日のルール")
        sat = st.checkbox("土曜可", value=current_data.get("sat", True) if current_data else True, key=f"{key_prefix}_sat")
        sun = st.checkbox("日曜可", value=current_data.get("sun", False) if current_data else False, key=f"{key_prefix}_sun")

        submitted = st.form_submit_button("保存")

        if submitted:
            address_val = st.session_state.get(f"{key_prefix}_address", "")
            # ensure lat/lon come from session state (may have been updated by address_changed)
            lat = st.session_state.get(f"{key_prefix}_lat", form_lat)
            lon = st.session_state.get(f"{key_prefix}_lon", form_lon)
            if company and name and address_val:
                form_data = {
                    "company": company, "name": name, "address": address_val,
                    "count": count, "rules": rules, "intervals": intervals,
                    "sat": sat, "sun": sun, "lat": float(lat), "lon": float(lon)
                }
                if st.session_state.editing_id is not None:
                    form_data["id"] = st.session_state.editing_id
                    # prepare new list and attempt save
                    new_locations = [form_data if l["id"] == st.session_state.editing_id else l for l in st.session_state.locations]
                    ok = save_data(new_locations)
                    if ok:
                        st.session_state.locations = new_locations
                        st.success("更新を保存しました。")
                        st.session_state.editing_id = None
                        st.rerun()
                    else:
                        st.error("保存に失敗しました。バックアップして再試行してください。")
                else:
                    current_ids = [loc["id"] for loc in st.session_state.locations]
                    new_id = max(current_ids + [0]) + 1
                    form_data["id"] = new_id
                    new_locations = st.session_state.locations + [form_data]
                    ok = save_data(new_locations)
                    if ok:
                        st.session_state.locations = new_locations
                        st.success("新しい現場を追加しました。")
                        st.rerun()
                    else:
                        st.error("保存に失敗しました。バックアップして再試行してください。")
            else:
                st.error("会社名、現場名、住所は必須です。")

with tab_schedule:
    st.subheader("📅 スケジュール生成")
    now = datetime.today()
    col_year, col_month = st.columns(2)
    with col_year:
        target_year = st.selectbox("年", [now.year-1, now.year, now.year+1], index=1)
    with col_month:
        target_month = st.selectbox("月", list(range(1,13)), index=now.month-1)

    custom_holidays = st.date_input("除外する日（複数選択可）", value=[])
    holiday_set = set()
    if isinstance(custom_holidays, (list, tuple)):
        holiday_set = {d.strftime("%Y-%m-%d") for d in custom_holidays if d}
    elif hasattr(custom_holidays, "strftime"):
        holiday_set = {custom_holidays.strftime("%Y-%m-%d")}

    col_min, col_max = st.columns(2)
    with col_min:
        min_tasks = st.selectbox("最小件数", list(range(1,11)), index=1)
    with col_max:
        max_tasks = st.selectbox("最大件数", list(range(1,13)), index=4)

    if st.button("🚀 スケジュールを生成"):
        if not st.session_state.locations:
            st.error("現場が登録されていません。")
        else:
            start_date = datetime(target_year, target_month, 1)
            if target_month == 12:
                end_date = datetime(target_year+1, 1, 1) - timedelta(days=1)
            else:
                end_date = datetime(target_year, target_month+1, 1) - timedelta(days=1)
            days_in_month = (end_date - start_date).days + 1
            all_days = [start_date + timedelta(days=x) for x in range(days_in_month)]

            task_pool = []
            for loc in st.session_state.locations:
                for step_idx in range(loc["count"]):
                    rules_list = loc.get("rules", [])
                    rule = next((r for r in rules_list if r["step"] == step_idx+1), {"type":"none","val":""})
                    priority = 0
                    if rule["type"] in ["exact","range"]:
                        priority += 20
                    elif rule["type"] == "until":
                        priority += 10
                    if step_idx+1 == loc["count"]:
                        priority += 5
                    task_pool.append({
                        "loc_id": loc["id"], "company": loc["company"], "name": loc["name"],
                        "lat": loc.get("lat",0.0), "lon": loc.get("lon",0.0),
                        "step": step_idx+1, "rule": rule, "priority": priority,
                        "sat": loc.get("sat", True), "sun": loc.get("sun", False),
                        "intervals": loc.get("intervals", [])
                    })

            current_schedule = {day.strftime("%Y-%m-%d"): [] for day in all_days}
            unassigned_tasks = sorted(task_pool, key=lambda x: (-x["priority"], x["step"], x["loc_id"]))
            history_days = {loc["id"]: [] for loc in st.session_state.locations}
            overflow_tasks = []

            for task in unassigned_tasks:
                best_day = None
                min_score = float("inf")
                for day in all_days:
                    day_str = day.strftime("%Y-%m-%d")
                    d_num = day.day
                    w = day.weekday()
                    if day_str in holiday_set: continue
                    if w == 5 and not task["sat"]: continue
                    if w == 6 and not task["sun"]: continue
                    if not check_date_rule(task["rule"], d_num): continue
                    if len(current_schedule[day_str]) >= max_tasks: continue
                    if not check_interval_rule(task, day, history_days): continue
                    current_count = len(current_schedule[day_str])
                    if current_schedule[day_str]:
                        valid_last_loc = (0.0,0.0)
                        for existing in reversed(current_schedule[day_str]):
                            if existing.get("lat",0.0) != 0.0 and existing.get("lon",0.0) != 0.0:
                                valid_last_loc = (existing["lat"], existing["lon"])
                                break
                        dist = calculate_geopy_distance(valid_last_loc, (task["lat"], task["lon"]))
                    else:
                        dist = 0.0
                    score = current_count*5.0 + dist
                    if score < min_score:
                        min_score = score
                        best_day = day
                if best_day:
                    best_day_str = best_day.strftime("%Y-%m-%d")
                    current_schedule[best_day_str].append(task)
                    history_days[task["loc_id"]].append({"step": task["step"], "day": best_day})
                else:
                    overflow_tasks.append(task)

            # overflow reassign
            for task in overflow_tasks:
                best_day = None
                min_score = float("inf")
                for allowed_max in range(max_tasks, max_tasks+10):
                    for day in all_days:
                        day_str = day.strftime("%Y-%m-%d")
                        d_num = day.day
                        w = day.weekday()
                        if day_str in holiday_set: continue
                        if w == 5 and not task["sat"]: continue
                        if w == 6 and not task["sun"]: continue
                        if not check_date_rule(task["rule"], d_num): continue
                        if not check_interval_rule(task, day, history_days): continue
                        if len(current_schedule[day_str]) >= allowed_max: continue
                        current_count = len(current_schedule[day_str])
                        if current_schedule[day_str]:
                            valid_last_loc = (0.0,0.0)
                            for existing in reversed(current_schedule[day_str]):
                                if existing.get("lat",0.0) != 0.0 and existing.get("lon",0.0) != 0.0:
                                    valid_last_loc = (existing["lat"], existing["lon"])
                                    break
                            dist = calculate_geopy_distance(valid_last_loc, (task["lat"], task["lon"]))
                        else:
                            dist = 0.0
                        score = current_count*5.0 + dist
                        if score < min_score:
                            min_score = score
                            best_day = day
                    if best_day:
                        break
                if best_day:
                    best_day_str = best_day.strftime("%Y-%m-%d")
                    current_schedule[best_day_str].append(task)
                    history_days[task["loc_id"]].append({"step": task["step"], "day": best_day})
                else:
                    fallback_day, forced = choose_fallback_day(all_days, holiday_set, task, current_schedule=current_schedule, history_days=history_days)
                    fd_str = fallback_day.strftime("%Y-%m-%d")
                    forced_task = dict(task)
                    forced_task["forced_fallback"] = forced
                    current_schedule[fd_str].append(forced_task)
                    history_days[task["loc_id"]].append({"step": task["step"], "day": fallback_day})

            # route sort per day
            for day_str in current_schedule:
                if len(current_schedule[day_str]) > 1:
                    ordered = []
                    unvisited = current_schedule[day_str].copy()
                    first_task = None
                    for t in unvisited:
                        if t.get("lat",0.0) != 0.0 and t.get("lon",0.0) != 0.0:
                            first_task = t
                            break
                    if first_task:
                        unvisited.remove(first_task)
                    else:
                        first_task = unvisited.pop(0)
                    curr_loc = (first_task.get("lat",0.0), first_task.get("lon",0.0))
                    ordered.append(first_task)
                    while unvisited:
                        closest_idx = 0
                        min_d = float("inf")
                        for idx, t in enumerate(unvisited):
                            if t.get("lat",0.0) == 0.0 or t.get("lon",0.0) == 0.0:
                                d = 9999.0
                            else:
                                d = calculate_geopy_distance(curr_loc, (t["lat"], t["lon"]))
                            if d < min_d:
                                min_d = d
                                closest_idx = idx
                        next_task = unvisited.pop(closest_idx)
                        if next_task.get("lat",0.0) != 0.0 and next_task.get("lon",0.0) != 0.0:
                            curr_loc = (next_task["lat"], next_task["lon"])
                        ordered.append(next_task)
                    current_schedule[day_str] = ordered

            st.session_state.schedule_results = {"calculated": True, "schedule": current_schedule}

    if st.session_state.schedule_results and st.session_state.schedule_results["calculated"]:
        st.success("スケジュールを生成しました")
        for day_str, tasks in st.session_state.schedule_results["schedule"].items():
            if tasks:
                date_obj = datetime.strptime(day_str, "%Y-%m-%d")
                weekday_str = ["月","火","水","木","金","土","日"][date_obj.weekday()]
                st.markdown(f"#### {day_str} ({weekday_str}) — `{len(tasks)} 件`")
                for idx, t in enumerate(tasks, 1):
                    geo_alert = " ⚠️(位置不明)" if t.get("lat",0.0) == 0.0 else ""
                    forced_alert = " 🔥(強制割当)" if t.get("forced_fallback") else ""
                    st.write(f"{idx}. {t['company']} : {t['name']} ({t['step']}回目){geo_alert}{forced_alert}")
                st.divider()

# End of file
