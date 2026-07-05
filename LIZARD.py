import streamlit as st
import pandas as pd
import numpy as np
import json
import os
from datetime import datetime, timedelta
import urllib.parse
import urllib.request

# ==========================================
# ⚙️ サーバーが寝ても絶対に消えない保存箱
# ==========================================
if "locations" not in st.session_state:
    st.session_state.locations = []
if "editing_id" not in st.session_state: st.session_state.editing_id = None
if "last_input" not in st.session_state: st.session_state.last_input = None
if "schedule_results" not in st.session_state: st.session_state.schedule_results = None

# --- 🗺️ 住所から位置（緯度経度）を測る機能 ---
def get_lat_lon_ai(address):
    if not address:
        return 0.0, 0.0
    try:
        encoded_address = urllib.parse.quote(address)
        url = f"https://msearch.gsi.go.jp/address-search/AddressSearch?q={encoded_address}"
        
        req = urllib.request.Request(url, headers={'User-Agent': 'MoneyCollectionScheduler_v2'})
        with urllib.request.urlopen(req, timeout=5) as response:
            data = json.loads(response.read().decode('utf-8'))
            if data and len(data) > 0:
                lon, lat = data[0]["geometry"]["coordinates"]
                return float(lat), float(lon)
    except:
        pass
    return 0.0, 0.0

# 2点間の直線距離を計算する関数
def calculate_distance(p1, p2):
    if p1 == (0.0, 0.0) or p2 == (0.0, 0.0):
        return 999.0  # 位置が分からないものは遠くとして扱う
    return np.sqrt((p1[0] - p2[0])**2 + (p1[1] - p2[1])**2)

st.set_page_config(page_title="集金スケジュール管理", layout="centered")

st.title("💰 集金スケジュール管理 (AI位置測定版)")

tab_manage, tab_schedule = st.tabs(["📋 現場管理", "📅 スケジュール生成"])

# ==========================================
# 1. 📋 現場管理タブ
# ==========================================
with tab_manage:
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
                        if st.button("編集", key=f"edit_{row['id']}"):
                            st.session_state.editing_id = row['id']
                            st.rerun()
                    with col_btn2:
                        if st.button("削除", key=f"del_{row['id']}"):
                            if st.session_state.editing_id == row['id']:
                                st.session_state.editing_id = None
                            st.session_state.locations = [l for l in st.session_state.locations if l["id"] != row['id']]
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

    if "form_version" not in st.session_state:
        st.session_state.form_version = 0

    default_count = current_data["count"] if current_data else 1
    count = st.selectbox("🔄 月に行く合計回数を選んでください", list(range(1, 11)), index=(default_count-1))

    with st.form(f"location_form_{st.session_state.form_version}", clear_on_submit=True):
        company = st.text_input("🏢 会社名", value=current_data["company"] if current_data else "")
        name = st.text_input("📍 現場名", value=current_data["name"] if current_data else "")
        address = st.text_input("🗺️ 現場住所（正しい住所を入れると距離を測ります）", value=current_data["address"] if current_data else "")
        
        st.markdown("---")
        st.markdown("### 📅 各回収日の詳細ルール設定")
        
        rules = []
        for i in range(1, count):
            st.markdown(f"**【{i}回目の集金】**")
            r_type = st.radio(f"{i}回目のルール選択", ["特になし", "○日まで", "○日〜○日の間", "○日ぴったり"], key=f"type_{i}")
            r_val = st.text_input(f"{i}回目の具体的な日付・期間 (例: 10、1-5)", key=f"val_{i}")
            rules.append({"step": i, "type": r_type, "val": r_val, "is_last": False})
        
        st.markdown(f"**🏁【最終集金日（{count}回目）のルール】 ※必須事項**")
        last_r_type = st.radio(f"最終集金のルール選択", ["特になし", "○日まで", "○日〜○日の間", "○日ぴったり"], key=f"type_last")
        last_r_val = st.text_input(f"最終集金の具体的な日付・期間 (例: 25、20-25)", key=f"val_last")
        rules.append({"step": count, "type": last_r_type, "val": last_r_val, "is_last": True})
        
        intervals = []
        if count >= 2:
            st.markdown("---")
            st.markdown("### ⏳ 間隔のルール")
            for i in range(1, count):
                next_label = f"{i+1}回目" if i+1 < count else "最終集金"
                span = st.number_input(f"「{i}回目」と「{next_label}」の間隔は何日以上空けますか？", min_value=0, max_value=30, value=0, key=f"span_{i}")
                intervals.append({"from": i, "to": i+1, "span": span})

        st.markdown("---")
        st.markdown("### 🗓️ 曜日・休日のルール")
        sat = st.checkbox("土曜日も入れてよい", value=True)
        sun = st.checkbox("日曜日も入れてよい", value=False)

        submitted = st.form_submit_button("更新する" if st.session_state.editing_id is not None else "現場を登録する")
        
        if submitted:
            if company and name and address:
                with st.spinner("🌍 住所から正確な位置を測定中..."):
                    lat, lon = get_lat_lon_ai(address)
                
                form_data = {
                    "company": company, "name": name, "address": address, "count": count,
                    "rules": rules, "intervals": intervals, "sat": sat, "sun": sun,
                    "lat": lat, "lon": lon
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
                    form_data["id"] = max([loc["id"] for loc in st.session_state.locations] + [0]) + 1
                    st.session_state.locations.append(form_data)
                    st.success(f"🎉 新しい現場「{name}」を追加しました！")
                    st.session_state.form_version += 1
                
                st.session_state.last_input = form_data
                st.rerun()
            else:
                st.error("会社名、現場名、住所は必須入力です。")
                
    if st.session_state.editing_id is not None:
        if st.button("編集をキャンセル"):
            st.session_state.editing_id = None
            st.rerun()

# ==========================================
# 2. 📅 スケジュール生成タブ（完全版・修正済）
# ==========================================
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
            
            # タスクプールを作成
            task_pool = []
            for loc in st.session_state.locations:
                for step_idx in range(loc["count"]):
                    task_pool.append({
                        "loc_id": loc["id"], "company": loc["company"], "name": loc["name"],
                        "lat": loc.get("lat", 0.0), "lon": loc.get("lon", 0.0),
                        "step": step_idx + 1, "rules": loc.get("rules", []),
                        "sat": loc.get("sat", True), "sun": loc.get("sun", False)
                    })
            
            # --- 🚀 距離＋ルールを考慮した生成ロジック ---
            current_schedule = {day.strftime('%Y-%m-%d'): [] for day in all_days}
            unassigned_tasks = sorted(task_pool, key=lambda x: (x['step'], x['loc_id']))

            # 最後に各現場を訪問した日を記録する辞書
            last_visited_day = {loc["id"]: None for loc in st.session_state.locations}

            # 前日の最終目的地を記憶する変数（最初は(0.0, 0.0)）
            prev_day_last_loc = (0.0, 0.0)
            last_active_day = None # 直前の稼働日を記録

            for day in all_days:
                day_str = day.strftime('%Y-%m-%d')
                d_num = day.day
                w = day.weekday()
                
                # もし前回の稼働日から2日以上空いたら（土日を挟むなど）、スタート位置をリセット
                if last_active_day is not None and (day - last_active_day).days > 1:
                    prev_day_last_loc = (0.0, 0.0)
                
                while len(current_schedule[day_str]) < max_tasks:
                    best_task_idx = -1
                    min_dist = 999999.0
                    
                    # 今日の直前の現場位置を取得
                    last_loc = (0.0, 0.0)
                    if current_schedule[day_str]:
                        last_loc = (current_schedule[day_str][-1]["lat"], current_schedule[day_str][-1]["lon"])
                    else:
                        # 今日の一件目なら、前日の最後の現場位置をベースにする
                        last_loc = prev_day_last_loc
                        
                    for idx, task in enumerate(unassigned_tasks):
                        # 1. 曜日制限チェック
                        if w == 5 and not task["sat"]: continue
                        if w == 6 and not task["sun"]: continue
                        
                        # 2. 日付詳細ルールチェック（インデント修正済）
                        rule = next((r for r in task["rules"] if r["step"] == task["step"]), None)
                        if rule and rule.get("val"):
                            try:
                                # 「○日〜○日の間」のチェック
                                if rule["type"] == "○日〜○日の間":
                                    s, e = map(int, rule["val"].split('-'))
                                    if not (s <= d_num <= e): continue
                                # 「○日まで」のチェック
                                elif rule["type"] == "○日まで":
                                    if d_num > int(rule["val"]): continue
                                # 「○日ぴったり」のチェック
                                elif rule["type"] == "○日ぴったり":
                                    if d_num != int(rule["val"]): continue
                            except (ValueError, TypeError):
                                pass
                        
                        # 3. 間隔ルールチェック
                        if task["step"] > 1:
                            last_day = last_visited_day[task["loc_id"]]
                            if last_day is not None:
                                days_passed = (day - last_day).days
                                loc_data = next(l for l in st.session_state.locations if l["id"] == task["loc_id"])
                                span_rule = next((span for span in loc_data.get("intervals", []) if span["to"] == task["step"]), None)
                                if span_rule and days_passed < span_rule["span"]:
                                    continue
                        
                        # 4. 距離の計算
                        dist = calculate_distance(last_loc, (task["lat"], task["lon"]))
                        if dist < min_dist:
                            min_dist = dist
                            best_task_idx = idx
                                
                    if best_task_idx != -1:
                        chosen_task = unassigned_tasks.pop(best_task_idx)
                        current_schedule[day_str].append(chosen_task)
                        last_visited_day[chosen_task["loc_id"]] = day
                        
                        # 今日訪問した最後の現場位置を、前日位置として常に更新
                        prev_day_last_loc = (chosen_task["lat"], chosen_task["lon"])
                        last_active_day = day # 稼働日を更新
                    else:
                        break # 条件に合う現場がもうない
            
            # --- 残った現場の安全分配処理 ---
            if unassigned_tasks:
                for task in unassigned_tasks:
                    for day in all_days:
                        day_str = day.strftime('%Y-%m-%d')
                        if len(current_schedule[day_str]) < max_tasks:
                            current_schedule[day_str].append(task)
                            break

            # 結果をセッション状態に保存
            st.session_state.schedule_results = {"calculated": True, "schedule": current_schedule}

    # スケジュール結果の表示（ボタンの外側）
    if st.session_state.schedule_results and st.session_state.schedule_results["calculated"]:
        st.success("🗺️ 各現場のルート距離を計算し、最も効率の良いスケジュールを生成しました！")
        for day_str, tasks in st.session_state.schedule_results["schedule"].items():
            if tasks:
                st.markdown(f"#### 📅 {day_str}")
                for t in tasks:
                    st.write(f"- 🏢 {t['company']} ： {t['name']} （{t['step']}回目）")
