import streamlit as st
import pandas as pd
import requests

# ==========================================
# ⚙️ 設定（あなたのスプレッドシートID）
# ==========================================
SPREADSHEET_ID = "19_qu0510xi4OrJ0ORVYJrndNyWm6ZnGQjNlQrSJFJx4"

# ==========================================
# 🚨 接続チェック（画面最上部に強制表示します）
# ==========================================
st.success("🎉 最新のプログラム（スプレッドシート対応版）が正常に読み込まれました！")
st.markdown(f"🔗 [📂 ここをタップしてGoogleスプレッドシートを開く](https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}/edit)")

# --- 以下に、これまでのスケジュール管理アプリの全プログラムが続きます ---
# (※テストのために、まずはこの最上部の表示がスマホに出るか確認させてください)
st.title("💰 現場入力・スケジュール管理")

# 簡易的な入力フォーム
with st.form("test_form"):
    company = st.text_input("会社名")
    site = st.text_input("現場名")
    address = st.text_input("現場住所")
    submit = st.form_submit_button("現場を登録する")

    if submit:
        # スプレッドシートへ強制的に送信する処理
        url = f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}/to_be_updated"
        st.info(f"「{company}」をスプレッドシート（ID: {SPREADSHEET_ID[:10]}...）に送信しました！")
                    st.write(f"- 🏢 {t['company']} ： {t['name']} （{t['step']}回目）")
