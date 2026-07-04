import streamlit as st
import pandas as pd
import numpy as np
import json
import os
from datetime import datetime, timedelta

# ==========================================
# ⚙️ データファイル保存設定（バグ修正版）
# ==========================================
DATA_FILE = "data.json"

def load_from_file():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            return []
    return []

def save_to_file(locations):
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(locations, f, ensure_ascii=False, indent=4)
    except Exception as e:
        st.error(f"❌ ファイルへの保存に失敗しました: {str(e)}")

# アプリ起動時にファイルからデータを読み込む
if "locations" not in st.session_state:
    st.session_state.locations = load_from_file()

# 状態の初期化
if "editing_id" not in st.session_state: st.session_state.editing_id = None
if "last_input" not in st.session_state: st.session_state.last_input = None
if "schedule_results" not in st.session_state: st.session_state.schedule_results = None

st.set_page_config(page_title="集金スケジュール管理", layout="centered")

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
                            # 削除された現場が編集中のものなら、編集状態を安全に解除（エラー対策）
                            if st.session_state.editing_id == row['id']:
                                st.session_state.editing_id = None
                            
                            st.session_state.locations = [l for l in st.session_state.locations if l["id"] != row['id']]
                            save_to_file(st.session_state.locations)
                            st.success("現場を削除しました。")
                            st.rerun()

    st.divider()
    
    # --- 編集フォームの取得処理 ---
    current_data = None
    if st.session_state.editing_id is not None:
        # 安全にデータを検索し、万が一見つからない場合は編集状態を解除する（StopIteration対策）
        found = [l for l in st.session_state.locations if l["id"] == st.session_state.editing_id]
        if found:
            st.subheader("📝 現場の条件を編集")
            current_data = found[0]
        else:
            st.session_state.editing_id = None

    if current_data is None:
        st.subheader("➕ 新しい現場を追加")
        if st.session_state.last_input is not None:
            if st.button("⏮️ 直前に登録した現場の条件をコピーする"):
                current_data = st.session_state.last_input.copy()
                current_data["name"] = "" 
                current_data["address"] = ""

    default_count = current_data["count"] if current_data else 1
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
                form_data = {
                    "company": company, "name": name, "address": address, "count": count,
                    "rules": rules, "intervals": intervals, "sat": sat, "sun": sun,
                    "lat": 0.0, "lon": 0.0
                }
                
                # 【バグ修正】編集モードなら上書き、新規なら追加するロジック
                if st.session_state.editing_id is not None:
                    form_data["id"] = st.session_state.editing_id
                    for idx, loc in enumerate(st.session_state.locations):
                        if loc["id"] == st.session_state.editing_id:
                            st.session_state.locations[idx] = form_data
                            break
                    st.session_state.editing_id = None
                    st.success("✨ 現場の情報を更新（上書き）しました！")
                else:
                    form_data["id"] = max([loc["id"] for loc in st.session_state.locations] + [0]) + 1
                    st.session_state.locations.append(form_data)
                    st.success("💾 新しい現場を登録しました！")
                
                st.session_state.last_input = form_data
                save_to_file(st.session_state.locations)
                st.rerun()
            else:
                st.error("会社名、現場名、住所は必須入力です。")
                
    if st.session_state.editing_id is not None:
        if st.button("編集をキャンセル"):
            st.session_state.editing_id = None
            st.rerun()

# ==========================================
# 2. 📅 スケジュール生成タブ
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
    
    if st.button("🚀 登録済みデータからスケジュールを自動生成する", type="primary"):
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
                    task_pool.append({
                        "loc_id": loc["id"], "company": loc["company"], "name": loc["name"],
                        "step": step_idx + 1, "rules": loc.get("rules", []),
                        "sat": loc.get("sat", True), "sun": loc.get("sun", False)
                    })
            
            current_schedule = {day.strftime('%Y-%m-%d'): [] for day in all_days}
            for task in task_pool:
                for day in all_days:
                    day_str = day.strftime('%Y-%m-%d')
                    if len(current_schedule[day_str]) < max_tasks:
                        current_schedule[day_str].append(task)
                        break
            
            st.session_state.schedule_results = {"calculated": True, "schedule": current_schedule}

    if st.session_state.schedule_results and st.session_state.schedule_results["calculated"]:
        st.success("🗓️ スケジュールの生成に成功しました！「登録済み一覧」がバッチリ反映されています。")
        for day_str, tasks in st.session_state.schedule_results["schedule"].items():
            if tasks:
                st.markdown(f"#### 📅 {day_str}")
                for t in tasks:
                    st.write(f"- 🏢 {t['company']} ： {t['name']} （{t['step']}回目）")
