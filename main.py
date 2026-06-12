import streamlit as st
import yfinance as yf
import pandas as pd
import requests
import os
import sys  # 嵌入修正：用於判斷自動化排程執行入口
import urllib3
import re
from io import BytesIO, StringIO  # 嵌入修正：結合官方 Open API 與 BytesIO 徹底防禦雲端解碼崩潰
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
import plotly.graph_objects as go
from plotly.subplots import make_subplots

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ==========================================
# 【嵌入修正】：改用安全路徑，防止 Streamlit Cloud 啟動時找不到 __file__ 閃退
# ==========================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__)) if "__file__" in dir() else os.getcwd()
try:
    os.chdir(BASE_DIR)
except:
    pass


# ==========================================
# 【嵌入修正】：大盤實際交易曆法判定（精準匹配需求一、二）
# ==========================================
@st.cache_data(ttl=3600, show_spinner=False)
def get_tw_market_calendar():
    """ 獲取台股實際交易日序列，用以跨越六日與例假日精準計算連續上榜天數 """
    try:
        # 確保此處前面的空格與下方的 if 完全對齊 (4個空格)
        twii = yf.Ticker("^TWII").history(period="45d")
        if not twii.empty:
            return sorted(pd.to_datetime(twii.index.date).tolist())
    except:
        pass
    today = datetime.now()
    return [today - timedelta(days=x) for x in range(30) if (today - timedelta(days=x)).weekday() < 5]

# ==========================================
# 0. 實體 CSV 記憶快取與歷史追蹤系統
# ==========================================
def get_cache_filename(latest_market_date_str):
    # 嵌入修正：快取檔名改以「大盤最新實際交易日」命名，確保假日不重跑重複的檔案
    return f"ScanCache_APlan_{latest_market_date_str}.csv"


def get_history_filename():
    return "ScanHistory_Tracker.csv"

def load_csv_cache(latest_market_date_str):
    filename = get_cache_filename(latest_market_date_str)
    if os.path.exists(filename):
        try:
            df = pd.read_csv(filename, dtype={'股票代號': str})
            # 💡 核心修正：如果檔案存在但根本沒有資料，直接視為無效快取，回傳空字典，迫使網頁重新掃描
            if df.empty or len(df) == 0:
                return {}
            return df.set_index('股票代號').to_dict('index')
        except:
            return {}
    return {}

def save_csv_cache(cache_dict, latest_market_date_str):
    # 💡 核心修正：如果掃描出來的名單是空的，絕對不存檔，避免寫入一個空檔案污染明天的讀取
    if not cache_dict or len(cache_dict) == 0:
        return
    filename = get_cache_filename(latest_market_date_str)
    df = pd.DataFrame.from_dict(cache_dict, orient='index')
    df.reset_index(inplace=True)
    df.rename(columns={'index': '股票代號'}, inplace=True)
    df.to_csv(filename, index=False, encoding='utf-8-sig')

def update_scan_history(current_matched_list, latest_market_date_str):
    """ 嵌入修正：精準跨假日連續天數演算法，100% 保留您原始的所有回測指標欄位紀錄 """
    hist_file = get_history_filename()
    calendar = get_tw_market_calendar()

    # 找到大盤前一個實際開盤日
    prev_market_date_str = None
    try:
        current_dt = pd.to_datetime(latest_market_date_str)
        if current_dt in calendar:
            curr_idx = calendar.index(current_dt)
            if curr_idx > 0:
                prev_market_date_str = calendar[curr_idx - 1].strftime('%Y-%m-%d')
    except:
        pass

    cols = [
        '日期', '股票代號', '股票名稱', '最新交易日股價', '最近一日交易量',
        '交易次數', '勝率', '平均投資報酬率', '最高投資報酬率', '最低投資報酬率', '標準差',
        '連續X天', '進場條件', '出場條件'
    ]

    if os.path.exists(hist_file):
        try:
            hist_df = pd.read_csv(hist_file, dtype={'股票代號': str})
        except:
            hist_df = pd.DataFrame(columns=cols)
    else:
        hist_df = pd.DataFrame(columns=cols)

    # 避免重複寫入
    hist_df = hist_df[hist_df['日期'] != latest_market_date_str]
    new_rows = []

    for stock in current_matched_list:
        ticker = stock['股票代號']
        en_cond = stock['勝率最高方案'].split('；')[0].replace('[進] 60分K之KD且RSI<20 且 ', '')
        ex_cond = stock['勝率最高方案'].split('；')[1].replace('[出] ', '')

        v_vol = stock.get('最近一日交易量', '0 張')
        t_count = stock.get('交易次數', 0)
        w_rate = stock.get('勝率', '0.0%')
        avg_p = stock.get('平均投資報酬率', stock.get('投資報酬率', '0.0%'))
        max_p = stock.get('最高投資報酬率', '0.0%')
        min_p = stock.get('最低投資報酬率', '0.0%')
        s_std = stock.get('標準差', 0.0)

        consecutive_days = 1
        # 判定前一實際開盤日是否上榜，跨六日或例假日精準累積天數
        if prev_market_date_str:
            past_rows = hist_df[(hist_df['股票代號'] == ticker) & (hist_df['日期'] == prev_market_date_str)]
            if not past_rows.empty:
                consecutive_days = int(past_rows.iloc[-1]['連續X天']) + 1

        new_rows.append({
            '日期': latest_market_date_str, '股票代號': ticker, '股票名稱': stock['股票名稱'],
            '最新交易日股價': stock['最新價位'],
            '最近一日交易量': v_vol, '交易次數': t_count, '勝率': w_rate, '平均投資報酬率': avg_p,
            '最高投資報酬率': max_p, '最低投資報酬率': min_p, '標準差': s_std, '連續X天': consecutive_days,
            '進場條件': en_cond, '出場條件': ex_cond
        })

    if new_rows:
        new_df = pd.DataFrame(new_rows)
        for c in cols:
            if c not in new_df.columns: new_df[c] = None
        hist_df = pd.concat([hist_df, new_df], ignore_index=True)
        hist_df.to_csv(hist_file, index=False, encoding='utf-8-sig')


# ==========================================
# 1. 核心資料獲取與指標計算（100% 官方 Open API 對接）
# ==========================================
@st.cache_data(ttl=3600, show_spinner=False)
def get_market_stocks():
    """ 採用官方 Open API 接口，完全解決雲端 IP 阻擋與網頁 Big5 解碼死穴 """
    twse_dict, tpex_dict = {}, {}
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}

    try:
        res_l = requests.get("https://openapi.twse.com.tw/v1/opendata/t187ap03_L", headers=headers, timeout=15)
        if res_l.status_code == 200:
            for item in res_l.json():
                code = item.get('公司代號', '').strip()
                if len(code) == 4: twse_dict[code] = item.get('公司簡稱', '').strip()
    except:
        pass

    try:
        res_o = requests.get("https://openapi.twse.com.tw/v1/opendata/t187ap03_OTC", headers=headers, timeout=15)
        if res_o.status_code == 200:
            for item in res_o.json():
                code = item.get('公司代號', '').strip()
                if len(code) == 4: tpex_dict[code] = item.get('公司簡稱', '').strip()
    except:
        pass

    combined = {**twse_dict, **tpex_dict}
    if not combined:
        st.cache_data.clear()
    return twse_dict, tpex_dict


@st.cache_data(ttl=1800, show_spinner=False)
def fetch_60m_kline_data(ticker):
    """ 100% 保留原本抓取兩年大資料的設定 """
    import time
    for attempt in range(3):
        try:
            time.sleep(0.1)
            df = yf.Ticker(f"{ticker}.TW" if len(ticker) == 4 else f"{ticker}.TWO").history(period="730d",
                                                                                            interval="1h")
            if not df.empty: return df
        except:
            time.sleep(1.0)
    return pd.DataFrame()


@st.cache_data(ttl=600, show_spinner=False)
def fetch_60m_kline_short_data(ticker):
    """ 100% 保留原本快速篩選短資料的設定 """
    try:
        df = yf.Ticker(f"{ticker}.TW" if len(ticker) == 4 else f"{ticker}.TWO").history(period="3d", interval="1h")
        return df
    except:
        return pd.DataFrame()


def calc_rsi(df, period):
    delta = df['Close'].diff()
    up, down = delta.clip(lower=0), -1 * delta.clip(upper=0)
    ema_up = up.ewm(alpha=1 / period, adjust=False).mean()
    ema_down = down.ewm(alpha=1 / period, adjust=False).mean()
    return 100 - (100 / (1 + ema_up / ema_down))


def calc_macd(df, fast=12, slow=26, signal=9):
    ema_fast = df['Close'].ewm(span=fast, adjust=False).mean()
    ema_slow = df['Close'].ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    macd_hist = macd_line - signal_line
    return macd_line, signal_line, macd_hist


# ==========================================
# 2. A計畫：回測引擎 (基於 60分K) ── 100% 保留全部原有進出場策略
# ==========================================
def get_strategy_rules():
    entry_rules = {
        "碰觸BBAND下緣進場": lambda r: r['Close'] <= r['BB_DN'],
        "站上5T進場": lambda r: r['Close'] > r['5MA'],
        "站上10T進場": lambda r: r['Close'] > r['10MA'],
        "站上20T進場": lambda r: r['Close'] > r['20MA'],
        "站上60T進場": lambda r: r['Close'] > r['60MA'],
        "直接進場": lambda r: True
    }
    exit_rules = {
        "KD且RSI大於70且在BBAND中線以上出場": lambda r: r['9K'] > 70 and r['5_RSI'] > 70 and r['Close'] >= r['20MA'],
        "KD且RSI大於70出場": lambda r: r['9K'] > 70 and r['5_RSI'] > 70,
        "碰觸20T(布林中線)以上出場": lambda r: r['Close'] >= r['20MA'],
        "站上60T出場": lambda r: r['Close'] >= r['60MA'],
        "跌破5T出場(停損/利)": lambda r: r['Close'] < r['5MA'],
        "布林通道觸頂出場": lambda r: r['Close'] >= r['BB_UP'],
        "乖離率大於15%停利出場": lambda r: r['BIAS_20'] > 15.0,
        "MACD柱狀體翻綠出場": lambda r: r['MACD_Hist'] < 0
    }
    return entry_rules, exit_rules


def find_best_strategy(df):
    entry_rules, exit_rules = get_strategy_rules()
    best_combo_name = "無合適策略"
    best_stats = {"count": 0, "win_rate": 0.0, "avg_profit": 0.0, "max_profit": 0.0, "min_profit": 0.0,
                  "std_profit": 0.0, "en_key": None, "ex_key": None}
    max_win_rate = -1
    records = df.to_dict('records')

    for en_name, en_func in entry_rules.items():
        for ex_name, ex_func in exit_rules.items():
            trades, holding, buy_price = [], False, 0
            for row in records:
                if pd.isna(row.get('BB_DN')) or pd.isna(row.get('60MA')) or pd.isna(row.get('BIAS_20')) or pd.isna(
                    row.get('MACD_Hist')): continue
                if not holding:
                    if row['9K'] < 20 and row['5_RSI'] < 20 and en_func(row):
                        holding, buy_price = True, row['Close']
                else:
                    if ex_func(row):
                        holding = False
                        trades.append(((row['Close'] - buy_price) / buy_price) * 100)

            t_count = len(trades)
            if t_count > 2:
                win_rate = round((len([t for t in trades if t > 0]) / t_count) * 100, 1)
                avg_profit = round(sum(trades) / t_count, 2)
                max_profit = round(max(trades), 2)
                min_profit = round(min(trades), 2)
                std_profit = round(float(pd.Series(trades).std()), 2)

                if win_rate >= 70.0 and avg_profit > 0:
                    if win_rate > max_win_rate:
                        max_win_rate = win_rate
                        best_combo_name = f"[進] 60分K之KD且RSI<20 且 {en_name}；[出] {ex_name}"
                        best_stats = {
                            "count": t_count, "win_rate": win_rate, "avg_profit": avg_profit,
                            "max_profit": max_profit, "min_profit": min_profit, "std_profit": std_profit,
                            "en_key": en_name, "ex_key": ex_name
                        }
    return best_combo_name, best_stats


# ==========================================
# 3. 掃描工作節點 ── 100% 保留全部原本篩選過濾與成交量計算邏輯
# ==========================================
def worker_scan_stock_logic(ticker, stock_name):
    current_time_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    fail_res = {"股票名稱": stock_name, "掃描時間": current_time_str, "是否符合": False}

    try:
        df_short = fetch_60m_kline_short_data(ticker).copy()
        if df_short.empty or len(df_short) < 5: return fail_res

        last_date = df_short.index[-1].date()
        today_date = datetime.now().date()
        if (today_date - last_date).days > 5: return fail_res
        if df_short['Volume'].iloc[-1] == 0: return fail_res

        df_short['5_RSI'] = calc_rsi(df_short, 5)
        df_short['9K'] = (100 * (df_short['Close'] - df_short['Low'].rolling(9).min()) / (
                df_short['High'].rolling(9).max() - df_short['Low'].rolling(9).min())).ewm(com=2).mean()

        latest = df_short.iloc[-1]
        if pd.isna(latest['9K']) or pd.isna(latest['5_RSI']): return fail_res
        if latest['9K'] >= 20 or latest['5_RSI'] >= 20: return fail_res

        df = fetch_60m_kline_data(ticker).copy()
        if df.empty or len(df) < 60: return fail_res

        df['5_RSI'] = calc_rsi(df, 5)
        df['10_RSI'] = calc_rsi(df, 10)
        df['9K'] = (100 * (df['Close'] - df['Low'].rolling(9).min()) / (
                df['High'].rolling(9).max() - df['Low'].rolling(9).min())).ewm(com=2).mean()
        df['9D'] = df['9K'].ewm(com=2).mean()

        df['5MA'] = df['Close'].rolling(5).mean()
        df['10MA'] = df['Close'].rolling(10).mean()
        df['20MA'] = df['Close'].rolling(20).mean()
        df['60MA'] = df['Close'].rolling(60).mean()
        df['BB_UP'] = df['20MA'] + 2 * df['Close'].rolling(20).std()
        df['BB_DN'] = df['20MA'] - 2 * df['Close'].rolling(20).std()
        df['BIAS_20'] = ((df['Close'] - df['20MA']) / df['20MA']) * 100
        _, _, df['MACD_Hist'] = calc_macd(df)

        latest_full = df.iloc[-1]
        daily_volume_shares = df.loc[df.index.date == last_date, 'Volume'].sum()
        daily_volume_v = int(round(daily_volume_shares / 1000, 0))

        best_name, stats = find_best_strategy(df)

        if stats['count'] > 0 and stats['win_rate'] >= 70.0 and stats['avg_profit'] > 0:
            return {
                "股票名稱": stock_name, "掃描時間": current_time_str, "是否符合": True,
                "最新價位": round(latest_full['Close'], 2), "最近一日交易量": f"{daily_volume_v} 張",
                "勝率最高方案": best_name, "交易次數": stats['count'], "勝率": f"{stats['win_rate']}%",
                "平均投資報酬率": f"{stats['avg_profit']}%", "最高投資報酬率": f"{stats['max_profit']}%",
                "最低投資報酬率": f"{stats['min_profit']}%", "標準差": stats['std_profit'],
                "進場Key": stats['en_key'], "出場Key": stats['ex_key']
            }
    except Exception:
        pass
    return fail_res


# ==========================================
# 4. 圖表渲染模組 ── 100% 保留全部原本畫圖設定與座標限制
# ==========================================
def build_chart_figure(ticker_code, ticker_name, stock_data, view_mode):
    end_date = stock_data.index[-1]
    if view_mode == "近 1 周":
        target_start = end_date - pd.DateOffset(weeks=1)
    elif view_mode == "近 1 個月":
        target_start = end_date - pd.DateOffset(months=1)
    elif view_mode == "近 6 個月":
        target_start = end_date - pd.DateOffset(months=6)
    else:
        target_start = stock_data.index[0]

    x_range = [target_start.strftime('%Y-%m-%d %H:%M:%S'), end_date.strftime('%Y-%m-%d %H:%M:%S')]
    fig = make_subplots(rows=3, cols=1, shared_xaxes=True, vertical_spacing=0.03, row_heights=[0.5, 0.25, 0.25])

    fig.add_trace(
        go.Candlestick(x=stock_data.index, open=stock_data['Open'], high=stock_data['High'], low=stock_data['Low'],
                       close=stock_data['Close'], name='60分K線'), row=1, col=1)
    for ma, color in zip(['5MA', '10MA', '20MA', '60MA'], ['orange', 'purple', 'green', 'blue']):
        if ma in stock_data.columns:
            fig.add_trace(go.Scatter(x=stock_data.index, y=stock_data[ma], name=ma, line=dict(width=1.5, color=color)),
                          row=1, col=1)
    if 'BB_UP' in stock_data.columns and 'BB_DN' in stock_data.columns:
        fig.add_trace(go.Scatter(x=stock_data.index, y=stock_data['BB_UP'], name='BB_UP',
                                 line=dict(dash='dash', color='gray', width=1)), row=1, col=1)
        fig.add_trace(go.Scatter(x=stock_data.index, y=stock_data['BB_DN'], name='BB_DN',
                                 line=dict(dash='dash', color='gray', width=1)), row=1, col=1)

    if '5_RSI' in stock_data.columns and '10_RSI' in stock_data.columns:
        fig.add_trace(go.Scatter(x=stock_data.index, y=stock_data['5_RSI'], name='5T RSI', line=dict(color='blue')),
                      row=2, col=1)
        fig.add_trace(go.Scatter(x=stock_data.index, y=stock_data['10_RSI'], name='10T RSI',
                                 line=dict(color='lightblue', dash='dot')), row=2, col=1)
        fig.add_hline(y=20, line_dash="dot", line_color="red", row=2, col=1)
        fig.add_hline(y=80, line_dash="dot", line_color="green", row=2, col=1)

    if '9K' in stock_data.columns and '9D' in stock_data.columns:
        fig.add_trace(go.Scatter(x=stock_data.index, y=stock_data['9K'], name='9K', line=dict(color='orange')), row=3,
                      col=1)
        fig.add_trace(
            go.Scatter(x=stock_data.index, y=stock_data['9D'], name='9D', line=dict(color='purple', dash='dot')), row=3,
            col=1)
        fig.add_hline(y=20, line_dash="dot", line_color="red", row=3, col=1)
        fig.add_hline(y=80, line_dash="dot", line_color="green", row=3, col=1)

    fig.update_layout(height=850, hovermode="x unified", title=f"{ticker_code} {ticker_name} - 60分K 技術分析",
                      xaxis_rangeslider_visible=False)
    fig.update_xaxes(range=x_range, rangebreaks=[dict(bounds=["sat", "mon"]), dict(bounds=[14, 9], pattern="hour")])
    return fig


# ==========================================
# 【嵌入修正】：4. 全自動無介面背景更新模組（供需求三自動排程呼叫）
# ==========================================
def run_headless_automation():
    calendar = get_tw_market_calendar()
    latest_market_date_str = calendar[-1].strftime('%Y-%m-%d') if calendar else datetime.now().strftime('%Y-%m-%d')
    print(f"📅 當前大盤最新有效交易日為: {latest_market_date_str}")

    # 💡 【核心修正】：已經刪除「若檔案存在就 return 跳過」的機制。
    # 這樣才能確保 08:05, 09:05... 每小時喚醒時，都會強制抓取最新股價並覆蓋檔案！

    twse, tpex = get_market_stocks()
    stock_dict = {**twse, **tpex}

    matched = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(worker_scan_stock_logic, code, name): code for code, name in stock_dict.items()}
        for future in as_completed(futures):
            ticker = futures[future]
            try:
                res = future.result()
                if res and res.get('是否符合'):
                    res['股票代號'] = ticker
                    res['currently_status'] = "🟢 已滿足進場條件"
                    matched.append(res)
            except:
                pass

    # 覆蓋並校正實體 CSV
    save_csv_cache({x['股票代號']: x for x in matched}, latest_market_date_str)
    update_scan_history(matched, latest_market_date_str)
    print(f"🏁 自動化更新完畢！今日共計成功儲存 {len(matched)} 檔最新紀錄。")

# ==========================================
# 5. UI 介面模組 ── 100% 保留全部原有互動與分頁設定
# ==========================================
def render_custom_stock_tab():
    st.subheader("🎯 自選股分析與圖表 (60分K線)")
    col1, col2 = st.columns([2, 1])
    with col1:
        ticker = st.text_input("輸入股票代碼", "2330")
    with col2:
        view_mode = st.selectbox("圖表觀看區間", ["近 1 周", "近 1 個月", "近 6 個月", "全部"], index=2,
                                 key="custom_view")

    if st.button("開始分析與繪圖"):
        with st.spinner("正在讀取資料與畫圖..."):
            df = fetch_60m_kline_data(ticker)
            if not df.empty:
                df['5_RSI'] = calc_rsi(df, 5)
                df['10_RSI'] = calc_rsi(df, 10)
                df['9K'] = (100 * (df['Close'] - df['Low'].rolling(9).min()) / (
                        df['High'].rolling(9).max() - df['Low'].rolling(9).min())).ewm(com=2).mean()
                df['9D'] = df['9K'].ewm(com=2).mean()
                df['5MA'], df['10MA'], df['20MA'], df['60MA'] = df['Close'].rolling(5).mean(), df['Close'].rolling(
                    10).mean(), df['Close'].rolling(20).mean(), df['Close'].rolling(60).mean()
                df['BB_UP'], df['BB_DN'] = df['20MA'] + 2 * df['Close'].rolling(20).std(), df['20MA'] - 2 * df[
                    'Close'].rolling(20).std()
                df['BIAS_20'] = ((df['Close'] - df['20MA']) / df['20MA']) * 100
                _, _, df['MACD_Hist'] = calc_macd(df)

                best_name, stats = find_best_strategy(df)
                st.success("✅ 計算完成！")

                # 💡 新增：獲取股票名稱字典並組合完整名稱
                try:
                    twse, tpex = get_market_stocks()
                    stock_dict = {**twse, **tpex}
                except:
                    stock_dict = {}

                # 組合出例如 "2330 台積電" 的格式，若找不到名稱就顯示原本的代碼
                stock_name = stock_dict.get(ticker, "")
                full_display_name = f"{ticker} {stock_name}".strip() if stock_name else ticker

                m1, m2, m3 = st.columns(3)
                m1.metric("最佳策略", "組合見詳細資訊" if len(best_name) > 15 else best_name)
                m2.metric("勝率 / 交易次數", f"{stats['win_rate']}% / {stats['count']}次")
                m3.metric("平均投資報酬", f"{stats['avg_profit']}%")

                # 💡 修改：將提示訊息與畫圖函數的傳入名稱改為組合好的 full_display_name
                st.info(f"💡 **【{full_display_name}】最佳策略：** {best_name}")
                fig = build_chart_figure(ticker, full_display_name, df, view_mode)
                st.plotly_chart(fig, use_container_width=True)
            else:
                st.error("查無資料，請確認代碼是否正確。")


def render_full_market_tab():
    if 'matched_results' not in st.session_state:
        st.session_state.matched_results = []
    st.info(
        "💡 **A計畫 60分K 全市場掃描器**\n" "1. 自動排除已下市、暫停交易與零交易量之公司。\n" "2. 嚴格門檻：勝率 >= 70% 且 投資報酬率 > 0。\n" "3. 採用官方 Open API 數據對接，100% 根除海外雲端 IP 連線阻擋與編碼問題。")

    calendar = get_tw_market_calendar()
    latest_market_date_str = calendar[-1].strftime('%Y-%m-%d') if calendar else datetime.now().strftime('%Y-%m-%d')
    st.write(f"📅 當前有效大盤交易日：**{latest_market_date_str}** (六日與例假日會自動與此日期同步)")

    # 💡 核心優化：如果 session_state 裡本來就有今天跑好的資料，網頁一打開直接秀出來，不必重複等
    if st.session_state.matched_results:
        matched = st.session_state.matched_results
    else:
        # 預設嘗試讀取快取庫
        matched_dict = load_csv_cache(latest_market_date_str)
        matched = list(matched_dict.values()) if matched_dict else []
        if matched and not st.session_state.matched_results:
            st.session_state.matched_results = matched

    # 如果有成功讀取到「健康的快取」，畫面上提示一下
    if matched and not st.session_state.get('just_scanned', False):
        st.success(f"ℹ️ 偵測到今日大盤檔案已存在快取庫，系統已秒速載入歷史紀錄！(若欲即時更新，請點擊下方按鈕強制重跑)")

    # 🚀 當您「點擊按鈕」，代表要現場看最新狀況，此處強制無視快取、百分之百當場重跑！
    if st.button("🚀 開始全市場 60分K 精準掃描", use_container_width=True):
        st.session_state.just_scanned = True
        st.subheader("🚀 60分K 極速掃描結果")

        with st.spinner("⏳ 正在獲取最新官方 Open API 股票清單並強制沖刷重跑..."):
            twse, tpex = get_market_stocks()
            stock_dict = {**twse, **tpex}
            all_stocks = list(stock_dict.keys())

        if not all_stocks:
            st.error("❌ 無法取得股票清單！")
            return

        matched = []
        entry_rules, _ = get_strategy_rules()
        total_tasks = len(all_stocks)
        processed_tasks = 0

        progress_bar = st.progress(0.0)
        table_placeholder = st.empty()

        def update_table():
            if matched:
                display_df = pd.DataFrame(matched).copy()
                display_df['目前策略狀態'] = "🟢 已滿足進場條件"
                cols = ['股票代號', '股票名稱', '最新價位', '最近一日交易量', '勝率最高方案', '交易次數', '勝率',
                        '平均投資報酬率', '最高投資報酬率', '最低投資報酬率', '標準差', '目前策略狀態']
                display_df = display_df[[c for c in cols if c in display_df.columns]]
                table_placeholder.dataframe(display_df, use_container_width=True)

        # 10 個 Workers 現場暴速衝刺，重寫乾淨檔案覆蓋掉壞快取！
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(worker_scan_stock_logic, code, stock_dict[code]): code for code in all_stocks}
            for future in as_completed(futures):
                processed_tasks += 1
                ticker = futures[future]
                try:
                    res = future.result()
                    if res and res.get('是否符合'):
                        row_data = res.copy()
                        row_data['股票代號'] = ticker
                        row_data['currently_status'] = "🟢 已滿足進場條件"
                        matched.append(row_data)
                        update_table()
                except:
                    pass
                progress_bar.progress(min(processed_tasks / total_tasks, 1.0))

        # 覆蓋並校正實體 CSV
        save_csv_cache({x['股票代號']: x for x in matched}, latest_market_date_str)
        update_scan_history(matched, latest_market_date_str)
        st.session_state.matched_results = matched
        st.success(f"🏁 全市場掃描完畢！共計篩選出 {len(matched)} 檔黃金個股。")

    if st.session_state.matched_results and len(st.session_state.matched_results) > 0:
        st.markdown("---")
        st.subheader("📊 檢視本次上榜個股 K 線圖表")

        # 💡 核心修正：確保 matched_results 內確實有字典和欄位，才去生成下拉選單，100% 根除 KeyError
        options = []
        for x in st.session_state.matched_results:
            if isinstance(x, dict) and '股票代號' in x and '勝率' in x:
                options.append(f"{x['股票代號']} - {x['股票名稱']} (勝率: {x['勝率']})")

        if options:
            selected_option = st.selectbox("選擇要檢視的股票", options, key="market_view_select")
            if selected_option:
                selected_ticker = selected_option.split(" ")[0]
                with st.spinner(f"正在繪製 {selected_ticker} 圖表..."):
                    df_chart = fetch_60m_kline_data(selected_ticker)
                    if not df_chart.empty:
                        df_chart['5_RSI'] = calc_rsi(df_chart, 5)
                        df_chart['10_RSI'] = calc_rsi(df_chart, 10)
                        df_chart['9K'] = (100 * (df_chart['Close'] - df_chart['Low'].rolling(9).min()) / (
                                    df_chart['High'].rolling(9).max() - df_chart['Low'].rolling(9).min())).ewm(
                            com=2).mean()
                        df_chart['9D'] = df_chart['9K'].ewm(com=2).mean()
                        df_chart['5MA'], df_chart['10MA'], df_chart['20MA'], df_chart['60MA'] = df_chart[
                            'Close'].rolling(5).mean(), df_chart['Close'].rolling(10).mean(), df_chart['Close'].rolling(
                            20).mean(), df_chart['Close'].rolling(60).mean()
                        df_chart['BB_UP'], df_chart['BB_DN'] = df_chart['20MA'] + 2 * df_chart['Close'].rolling(
                            20).std(), df_chart['20MA'] - 2 * df_chart['Close'].rolling(20).std()

                        fig = build_chart_figure(selected_ticker, "", df_chart, "近 1 個月")
                        st.plotly_chart(fig, use_container_width=True)


def render_history_tracker_tab():
    st.subheader("📜 歷日上榜標的追蹤動態庫")
    hist_file = get_history_filename()
    entry_rules, _ = get_strategy_rules()

    if os.path.exists(hist_file):
        try:
            target_df = pd.read_csv(hist_file, dtype={'股票代號': str})
            if not target_df.empty:
                total_rows = len(target_df)
                progress_bar = st.progress(0.0)
                status_list = []

                for i, row in target_df.iterrows():
                    ticker = str(row['股票代號'])
                    df_temp = fetch_60m_kline_data(ticker)

                    if not df_temp.empty and len(df_temp) >= 60:
                        df_temp['5MA'] = df_temp['Close'].rolling(5).mean()
                        df_temp['10MA'] = df_temp['Close'].rolling(10).mean()
                        df_temp['20MA'] = df_temp['Close'].rolling(20).mean()
                        df_temp['60MA'] = df_temp['Close'].rolling(60).mean()
                        df_now_dn = df_temp['20MA'] - 2 * df_temp['Close'].rolling(20).std()

                        df_temp['5_RSI'] = calc_rsi(df_temp, 5)
                        df_temp['9K'] = (100 * (df_temp['Close'] - df_temp['Low'].rolling(9).min()) / (
                                    df_temp['High'].rolling(9).max() - df_temp['Low'].rolling(9).min())).ewm(
                            com=2).mean()

                        latest_bar = df_temp.iloc[-1].to_dict()
                        latest_bar['BB_DN'] = df_now_dn.iloc[-1]

                        en_key = row['進場條件']
                        en_func = entry_rules.get(en_key)

                        if en_func and latest_bar['9K'] < 20 and latest_bar['5_RSI'] < 20 and en_func(latest_bar):
                            status_list.append("🟢 已滿足進場條件")
                        else:
                            status_list.append("⚪ 尚未滿足進場條件")
                    else:
                        status_list.append("⚪ 資料暫無")

                    progress_bar.progress(min((i + 1) / total_rows, 1.0))

                target_df['目前策略狀態'] = status_list
                final_cols = [
                    '日期', '股票代號', '股票名稱', '最新交易日股價', '最近一日交易量',
                    '交易次數', '勝率', '平均投資報酬率', '最高投資報酬率', '最低投資報酬率', '標準差',
                    '連續X天', '進場條件', '出場條件', '目前策略狀態'
                ]
                cols_to_display = [c for c in final_cols if c in target_df.columns]
                st.dataframe(target_df[cols_to_display], use_container_width=True)
            else:
                st.info("歷史紀錄檔為空。")
        except Exception as e:
            st.error(f"讀取錯誤: {e}")
    else:
        st.info("💡 尚未建立歷史追蹤紀錄。")


def main():
    # 【嵌入修正】：配合排程自動更新。若帶有 --automation 參數，直接背景跑完存檔並結束，不啟動 UI
    if len(sys.argv) > 1 and sys.argv[1] == "--automation":
        run_headless_automation()
        return

    st.set_page_config(page_title="A計畫：60分K線全市場全自動攔截系統", layout="wide")
    st.title("📈 A計畫：60分K線超賣區全自動多策略攔截系統")
    tab1, tab2, tab3 = st.tabs(["🎯 自選股個股分析", "🚀 全市場極速掃描", "📜 歷日上榜動態追蹤"])
    with tab1: render_custom_stock_tab()
    with tab2: render_full_market_tab()
    with tab3: render_history_tracker_tab()


if __name__ == "__main__":
    main()