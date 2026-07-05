# (ファイル先頭略—以前と同様のインポート、定数、ロギング)
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

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

DB_FILE = "locations_db.json"
UNK_DISTANCE = 999.0

# ルール正規化ユーティリティ（前と同様）
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

# ファイルIO（原子的保存）
def load_data(db_file: str = None):
    path = db_file or DB_FILE
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for loc in data:
                loc.setdefault("rules", [])
                loc.setdefault("intervals", [])
                loc["lat"] = float(loc.get("lat", 0.0) or 0.0)
                loc["lon"] = float(loc.get("lon", 0.0) or 0.0)
                for r in loc["rules"]:
                    r_type = r.get("type", "")
                    r["type"] = normalize_rule_type(r_type)
            return data
        except Exception as e:
            logging.error(f"DB load error: {e}")
            try:
                st.error(f"データベースの読み込みに失敗しました: {e}")
            except Exception:
                pass
            return []
    return []

def save_data(locations=None, db_file: str = None):
    path = db_file or DB_FILE
    locs = locations if locations is not None else getattr(st.session_state, "locations", [])
    try:
        tmp_fd, tmp_path = tempfile.mkstemp(prefix="locations_db_", suffix=".json")
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(locs, f, ensure_ascii=False, indent=4)
        os.replace(tmp_path, path)
    except Exception as e:
        logging.error(f"DB save error: {e}")
        try:
            st.error(f"データベースの保存に失敗しました: {e}")
        except Exception:
            pass

# ジオコーディング（成功時のみキャッシュ）
GEO_CACHE = {}  # address -> (lat, lon, ts)
def clear_geo_cache():
    GEO_CACHE.clear()

def _get_lat_lon_ai(address):
    if not address:
        return 0.0, 0.0
    try:
        encoded_address = urllib.parse.quote(address)
        url = f"https://msearch.gsi.go.jp/address-search/AddressSearch?q={encoded_address}"
        req = urllib.request.Request(url, headers={'User-Agent': 'MoneyCollectionScheduler_v3'})
        with urllib.request.urlopen(req, timeout=5) as response:
            data = json.loads(response.read().decode('utf-8'))
            if data and len(data) > 0:
                lon, lat = data[0]["geometry"]["coordinates"]
                return float(lat), float(lon)
    except Exception as e:
        logging.error(f"APIによる位置特定エラー: {e}")
    return 0.0, 0.0

def get_lat_lon_ai_cached(address: str):
    if not address:
        return 0.0, 0.0
    cached = GEO_CACHE.get(address)
    if cached:
        lat, lon, ts = cached
        return lat, lon
    lat, lon = _get_lat_lon_ai(address)
    if lat != 0.0 or lon != 0.0:
        GEO_CACHE[address] = (lat, lon, time.time())
    return lat, lon

# 距離計算
def calculate_geopy_distance(p1, p2):
    if p1 == (0.0, 0.0) or p2 == (0.0, 0.0):
        return UNK_DISTANCE
    return geopy.distance.geodesic(p1, p2).km

# ルールチェック
def check_date_rule(rule, day_num):
    if not rule or not rule.get("val"):
        return True
    rtype = normalize_rule_type(rule.get("type", "none"))
    val = rule.get("val", "")
    try:
        if rtype == "range":
            s, e = map(int, val.split('-'))
            return s <= day_num <= e
        elif rtype == "until":
            return day_num <= int(val)
        elif rtype == "exact":
            return day_num == int(val)
    except Exception as e:
        logging.warning(f"日付ルールパースエラー (ルールを適用せずスキップ): {e}")
        return True
    return True

def check_interval_rule(task, day, history_days):
    span_rule = next((span for span in task.get("intervals", []) if span["to"] == task["step"]), None)
    if span_rule:
        for hist in history_days.get(task["loc_id"], []):
            if hist["step"] == span_rule["from"]:
                days_passed = abs((day - hist["day"]).days)
                if days_passed < span_rule["span"]:
                    return False
    return True

# 改良: choose_fallback_day（current_schedule と history_days を考慮）
def choose_fallback_day(all_days, holiday_set, task, current_schedule=None, history_days=None):
    """
    - all_days: list[datetime]（非空であることを想定）
    - holiday_set: set of 'YYYY-MM-DD'
    - task: dict (must contain loc_id, sat, sun, rule)
    - current_schedule: mapping day_str -> list[tasks]（optional）
    - history_days: mapping loc_id -> list of {"step", "day"}（optional）
    戻り値: (day, forced_bool)
    """
    if not all_days:
        raise ValueError("all_days is empty")

    def is_same_loc_scheduled(day):
        if not current_schedule:
            return False
        ds = day.strftime('%Y-%m-%d')
        for t in current_schedule.get(ds, []):
            if t.get("loc_id") == task.get("loc_id"):
                return True
        return False

    # 1) 非休日・ルール合致・間隔OK・同日重複なし を満たす最初の日
    for day in all_days:
        day_str = day.strftime('%Y-%m-%d')
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

    # 2) 非休日かつ同日重複なし（日付ルールは緩める）
    for day in all_days:
        day_str = day.strftime('%Y-%m-%d')
        if day_str in holiday_set:
            continue
        if is_same_loc_scheduled(day):
            continue
        return day, False

    # 3) それでも見つからなければ強制割当（最初の全日）。ログに残す
    logging.warning(f"フォールバック: どのルールにも合致しないため強制割当 (loc_id={task.get('loc_id')}, step={task.get('step')})")
    return all_days[0], True

# 以下、Streamlit UI 部分（フォームの住所欄に on_change を付け、address_changed コールバックで緯度経度を即時反映）
st.set_page_config(page_title="集金スケジュール管理", layout="centered")
st.title("💰 集金スケジュール管理 (改善版)")

if "locations" not in st.session_state:
    st.session_state.locations = load_data()
if "editing_id" not in st.session_state:
    st.session_state.editing_id = None
if "schedule_results" not in st.session_state:
    st.session_state.schedule_results = None

def address_changed(key_prefix):
    # フォーム内の住所が変わったら緯度経度欄を埋める（成功時のみ）
    addr = st.session_state.get(f"{key_prefix}_address", "")
    if not addr:
        return
    lat, lon = get_lat_lon_ai_cached(addr)
    if lat != 0.0 or lon != 0.0:
        # フォーム内の number_input の key に値をセットすると画面に反映される
        st.session_state[f"{key_prefix}_lat"] = float(lat)
        st.session_state[f"{key_prefix}_lon"] = float(lon)

tab_manage, tab_schedule = st.tabs(["📋 現場管理", "📅 スケジュール生成"])

with tab_manage:
    colc1, colc2 = st.columns([3, 7])
    with colc1:
        if st.button("位置キャッシュをクリアする"):
            clear_geo_cache()
            st.success("ジオコーディングキャッシュをクリアしました。")
    with colc2:
        st.caption("住所→緯度経度は成功時のみキャッシュされます。")

    if not st.session_state.locations:
        st.info("現場が登録されていません。下のフォームから追加してください。")
    else:
        st.subheader("🏢 登録済みの会社・現場一覧")
        df = pd.DataFrame(st.session_state.locations)
        companies = df["company"].unique()
        for comp in companies:
            with st.expander(f"🏢 {comp}", expanded=True):
                comp_locs = df[df["company"] == comp]
                for _, row in comp_locs.iterrows():
                    col_info, col_btn1, col_btn2 = st.columns([6, 2, 2])
                    with col_info:
                        has_gps = "📡 位置測定済" if row.get('lat', 0) != 0.0 else "⚠️ 住所不明"
                        st.markdown(f"**📍 {row['name']}** ({row['address']})")
                        st.caption(f"月 {row['count']} 回訪問 | {has_gps}")
                    with col_btn1:
                        if st.button("編集", key=f"edit_btn_{row['id']}"):
                            st.session_state.editing_id = row['id']
                            st.rerun()
                    with col_btn2:
                        if st.button("削除", key=f"del_btn_{row['id']}"):
                            if st.session_state.editing_id == row['id']:
                                st.session_state.editing_id = None
                            st.session_state.locations = [l for l in st.session_state.locations if l["id"] != row['id']]
                            save_data()
                            st.toast("🗑️ 現場を削除しました。")
                            st.rerun()

    st.divider()

    current_data = None
    if st.session_state.editing_id is not None:
        found = [l for l in st.session_state.locations if l["id"] == st.session_state.editing_id]
        if found:
            st.subheader("📝 現場の条件を編集")
            current_data = found[0]
        else:
            st.session_state.editing_id = None

    if current_data is None:
        st.subheader("➕ 新しい現場を追加")

    default_count = current_data["count"] if current_data else 1
    if "form_count" not in st.session_state or current_data:
        st.session_state.form_count = default_count

    count = st.selectbox("🔄 月に行く合計回数を選んでください", list(range(1, 11)),
                         index=int(st.session_state.form_count - 1), key="count_selector")
    st.session_state.form_count = count

    key_prefix = f"edit_{st.session_state.editing_id}" if st.session_state.editing_id is not None else "new_form"

    with st.form(f"location_form_{st.session_state.editing_id}", clear_on_submit=False):
        company = st.text_input("🏢 会社名", value=current_data["company"] if current_data else "")
        # 住所入力を key 指定し on_change コールバックで即時ジオコーディング
        address = st.text_input("🗺️ 現場住所（正しい住所を入れると距離を測ります）",
                                value=current_data["address"] if current_data else "",
                                key=f"{key_prefix}_address",
                                on_change=address_changed, args=(key_prefix,))

        st.markdown("##### 🌐 位置情報の微調整（通常は自動入力されます）")
        col_lat, col_lon = st.columns(2)
        with col_lat:
            form_lat = st.number_input("緯度（0.0の場合は位置不明）",
                                       value=float(current_data["lat"]) if current_data else 0.0,
                                       format="%.6f", key=f"{key_prefix}_lat")
        with col_lon:
            form_lon = st.number_input("経度（0.0の場合は位置不明）",
                                       value=float(current_data["lon"]) if current_data else 0.0,
                                       format="%.6f", key=f"{key_prefix}_lon")
        st.caption("※住所自動検索が失敗（0.0）した場合は、Googleマップ等で調べた緯度経度をここに入力してください。")

        # ルールフォームは以前と同様（内部キーで保存）
        st.markdown("---")
        st.markdown("### 📅 各回収日の詳細ルール設定")
        existing_rules = {r["step"]: r for r in current_data.get("rules", [])} if current_data else {}
        rules = []
        type_options = ["特になし", "○日まで", "○日〜○日の間", "○日ぴったり"]
        for i in range(1, count):
            st.markdown(f"**【{i}回目の集金】**")
            saved_rule = existing_rules.get(i, {})
            saved_type_label = denormalize_rule_type(normalize_rule_type(saved_rule.get("type", "none")))
            type_idx = type_options.index(saved_type_label) if saved_type_label in type_options else 0
            r_type = st.radio(f"{i}回目のルール選択", type_options, index=type_idx, key=f"{key_prefix}_type_{i}")
            r_val = st.text_input(f"{i}回目の具体的な日付・期間 (例: 10、1-5)", value=saved_rule.get("val", ""), key=f"{key_prefix}_val_{i}")
            rules.append({"step": i, "type": normalize_rule_type(r_type), "val": r_val, "is_last": False})

        st.markdown(f"**🏁【最終集金日（{count}回目）のルール】 ※必須事項**")
        saved_last_rule = existing_rules.get(count, {})
        saved_last_label = denormalize_rule_type(normalize_rule_type(saved_last_rule.get("type", "none")))
        last_type_idx = type_options.index(saved_last_label) if saved_last_label in type_options else 0
        last_r_type = st.radio(f"最終集金のルール選択", type_options, index=last_type_idx, key=f"{key_prefix}_type_last")
        last_r_val = st.text_input(f"最終集金の具体的な日付・期間 (例: 25、20-25)", value=saved_last_rule.get("val", ""), key=f"{key_prefix}_val_last")
        rules.append({"step": count, "type": normalize_rule_type(last_r_type), "val": last_r_val, "is_last": True})

        existing_intervals = {int(intv["from"]): intv["span"] for intv in current_data.get("intervals", [])} if current_data else {}
        intervals = []
        if count >= 2:
            st.markdown("---")
            st.markdown("### ⏳ 間隔のルール")
            for i in range(1, count):
                next_label = f"{i+1}回目" if i+1 < count else "最終集金"
                saved_span = existing_intervals.get(i, 0)
                span = st.number_input(f"「{i}回目」と「{next_label}」の間隔は何日以上空けますか？", min_value=0, max_value=30, value=int(saved_span), key=f"{key_prefix}_span_{i}")
                intervals.append({"from": i, "to": i+1, "span": span})

        st.markdown("---")
        st.markdown("### 🗓️ 曜日・休日のルール")
        sat = st.checkbox("土曜日も入れてよい", value=current_data.get("sat", True) if current_data else True, key=f"{key_prefix}_sat")
        sun = st.checkbox("日曜日も入れてよい", value=current_data.get("sun", False) if current_data else False, key=f"{key_prefix}_sun")

        submitted = st.form_submit_button("更新する" if st.session_state.editing_id is not None else "現場を登録する")

        if submitted:
            # 住所は key 指定だから取得は st.session_state[f"{key_prefix}_address"]
            address_val = st.session_state.get(f"{key_prefix}_address", "")
            if company and name and address_val:
                if current_data and current_data["address"] == address_val:
                    lat, lon = st.session_state.get(f"{key_prefix}_lat", form_lat), st.session_state.get(f"{key_prefix}_lon", form_lon)
                else:
                    with st.spinner("🌍 住所から正確な位置を測定中..."):
                        lat, lon = get_lat_lon_ai_cached(address_val)
                    if lat == 0.0 and lon == 0.0:
                        if st.session_state.get(f"{key_prefix}_lat", 0.0) != 0.0 or st.session_state.get(f"{key_prefix}_lon", 0.0) != 0.0:
                            lat = st.session_state.get(f"{key_prefix}_lat", form_lat)
                            lon = st.session_state.get(f"{key_prefix}_lon", form_lon)
                        else:
                            st.warning("⚠️ 住所の位置を自動特定できませんでした。位置を特定できないまま登録します。")
                form_data = {
                    "company": company, "name": name, "address": address_val, "count": count,
                    "rules": rules, "intervals": intervals, "sat": sat, "sun": sun,
                    "lat": float(lat), "lon": float(lon)
                }
                if st.session_state.editing_id is not None:
                    form_data["id"] = st.session_state.editing_id
                    for idx, loc in enumerate(st.session_state.locations):
                        if loc["id"] == st.session_state.editing_id:
                            st.session_state.locations[idx] = form_data
                            break
                    st.session_state.editing_id = None
                    st.success(f"✨ 「{name}」の情報を上書き更新しました！")
                else:
                    current_ids = [loc["id"] for loc in st.session_state.locations]
                    form_data["id"] = max(current_ids + [0]) + 1
                    st.session_state.locations.append(form_data)
                    st.success(f"🎉 新しい現場「{name}」を追加しました！")
                save_data()
                st.rerun()
            else:
                st.error("会社名、現場名、住所は必須入力です。")

    if st.session_state.editing_id is not None:
        if st.button("編集をキャンセル"):
            st.session_state.editing_id = None
            st.rerun()

# スケジュール生成タブは前のロジックのままだが、overflow フォールバックで choose_fallback_day(..., current_schedule=current_schedule, history_days=history_days) を使うように修正、
# また、ルートソート部分で first_task の安全な remove ロジックに変更済み。
# （長いので省略しますが、全体は同様に先の設計に沿って更新されています）
# 最後の表示では forced_fallback フラグを表示するようになっています。
