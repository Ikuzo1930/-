import streamlit as st
import pandas as pd
import requests
import json

# 保存先の箱（一度入れたデータを記憶する場所）
JSON_BIN_URL = "https://api.jsonbin.io/v3/b/66180df6ad19ca34f857e4e0"
HEADERS = {"X-Master-Key": "$2b$10$wE9S8JqP68wE9S8JqP68wE9S8JqP68wE9S8JqP68wE9S8JqP68wE"}

st.set_page_config(page_title="集金スケジュール管理", page_icon="💰", layout="centered")

# データを読み込む処理
if "targets" not in st.session_state:
    try:
        res = requests.get(f"{JSON_BIN_URL}/latest", headers=HEADERS)
        if res.status_code == 200:
            st.session_state.targets = res.json().get("record", {}).get("targets", [])
        else:
            st.session_state.targets = []
    except:
        st.session_state.targets = []

targets = st.session_state.targets

# データを保存する処理
def save_data():
    try:
        payload = {"targets": st.session_state.targets}
        requests.put(JSON_BIN_URL, json=payload, headers=HEADERS)
    except:
        st.error("データの保存に失敗しました。")

st.title("💰 集金スケジュール管理")

tab1, tab2 = st.tabs(["📋 現場管理", "📅 スケジュール生成"])

with tab1:
    st.header("🏢 登録済みの会社・現場一覧")
    
    if not targets:
        st.info("登録された現場はまだありません。下のフォームから追加してください。")
    else:
        for idx, t in enumerate(targets):
            with st.expander(f"🏢 {t['company']} ： {t['name']}"):
                st.write(f"📍 **住所:** {t['address']}")
                st.write(f"🔄 **訪問ルール:** 月 {t['frequency']} 回訪問")
                
                if st.button("削除", key=f"del_{idx}"):
                    targets.pop(idx)
                    st.session_state.targets = targets
                    save_data()
                    st.rerun()

    st.write("---")
    st.header("➕ 新しい現場を追加")
    
    with st.form("add_form", clear_on_submit=True):
        new_company = st.text_input("会社名")
        new_name = st.text_input("現場名")
        new_address = st.text_input("現場住所")
        new_freq = st.number_input("月の訪問回数", min_value=1, max_value=10, value=1)
        
        submit = st.form_submit_button("現場を登録する")
        
        if submit:
            if new_company and new_name and new_address:
                new_target = {
                    "company": new_company,
                    "name": new_name,
                    "address": new_address,
                    "frequency": new_freq
                }
                targets.append(new_target)
                st.session_state.targets = targets
                save_data()
                st.success(f"「{new_name}」を登録しました！")
                st.rerun()
            else:
                st.error("入力漏れがあります。")

with tab2:
    st.header("🗓️ スケジュールの自動生成")
    if st.button("スケジュールを生成する"):
        if not targets:
            st.warning("現場が登録されていません。")
        else:
            st.info("ルート計算中...")
            for t in targets:
                st.write(f"・{t['company']} ({t['name']}) -> 📍 {t['address']}")
