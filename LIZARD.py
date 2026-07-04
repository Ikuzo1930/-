import streamlit as st
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from geopy.geocoders import Nominatim
from geopy.exc import GeocoderTimedOut

st.set_page_config(page_title="集金スケジュール管理", layout="centered")

# 住所から緯度経度を取得する関数（キャッシュして高速化）
@st.cache_data(ttl=3600)
def get_lat_lon(address):
    try:
        geolocator = Nominatim(user_agent="money_collection_scheduler_2026")
        location = geolocator.geocode(address, timeout=5)
        if location:
            return location.latitude, location.longitude
    except GeocoderTimedOut:
        return None, None
    return None, None

# 2点間の簡易距離計算（三平方の定理）
def calculate_distance(p1, p2):
    if p1 == (0,0) or p2 == (0,0):
        return 999.0
    return np.sqrt((p1[0] - p2[0])**2 + (p1[1] - p2[1])**2)

# --- データの初期化 ---
if "locations" not in st.session_state:
    st.session_state.locations = []
if "editing_id" not in st.session_state:
    st.session_state.editing_id = None
if "last_input" not in st.session_state:
    st.session_state.last_input = None
if "schedule_results" not in st.session_state:
    st.session_state.schedule_results = None

def save_location(data):
    lat, lon = get_lat_lon(data["address"])
    data["lat"] = lat if lat else 0.0
    data["lon"] = lon if lon else 0.0
    
    if st.session_state.editing_id is not None:
        for i, loc in enumerate(st.session_state.locations):
            if loc["id"] == st.session_state.editing_id:
                st.session_state.locations[i] = data
                break
        st.session_state.editing_id = None
    else:
        data["id"] = max([loc["id"] for loc in st.session_state.locations] + [0]) + 1
        st.session_state.locations.append(data)
    st.session_state.last_input = data
    st.success("データを保存しました！")
    st.rerun()

# --- 画面の構築 ---
st.title("💰 集金スケジュール管理")

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
                        st.markdown(f"**📍 {row['name']}** ({row['address']})")
                        st.caption(f"月 {row['count']} 回訪問")
                    with col_btn1:
                        if st.button("編集", key=f"edit_{row['id']}"):
                            st.session_state.editing_id = row['id']
                            st.rerun()
                    with col_btn2:
                        if st.button("削除", key=f"del_{row['id']}"):
                            st.session_state.locations = [l for l in st.session_state.locations if l["id"] != row['id']]
                            st.rerun()

    st.divider()
    
    if st.session_state.editing_id is not None:
        st.subheader("📝 現場の条件を編集")
        current_data = next(l for l in st.session_state.locations if l["id"] == st.session_state.editing_id)
    else:
        st.subheader("➕ 新しい現場を追加")
        current_data = None

    if current_data is None and st.session_state.last_input is not None:
        if st.button("⏮️ 直前に登録した現場の条件をコピーする"):
            current_data = st.session_state.last_input.copy()
            current_data["name"] = "" 
            current_data["address"] = ""

    if current_data:
        default_count = current_data["count"]
    else:
        default_count = 1
        
    count = st.selectbox("🔄 月に行く合計回数を選んでください", list(range(1, 11)), index=(default_count-1))

    with st.form("location_form", clear_on_submit=False):
        company = st.text_input("🏢 会社名", value=current_data["company"] if current_data else "")
        name = st.text_input("📍 現場名", value=current_data["name"] if current_data else "")
        address = st.text_input("🗺️ 現場住所", value=current_data["address"] if current_data else "")
        
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
            st.markdown("### ⏳ 間隔のルール（※必要な場合だけ入力してください）")
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
                save_location({
                    "company": company, "name": name, "address": address, "count": count,
                    "rules": rules, "intervals": intervals, "sat": sat, "sun": sun
                })
            else:
                st.error("会社名、現場名、住所は必須入力です。")
                
    if st.session_state.editing_id is not None:
        if st.button("キャンセル"):
            st.session_state.editing_id = None
            st.rerun()

# ==========================================
# 2. 📅 スケジュール生成タブ（年・月選択に修正）
# ==========================================
with tab_schedule:
    st.subheader("📅 月間スケジュールの自動生成")
    
    # 【修正】カレンダーではなく、年と月をセレクトボックスで別々に選べるようにしました
    now = datetime.today()
    st.markdown("### 📅 スケジュールを組む月を選択してください")
    col_year, col_month = st.columns(2)
    with col_year:
        # 今年を中心に前後2年を選択肢にする
        target_year = st.selectbox("年", [now.year - 1, now.year, now.year + 1], index=1)
    with col_month:
        # 1〜12月を選択、デフォルトは現在の月
        target_month = st.selectbox("月", list(range(1, 13)), index=now.month - 1)
    
    st.markdown("---")
    st.markdown("### 🚗 1日の件数は何件から何件にしますか？")
    col_min, col_max = st.columns(2)
    with col_min:
        min_tasks = st.selectbox("最小件数", list(range(1, 11)), index=1)
    with col_max:
        max_tasks = st.selectbox("最大件数", list(range(1, 12)), index=4)
    st.markdown("---")
    
    if st.button("🚀 現在の現場データでスケジュールを自動計算する", type="primary"):
        if not st.session_state.locations:
            st.error("現場データが登録されていません。")
        else:
            # 選択された年・月から1日を生成
            start_date = datetime(target_year, target_month, 1)
            if target_month == 12:
                end_date = datetime(target_year + 1, 1, 1) - timedelta(days=1)
            else:
                end_date = datetime(target_year, target_month + 1, 1) - timedelta(days=1)
            
            days_in_month = (end_date - start_date).days + 1
            all_days = [start_date + timedelta(days=x) for x in range(days_in_month)]
            
            # 訪問すべき全タスクの切り出し
            task_pool = []
            for loc in st.session_state.locations:
                for step_idx in range(loc["count"]):
                    task_pool.append({
                        "loc_id": loc["id"], "company": loc["company"], "name": loc["name"],
                        "lat": loc["lat"], "lon": loc["lon"], "step": step_idx + 1,
                        "rules": loc["rules"], "intervals": loc["intervals"],
                        "sat": loc["sat"], "sun": loc["sun"]
                    })
            
            # 3つの候補用スケジュール格納庫
            candidates = {1: {}, 2: {}, 3: {}}
            
            # --- アルゴリズムによる割り当て（ルールベースの最適化選別） ---
            for cand_type in [1, 2, 3]:
                current_schedule = {day.strftime('%Y-%m-%d'): [] for day in all_days}
                loc_last_assigned = {}
                
                sorted_tasks = sorted(task_pool, key=lambda x: x['step'])
                
                for task in sorted_tasks:
                    assigned = False
                    for day in all_days:
                        day_str = day.strftime('%Y-%m-%d')
                        d_num = day.day
                        w = day.weekday() # 5=土, 6=日
                        
                        if w == 5 and not task["sat"]: continue
                        if w == 6 and not task["sun"]: continue
                        if len(current_schedule[day_str]) >= max_tasks: continue
                        
                        rule = next((r for r in task["rules"] if r["step"] == task["step"]), None)
                        if rule:
                            if rule["type"] == "○日まで" and d_num > int(rule["val"]): continue
                            if rule["type"] == "○日ぴったり" and d_num != int(rule["val"]): continue
                            if rule["type"] == "○日〜○日の間":
                                try:
                                    s, e = map(int, rule["val"].split('-'))
                                    if not (s <= d_num <= e): continue
                                except: pass
                        
                        if task["loc_id"] in loc_last_assigned:
                            prev_day = loc_last_assigned[task["loc_id"]]
                            gap = (day - prev_day).days
                            interval_cfg = next((inv for inv in task["intervals"] if inv["from"] == task["step"]-1), None)
                            if interval_cfg and gap < interval_cfg["span"]: continue

                        if len(current_schedule[day_str]) > 0:
                            last_task_today = current_schedule[day_str][-1]
                            dist = calculate_distance((task["lat"], task["lon"]), (last_task_today["lat"], last_task_today["lon"]))
                            
                            if cand_type == 1 and dist > 0.05: continue 
                            if cand_type == 2 and task["company"] != last_task_today["company"] and dist > 0.1: continue 

                        current_schedule[day_str].append(task)
                        loc_last_assigned[task["loc_id"]] = day
                        assigned = True
                        break
                        
                    if not assigned:
                        first_day_str = all_days[0].strftime('%Y-%m-%d')
                        current_schedule[first_day_str].append(task)

                formatted_schedule = []
                for day in all_days:
                    d_str = day.strftime('%Y-%m-%d')
                    if current_schedule[d_str]:
                        formatted_schedule.append({
                            "date_label": f"{day.month}月{day.day}日 ({['月','火','水','木','金','土','日'][day.weekday()]})",
                            "tasks": current_schedule[d_str]
                        })
                candidates[cand_type] = formatted_schedule

            st.session_state.schedule_results = {
                "month": target_month,
                "calculated": True,
                "candidates": candidates
            }

    # --- 結果の表示 ---
    if st.session_state.schedule_results and st.session_state.schedule_results["calculated"]:
        cand_data = st.session_state.schedule_results["candidates"]
        
        cand_tab1, cand_tab2, cand_tab3 = st.tabs([
            "📍 候補1: 移動距離最小（効率重視）", 
            "🏢 候補2: エリアまとまり重視", 
            "⚖️ 候補3: ゆったり均等（件数分散）"
        ])
        
        with cand_tab1:
            if not cand_data[1]: st.info("この条件で組めるスケジュールがありませんでした。")
            for day_info in cand_data[1]:
                st.markdown(f"#### 📅 {day_info['date_label']}")
                for t in day_info["tasks"]:
                    st.write(f"- 🏢 {t['company']} ： {t['name']} （{t['step']}回目）")
            
        with cand_tab2:
            if not cand_data[2]: st.info("この条件で組めるスケジュールがありませんでした。")
            for day_info in cand_data[2]:
                st.markdown(f"#### 📅 {day_info['date_label']}")
                for t in day_info["tasks"]:
                    st.write(f"- 🏢 {t['company']} ： {t['name']} （{t['step']}回目）")
                    
        with cand_tab3:
            if not cand_data[3]: st.info("この条件で組めるスケジュールがありませんでした。")
            for day_info in cand_data[3]:
                st.markdown(f"#### 📅 {day_info['date_label']}")
                for t in day_info["tasks"]:
                    st.write(f"- 🏢 {t['company']} ： {t['name']} （{t['step']}回目）")
