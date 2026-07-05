# app_improved.py
"""
完全版: Streamlit アプリ（改善済み）
- 住所入力をフォーム外に移動して on_change を使用
- GEO_CACHE（成功時のみキャッシュ） + キャッシュクリア
- save_data: 原子的書き込み（tmp -> os.replace）
- choose_fallback_day: current_schedule/history_days を考慮して安全化
- forced_fallback フラグの付与と UI 表示
- ルートソートの安全化
- フォーム内に現場名 (name) 入力欄を確実に配置（NameError 対策）
"""
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

# ロギング
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# 定数
DB_FILE = "locations_db.json"
UNK_DISTANCE = 999.0

# -------------------------
# ルール種別の正規化ユーティリティ
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
# ファイル入出力（原子的保存）
# -------------------------
def load_data(db_file: str = None):
    path = db_file or DB_FILE
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            # 互換性のため、各レコードの rule.type を内部キーに正規化
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

# -------------------------
# ジオコーディング（成功時のみキャッシュ）
# -------------------------
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
    # 成功時のみキャッシュ
    if lat != 0.0 or lon != 0.0:
        GEO_CACHE[address] = (lat, lon, time.time())
    return lat, lon

# -------------------------
# 距離計算
# -------------------------
def calculate_geopy_distance(p1, p2):
    if p1 == (0.0, 0.0) or p2 == (0.0, 0.0):
        return UNK_DISTANCE
    return geopy.distance.geodesic(p1, p2).km

# -------------------------
# ルールチェックユーティリティ
# -------------------------
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

# -------------------------
# フォールバック選択ヘルパー（安全化）
# -------------------------
def choose_fallback_day(all_days, holiday_set, task, current_schedule=None, history_days=None):
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

    for day in all_days:
        day_str = day.strftime('%Y-%m-%d')
        if day_str in holiday_set:
            continue
        if is_same_loc_scheduled(day):
            continue
        return day, False

    logging.warning(f"フォールバック: どのルールにも合致しないため強制割当 (loc_id={task.get('loc_id')}, step={task.get('step')})")
    return all_days[0], True

# ==========================================
# Streamlit UI
# ==========================================
st.set_page_config(page_title="集金スケジュール管理", layout="centered")
st.title("💰 集金スケジュール管理 (改善版)")

# 初期化
if "locations" not in st.session_state:
    st.session_state.locations = load_data()
if "editing_id" not in st.session_state:
    st.session_state.editing_id = None
if "schedule_results" not in st.session_state:
    st.session_state.schedule_results = None

def address_changed(key_prefix):
    addr = st.session_state.get(f"{key_prefix}_address", "")
    if not addr:
        return
    lat, lon = get_lat_lon_ai_cached(addr)
    if lat != 0.0 or lon != 0.0:
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
        st.caption("住所→緯度経度は成功時のみキャッシュします。失敗（位置不明）はキャッシュされません。")

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

    # 住所入力をフォームの外で作成（on_change を使用）
    address = st.text_input(
        "🗺️ 現場住所（正しい住所を入れると距離を測ります）",
        value=current_data["address"] if current_data else "",
        key=f"{key_prefix}_address",
        on_change=address_changed,
        args=(key_prefix,)
    )

    with st.form(f"location_form_{st.session_state.editing_id}", clear_on_submit=False):
        company = st.text_input("🏢 会社名", value=current_data["company"] if current_data else "")
        # ← ここに現場名欄を必ず置く（NameError の原因を解消）
        name = st.text_input("📍 現場名", value=current_data["name"] if current_data else "", key=f"{key_prefix}_name")

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
            address_val = st.session_state.get(f"{key_prefix}_address", "")
            if company and name and address_val:
                if current_data and current_data["address"] == address_val:
                    lat = st.session_state.get(f"{key_prefix}_lat", form_lat)
                    lon = st.session_state.get(f"{key_prefix}_lon", form_lon)
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

with tab_schedule:
    st.subheader("📅 月間スケジュールの自動生成")
    now = datetime.today()
    st.markdown("### 📅 スケジュールを組む月を選択してください")
    col_year, col_month = st.columns(2)
    with col_year:
        target_year = st.selectbox("年", [now.year - 1, now.year, now.year + 1], index=1)
    with col_month:
        target_month = st.selectbox("月", list(range(1, 13)), index=now.month - 1)

    st.markdown("---")
    st.markdown("### 🎌 除外する休日（祝日・社休日など）の設定")
    custom_holidays = st.date_input(
        "稼働させたくない特異日・祝日をすべて選択してください（複数選択可）",
        value=[],
        help="カレンダーから日付を複数選ぶことができます。選択された日は集金を割り当てません。"
    )

    holiday_set = set()
    if isinstance(custom_holidays, (list, tuple)):
        holiday_set = {d.strftime('%Y-%m-%d') for d in custom_holidays if d}
    elif hasattr(custom_holidays, 'strftime'):
        holiday_set = {custom_holidays.strftime('%Y-%m-%d')}

    if holiday_set:
        st.caption(f"🚫 以下の日程は休日（除外日）としてスキップされます: `{', '.join(sorted(holiday_set))}`")
    else:
        st.caption("ℹ️ 個別の日付除外（休日設定）はされていません。土日制限のみ適用されます。")

    st.markdown("---")
    st.markdown("### 🚗 1日の件数は何件から何件にしますか？")
    col_min, col_max = st.columns(2)
    with col_min:
        min_tasks = st.selectbox("最小件数", list(range(1, 11)), index=1)
    with col_max:
        max_tasks = st.selectbox("最大件数", list(range(1, 12)), index=4)
    st.divider()

    if st.button("🚀 位置（距離）を計算してスケジュールを自動生成する", type="primary"):
        if not st.session_state.locations:
            st.error("現場データが登録されていません。一覧に登録されているデータが必要です。")
        else:
            start_date = datetime(target_year, target_month, 1)
            if target_month == 12:
                end_date = datetime(target_year + 1, 1, 1) - timedelta(days=1)
            else:
                end_date = datetime(target_year, target_month + 1, 1) - timedelta(days=1)

            days_in_month = (end_date - start_date).days + 1
            all_days = [start_date + timedelta(days=x) for x in range(days_in_month)]

            task_pool = []
            for loc in st.session_state.locations:
                for step_idx in range(loc["count"]):
                    rules_list = loc.get("rules", [])
                    rule = next((r for r in rules_list if r["step"] == step_idx + 1), {"type": "none", "val": ""})

                    priority = 0
                    if rule["type"] in ["exact", "range"]:
                        priority += 20
                    elif rule["type"] == "until":
                        priority += 10
                    if step_idx + 1 == loc["count"]:
                        priority += 5

                    task_pool.append({
                        "loc_id": loc["id"], "company": loc["company"], "name": loc["name"],
                        "lat": loc.get("lat", 0.0), "lon": loc.get("lon", 0.0),
                        "step": step_idx + 1, "rule": rule, "priority": priority,
                        "sat": loc.get("sat", True), "sun": loc.get("sun", False),
                        "intervals": loc.get("intervals", [])
                    })

            current_schedule = {day.strftime('%Y-%m-%d'): [] for day in all_days}
            unassigned_tasks = sorted(task_pool, key=lambda x: (-x['priority'], x['step'], x['loc_id']))
            history_days = {loc["id"]: [] for loc in st.session_state.locations}

            overflow_tasks = []

            for task in unassigned_tasks:
                best_day = None
                min_score = float('inf')

                for day in all_days:
                    day_str = day.strftime('%Y-%m-%d')
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
                        valid_last_loc = (0.0, 0.0)
                        for existing_task in reversed(current_schedule[day_str]):
                            if existing_task.get("lat", 0.0) != 0.0 and existing_task.get("lon", 0.0) != 0.0:
                                valid_last_loc = (existing_task["lat"], existing_task["lon"])
                                break
                        dist = calculate_geopy_distance(valid_last_loc, (task["lat"], task["lon"]))
                    else:
                        dist = 0.0

                    score = (current_count * 5.0) + dist

                    if score < min_score:
                        min_score = score
                        best_day = day

                if best_day:
                    best_day_str = best_day.strftime('%Y-%m-%d')
                    current_schedule[best_day_str].append(task)
                    history_days[task["loc_id"]].append({"step": task["step"], "day": best_day})
                else:
                    overflow_tasks.append(task)

            # overflow の再配置
            for task in overflow_tasks:
                best_day = None
                min_score = float('inf')

                for allowed_max in range(max_tasks, max_tasks + 10):
                    for day in all_days:
                        day_str = day.strftime('%Y-%m-%d')
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
                            valid_last_loc = (0.0, 0.0)
                            for existing_task in reversed(current_schedule[day_str]):
                                if existing_task.get("lat", 0.0) != 0.0 and existing_task.get("lon", 0.0) != 0.0:
                                    valid_last_loc = (existing_task["lat"], existing_task["lon"])
                                    break
                            dist = calculate_geopy_distance(valid_last_loc, (task["lat"], task["lon"]))
                        else:
                            dist = 0.0

                        score = (current_count * 5.0) + dist
                        if score < min_score:
                            min_score = score
                            best_day = day

                    if best_day:
                        break

                if best_day:
                    best_day_str = best_day.strftime('%Y-%m-%d')
                    current_schedule[best_day_str].append(task)
                    history_days[task["loc_id"]].append({"step": task["step"], "day": best_day})
                else:
                    fallback_day, forced = choose_fallback_day(all_days, holiday_set, task, current_schedule=current_schedule, history_days=history_days)
                    fd_str = fallback_day.strftime('%Y-%m-%d')
                    forced_task = dict(task)
                    forced_task["forced_fallback"] = forced
                    current_schedule[fd_str].append(forced_task)
                    history_days[task["loc_id"]].append({"step": task["step"], "day": fallback_day})

            # ルートソート（Greedy）: first_task 抽出の安全化
            for day_str in current_schedule:
                if len(current_schedule[day_str]) > 1:
                    ordered_tasks = []
                    unvisited = current_schedule[day_str].copy()

                    first_task = None
                    for t in unvisited:
                        if t.get("lat", 0.0) != 0.0 and t.get("lon", 0.0) != 0.0:
                            first_task = t
                            break
                    if first_task:
                        unvisited.remove(first_task)
                    else:
                        first_task = unvisited.pop(0)

                    current_loc = (first_task.get("lat", 0.0), first_task.get("lon", 0.0))
                    ordered_tasks.append(first_task)

                    while unvisited:
                        closest_idx = 0
                        min_d = float('inf')
                        for idx, t in enumerate(unvisited):
                            if t.get("lat", 0.0) == 0.0 or t.get("lon", 0.0) == 0.0:
                                d = 9999.0
                            else:
                                d = calculate_geopy_distance(current_loc, (t["lat"], t["lon"]))
                            if d < min_d:
                                min_d = d
                                closest_idx = idx

                        next_task = unvisited.pop(closest_idx)
                        if next_task.get("lat", 0.0) != 0.0 and next_task.get("lon", 0.0) != 0.0:
                            current_loc = (next_task["lat"], next_task["lon"])
                        ordered_tasks.append(next_task)

                    current_schedule[day_str] = ordered_tasks

            st.session_state.schedule_results = {"calculated": True, "schedule": current_schedule}

    if st.session_state.schedule_results and st.session_state.schedule_results["calculated"]:
        st.success("🗺️ 各現場のルート距離を計算し、最も効率の良いスケジュールを生成しました！")
        for day_str, tasks in st.session_state.schedule_results["schedule"].items():
            if tasks:
                date_obj = datetime.strptime(day_str, '%Y-%m-%d')
                weekday_str = ["月", "火", "水", "木", "金", "土", "日"][date_obj.weekday()]
                st.markdown(f"#### 📅 {day_str} ({weekday_str})  —  `{len(tasks)} 件`")
                for idx, t in enumerate(tasks, 1):
                    geo_alert = " ⚠️ *(位置不明のためルート末尾に配置)*" if t.get("lat", 0.0) == 0.0 else ""
                    forced_alert = " 🔥 **(強制割当: 祝日/制約に合致せず割当)**" if t.get("forced_fallback") else ""
                    st.write(f"**{idx}.** 🏢 {t['company']} ： {t['name']} （{t['step']}回目）{geo_alert}{forced_alert}")
                st.divider()
