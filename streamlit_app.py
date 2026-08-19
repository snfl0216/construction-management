import streamlit as st
from sqlalchemy import create_engine, text
import pandas as pd
from datetime import datetime, date
import io
import calendar as pycal

st.set_page_config(page_title="건설 현장 정산 & 지연 관리 시스템", layout="wide")
st.title("🏗️ 건설 현장 정산 & 지연 관리 시스템")

# ⚠️ 관리자 비밀번호
ADMIN_PASSWORD = "chdan1576**"

CLAIM_TYPES = ["선급금", "기성금", "중도금", "잔금", "추가금", "정산금", "AS", "시공부자재"]

engine = create_engine("sqlite:///construction_v6.db")

# --------------------------------------------------------------------------
# DB 초기화 — 두 데이터 파이프라인 완전히 분리
# --------------------------------------------------------------------------
with engine.connect() as conn:
    # ===== 일일수금관리(이력) 기준 : 기성청구현황 / 캘린더 / 리스크현장 =====
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS claims (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            site_name TEXT,
            manager TEXT,
            claim_type TEXT DEFAULT '기성금',
            claim_date TEXT,
            original_due_date TEXT,
            current_due_date TEXT,
            claim_amount INTEGER DEFAULT 0,
            status TEXT DEFAULT '입금대기',
            last_flagged_due_date TEXT
        );
    """))
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            claim_id INTEGER,
            payment_date TEXT,
            payment_amount INTEGER DEFAULT 0
        );
    """))
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS claim_delay_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            claim_id INTEGER,
            event_type TEXT,
            changed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            old_due_date TEXT,
            new_due_date TEXT,
            payment_date TEXT,
            delay_days INTEGER DEFAULT 0,
            reason TEXT
        );
    """))
    # ===== 현장별 미수관리(미수내역) 기준 : 현장별 미수현황 / 완불현장 / 계약현황 =====
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS site_receivables (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            site_name TEXT,
            company_name TEXT,
            manager TEXT,
            branch TEXT,
            contract_code TEXT,
            contract_date TEXT,
            start_date TEXT,
            completion_date TEXT,
            contract_yearmonth TEXT,
            contract_amount INTEGER DEFAULT 0,
            change_amount INTEGER DEFAULT 0,
            total_paid INTEGER DEFAULT 0,
            unpaid_balance INTEGER DEFAULT 0,
            progress_rate REAL DEFAULT 0,
            invoice_progress_rate REAL DEFAULT 0,
            invoice_issue_rate REAL DEFAULT 0,
            is_active INTEGER DEFAULT 1,
            status_label TEXT
        );
    """))
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS site_receivable_details (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            site_receivable_id INTEGER,
            detail_type TEXT,
            detail_date TEXT,
            amount INTEGER DEFAULT 0,
            note TEXT
        );
    """))
    conn.commit()


# --------------------------------------------------------------------------
# 공용 함수
# --------------------------------------------------------------------------
def parse_amount(v):
    if v is None:
        return 0
    if isinstance(v, (int, float)):
        if pd.isna(v):
            return 0
        return int(v)
    s = str(v).strip().replace(",", "").replace("원", "").replace(" ", "")
    if s in ("", "-", "nan"):
        return 0
    try:
        return int(float(s))
    except ValueError:
        return 0


def safe_date(v):
    if v is None:
        return None
    if isinstance(v, str):
        v = v.strip()
        if v == "" or v.lower() == "nat":
            return None
        try:
            return datetime.strptime(v[:10], "%Y-%m-%d").date()
        except ValueError:
            return None
    try:
        d = pd.to_datetime(v)
        if pd.isna(d):
            return None
        return d.date()
    except Exception:
        return None


def fmt_money(x):
    try:
        if pd.isna(x):
            return "0"
    except (TypeError, ValueError):
        pass
    try:
        return f"{int(x):,}"
    except (TypeError, ValueError):
        return str(x)


def fmt_money_cols(df, cols):
    df = df.copy()
    for c in cols:
        if c in df.columns:
            df[c] = df[c].apply(fmt_money)
    return df


def render_html_table(df, money_cols=None, left_cols=None):
    """현장명만 왼쪽, 금액은 오른쪽(콤마), 나머진 가운데 정렬로 확실하게 고정해서 그린다."""
    money_cols = set(money_cols or [])
    left_cols = set(left_cols if left_cols is not None else ["현장명"])
    d = df.copy()
    html = "<div style='overflow-x:auto;'><table style='width:100%;border-collapse:collapse;font-size:13px;'>"
    html += "<thead><tr>"
    for col in d.columns:
        align = "right" if col in money_cols else ("left" if col in left_cols else "center")
        html += (f"<th style='padding:6px 10px;border-bottom:2px solid #ddd;background:#fafafa;"
                  f"text-align:{align};white-space:nowrap;'>{col}</th>")
    html += "</tr></thead><tbody>"
    for _, row in d.iterrows():
        html += "<tr>"
        for col in d.columns:
            val = row[col]
            if col in money_cols:
                try:
                    val_disp = f"{int(val):,}"
                except (TypeError, ValueError):
                    val_disp = "" if pd.isna(val) else str(val)
                align = "right"
            else:
                val_disp = "" if pd.isna(val) else str(val)
                align = "left" if col in left_cols else "center"
            html += (f"<td style='padding:5px 10px;border-bottom:1px solid #eee;"
                      f"text-align:{align};white-space:nowrap;'>{val_disp}</td>")
        html += "</tr>"
    html += "</tbody></table></div>"
    st.markdown(html, unsafe_allow_html=True)


def is_overdue(current_due_date, status, today):
    if status in ("완납", "확인필요"):
        return False
    d = safe_date(current_due_date)
    return d is not None and d < today


def display_status(status, current_due_date, today):
    if status in ("완납", "확인필요"):
        return status
    overdue = is_overdue(current_due_date, status, today)
    if status == "일부입금":
        return "일부입금(지연)" if overdue else "일부입금"
    return "지연중" if overdue else "입금대기"


def calc_delay_days(original_due_date, ref_date):
    orig = safe_date(original_due_date)
    if orig is None or ref_date is None:
        return 0
    return max(0, (ref_date - orig).days)


def claim_severity(delay_count, delay_days):
    if delay_days >= 90:
        sev = 3
    elif delay_days >= 60:
        sev = 2
    elif delay_days >= 30:
        sev = 1
    else:
        sev = 0
    if delay_count >= 3:
        sev = max(sev, 1)
    return sev


SEVERITY_LABEL = {0: "정상", 1: "🟡 주의", 2: "🟠 경고", 3: "🔴 심각"}


def run_daily_delay_check():
    today_str = date.today().isoformat()
    with engine.connect() as conn:
        open_claims = conn.execute(text("""
            SELECT id, current_due_date, original_due_date, last_flagged_due_date, status
            FROM claims WHERE status IN ('입금대기', '일부입금')
        """)).fetchall()
        for cid, cur_due, orig_due, last_flag, status in open_claims:
            due_d = safe_date(cur_due)
            if due_d is None or due_d >= date.today():
                continue
            if last_flag == cur_due:
                continue
            delay_days = calc_delay_days(orig_due, date.today())
            conn.execute(text("""
                INSERT INTO claim_delay_history (claim_id, event_type, old_due_date, new_due_date, delay_days, reason)
                VALUES (:cid, '자동지연', :due, :due, :ddays, '예정일 경과 자동 감지')
            """), {"cid": cid, "due": cur_due, "ddays": delay_days})
            conn.execute(text("UPDATE claims SET last_flagged_due_date=:due WHERE id=:cid"), {"due": cur_due, "cid": cid})
        conn.commit()


run_daily_delay_check()

st.sidebar.markdown("### 🔐 관리자 로그인")
pw_input = st.sidebar.text_input("관리자 비밀번호", type="password")
is_admin = pw_input == ADMIN_PASSWORD
if pw_input:
    if is_admin:
        st.sidebar.success("관리자 모드 ✅")
    else:
        st.sidebar.error("비밀번호 틀림")

tab_receivable, tab_progress, tab_calendar, tab_risk, tab_contract, tab_admin = st.tabs([
    "📋 현장별 미수현황", "📊 기성청구현황", "📅 입금 캘린더", "🚨 리스크 현장", "📈 계약현황", "🔐 관리자"
])

# ==========================================================================
# TAB: 현장별 미수현황  (미수내역 엑셀 미러링)
# ==========================================================================
with tab_receivable:
    st.subheader("📋 현장별 미수현황")
    with engine.connect() as conn:
        sr_df = pd.read_sql("SELECT * FROM site_receivables ORDER BY contract_date;", conn)

    if sr_df.empty:
        st.info("데이터가 없습니다. '🔐 관리자' 탭에서 '현장별 미수관리' 엑셀을 업로드해주세요.")
    else:
        active_df = sr_df[sr_df["is_active"] == 1].copy()

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("활성 현장 수", f"{len(active_df)}개")
        c2.metric("총 계약금액", f"{(active_df['contract_amount']+active_df['change_amount']).sum():,} 원")
        c3.metric("총 입금액", f"{active_df['total_paid'].sum():,} 원")
        c4.metric("총 미수잔액", f"{active_df['unpaid_balance'].sum():,} 원")

        st.divider()
        st.caption("👉 아래 표에서 현장 행을 클릭하면 그 밑에 계산서·입금·변경계약 세부내역이 뜹니다.")

        disp = active_df.rename(columns={
            "site_name": "현장명", "company_name": "업체명", "contract_date": "계약일",
            "contract_amount": "총계약금액", "total_paid": "총입금액", "unpaid_balance": "미수잔액",
            "manager": "담당자",
        })
        disp["공정율(%)"] = (active_df["progress_rate"] * 100).round(0).astype(int)
        show_cols = ["현장명", "업체명", "계약일", "총계약금액", "총입금액", "미수잔액", "공정율(%)", "담당자"]
        show_df = disp[show_cols].reset_index(drop=True)

        sel_site = None
        try:
            event = st.dataframe(
                show_df, use_container_width=True, hide_index=True,
                on_select="rerun", selection_mode="single-row", key="recv_table",
                column_config={
                    "총계약금액": st.column_config.NumberColumn(),
                    "총입금액": st.column_config.NumberColumn(),
                    "미수잔액": st.column_config.NumberColumn(),
                },
            )
            if event and event.selection and event.selection.get("rows"):
                sel_site = show_df.iloc[event.selection["rows"][0]]["현장명"]
        except Exception:
            st.dataframe(show_df, use_container_width=True, hide_index=True)
            sel_site = st.selectbox("현장 선택 (상세 보기)", show_df["현장명"].tolist())

        if sel_site:
            st.divider()
            row = active_df[active_df["site_name"] == sel_site].iloc[0]
            st.markdown(f"#### 🔍 {sel_site} 상세")
            i1, i2, i3, i4 = st.columns(4)
            i1.metric("착공일", row["start_date"] or "-")
            i2.metric("준공일", row["completion_date"] or "-")
            i3.metric("변경계약금액", f"{row['change_amount']:,} 원")
            i4.metric("기성율", f"{round(row['invoice_progress_rate']*100)}%")

            with engine.connect() as conn:
                detail_df = pd.read_sql(
                    "SELECT detail_type as 구분, detail_date as 일자, amount as 금액, note as 비고 "
                    "FROM site_receivable_details WHERE site_receivable_id=:sid ORDER BY detail_date;",
                    conn, params={"sid": int(row["id"])}
                )
            if detail_df.empty:
                st.caption("세부내역이 없습니다.")
            else:
                render_html_table(detail_df, money_cols=["금액"], left_cols=[])

# ==========================================================================
# TAB: 기성청구현황  (일일수금관리=이력 엑셀 기준)
# ==========================================================================
with tab_progress:
    st.subheader("📊 기성청구현황")
    view_mode = st.radio("보기 기준", ["현장별", "담당자별"], horizontal=True)
    st.divider()

    with engine.connect() as conn:
        claims_df = pd.read_sql("SELECT * FROM claims;", conn)
        payments_df = pd.read_sql("SELECT * FROM payments;", conn)
        history_df = pd.read_sql("SELECT * FROM claim_delay_history;", conn)

    if claims_df.empty:
        st.info("데이터가 없습니다. '🔐 관리자' 탭에서 '일일수금관리' 엑셀을 업로드해주세요.")
    else:
        today = date.today()
        claim_rows = []
        for _, c in claims_df.iterrows():
            cid = c["id"]
            paid = payments_df[payments_df["claim_id"] == cid]["payment_amount"].sum() if not payments_df.empty else 0
            unpaid = (c["claim_amount"] or 0) - paid
            hist = history_df[(history_df["claim_id"] == cid) & (history_df["event_type"] == "자동지연")] if not history_df.empty else pd.DataFrame()
            delay_count = len(hist)
            if c["status"] == "완납":
                pay_rows = payments_df[payments_df["claim_id"] == cid] if not payments_df.empty else pd.DataFrame()
                ref_date = (safe_date(pay_rows.iloc[-1]["payment_date"]) if not pay_rows.empty else today) or today
            else:
                ref_date = today
            delay_days = calc_delay_days(c["original_due_date"], ref_date) if c["status"] != "확인필요" else 0
            claim_rows.append({
                "id": cid, "현장명": c["site_name"], "담당자": c["manager"], "채권종류": c["claim_type"],
                "청구금액": c["claim_amount"], "입금액": paid, "미수잔액": unpaid,
                "지연횟수": delay_count, "총지연일수": delay_days,
                "상태": display_status(c["status"], c["current_due_date"], today),
                "완납여부": c["status"] == "완납", "지연이력있음": delay_count >= 1,
            })
        claim_df_all = pd.DataFrame(claim_rows)

        if view_mode == "현장별":
            disp = claim_df_all[claim_df_all["미수잔액"] > 0].copy()
            f1, f2 = st.columns(2)
            site_filter = f1.selectbox("현장 필터", ["전체"] + sorted(disp["현장명"].unique().tolist()))
            type_filter = f2.selectbox("채권종류 필터", ["전체"] + CLAIM_TYPES)
            if site_filter != "전체":
                disp = disp[disp["현장명"] == site_filter]
            if type_filter != "전체":
                disp = disp[disp["채권종류"] == type_filter]
            show = disp[["현장명", "담당자", "채권종류", "청구금액", "입금액", "미수잔액", "지연횟수", "총지연일수", "상태"]]
            render_html_table(show, money_cols=["청구금액", "입금액", "미수잔액"])
            st.download_button("📥 CSV 다운로드", show.to_csv(index=False).encode("utf-8-sig"),
                                file_name=f"기성청구현황_현장별_{date.today()}.csv", mime="text/csv")

        else:  # 담당자별
            managers = sorted(claim_df_all["담당자"].dropna().unique().tolist())
            rows = []
            for m in managers:
                mc = claim_df_all[claim_df_all["담당자"] == m]
                total_cnt = len(mc)
                completed_cnt = int(mc["완납여부"].sum())
                unpaid_cnt = total_cnt - completed_cnt
                ever_delayed_cnt = int(mc["지연이력있음"].sum())
                total_claim = mc["청구금액"].sum()
                total_paid = mc["입금액"].sum()
                unpaid_amt = mc["미수잔액"].sum()
                rows.append({
                    "담당자": m, "총청구금액": total_claim, "총입금액": total_paid, "미수잔액": unpaid_amt,
                    "총청구건": total_cnt, "완납건": completed_cnt, "미수건": unpaid_cnt, "지연건": ever_delayed_cnt,
                    "수금률(%)": round(total_paid / total_claim * 100, 1) if total_claim else 0,
                    "지연률(%)": round(ever_delayed_cnt / total_cnt * 100, 1) if total_cnt else 0,
                })
            manager_df = pd.DataFrame(rows)
            render_html_table(manager_df, money_cols=["총청구금액", "총입금액", "미수잔액"])
            st.download_button("📥 CSV 다운로드", manager_df.to_csv(index=False).encode("utf-8-sig"),
                                file_name=f"기성청구현황_담당자별_{date.today()}.csv", mime="text/csv")

            st.divider()
            st.markdown("#### 🔍 담당자 상세 (채권종류별 3단 현황)")
            pick_m = st.selectbox("담당자 선택", managers)
            mc = claim_df_all[claim_df_all["담당자"] == pick_m]

            st.markdown("**1. 청구현황** (채권종류별 전체 건수·금액)")
            t1 = mc.groupby("채권종류").agg(건수=("id", "count"), 금액=("청구금액", "sum")).reset_index()
            render_html_table(t1, money_cols=["금액"], left_cols=[])

            st.markdown("**2. 미수현황** (채권종류별 미수 건수·금액)")
            mc_unpaid = mc[mc["미수잔액"] > 0]
            if mc_unpaid.empty:
                st.caption("미수 없음")
            else:
                t2 = mc_unpaid.groupby("채권종류").agg(건수=("id", "count"), 미수금액=("미수잔액", "sum")).reset_index()
                render_html_table(t2, money_cols=["미수금액"], left_cols=[])

            st.markdown("**3. 지연현황** (채권종류별 지연 건수·총지연일수)")
            mc_delayed = mc[mc["지연이력있음"]]
            if mc_delayed.empty:
                st.caption("지연 이력 없음")
            else:
                t3 = mc_delayed.groupby("채권종류").agg(건수=("id", "count"), 총지연일수=("총지연일수", "sum")).reset_index()
                render_html_table(t3, money_cols=[], left_cols=[])

# ==========================================================================
# TAB: 입금 캘린더 (이력 데이터만)
# ==========================================================================
with tab_calendar:
    st.subheader("📅 입금 캘린더")
    with engine.connect() as conn:
        claims_df = pd.read_sql("SELECT * FROM claims WHERE current_due_date IS NOT NULL AND current_due_date != '';", conn)

    if claims_df.empty:
        st.info("데이터가 없습니다.")
    else:
        today = date.today()
        if "cal_year" not in st.session_state:
            st.session_state.cal_year = today.year
            st.session_state.cal_month = today.month

        nav1, nav2, nav3 = st.columns([1, 3, 1])
        if nav1.button("◀ 이전달", use_container_width=True):
            st.session_state.cal_month -= 1
            if st.session_state.cal_month == 0:
                st.session_state.cal_month = 12
                st.session_state.cal_year -= 1
            st.rerun()
        nav2.markdown(f"<h3 style='text-align:center'>{st.session_state.cal_year}년 {st.session_state.cal_month}월</h3>", unsafe_allow_html=True)
        if nav3.button("다음달 ▶", use_container_width=True):
            st.session_state.cal_month += 1
            if st.session_state.cal_month == 13:
                st.session_state.cal_month = 1
                st.session_state.cal_year += 1
            st.rerun()

        st.caption("🔴 미입금(지연)  🟢 입금완료")
        yr, mo = st.session_state.cal_year, st.session_state.cal_month

        day_entries = {}
        for _, c in claims_df.iterrows():
            d = safe_date(c["current_due_date"])
            if d and d.year == yr and d.month == mo:
                st_disp = display_status(c["status"], c["current_due_date"], today)
                if c["status"] == "완납":
                    color = "#2ecc71"
                elif "지연" in st_disp:
                    color = "#e74c3c"
                else:
                    color = "#888888"
                site_short = c["site_name"][:16] + ("…" if len(c["site_name"]) > 16 else "")
                amt_disp = f"{int(c['claim_amount']) // 1000:,}"
                day_entries.setdefault(d.day, []).append({"site": site_short, "claim_type": c["claim_type"], "amt": amt_disp, "color": color})

        cal = pycal.Calendar(firstweekday=6)
        weeks = cal.monthdayscalendar(yr, mo)
        weekday_labels = ["일", "월", "화", "수", "목", "금", "토"]
        header_cols = st.columns(7)
        for i, lab in enumerate(weekday_labels):
            header_cols[i].markdown(f"<div style='text-align:center;font-weight:bold'>{lab}</div>", unsafe_allow_html=True)

        MAX_SHOWN = 6
        for week in weeks:
            row_cols = st.columns(7)
            for i, daynum in enumerate(week):
                if daynum == 0:
                    row_cols[i].write("")
                    continue
                with row_cols[i]:
                    if st.button(str(daynum), key=f"cal_{yr}_{mo}_{daynum}", use_container_width=True):
                        st.session_state["cal_selected_date"] = date(yr, mo, daynum).isoformat()
                    entries = day_entries.get(daynum, [])
                    if entries:
                        rows_html = ""
                        shown = entries[:MAX_SHOWN]
                        for idx, e in enumerate(shown):
                            border_style = "" if idx == len(shown) - 1 else "border-bottom:1px solid #eee;"
                            rows_html += (
                                f"<div style='padding:3px 2px;{border_style}'>"
                                f"<div style='font-size:14px;font-weight:700;color:#000;line-height:1.25;"
                                f"white-space:nowrap;overflow:hidden;text-overflow:ellipsis' title='{e['site']}'>"
                                f"<span style='color:{e['color']}'>●</span> {e['site']}</div>"
                                "<div style='font-size:12px;color:#333;display:flex;justify-content:space-between;'>"
                                f"<span>{e['claim_type']}</span><span>{e['amt']}</span></div></div>"
                            )
                        more_html = f"<div style='font-size:11px;color:#888;'>+{len(entries)-MAX_SHOWN}건 더</div>" if len(entries) > MAX_SHOWN else ""
                        st.markdown(f"<div style='border:1px solid #d9d9d9;border-radius:6px;padding:4px 6px;'>{rows_html}{more_html}</div>", unsafe_allow_html=True)

        sel_date = st.session_state.get("cal_selected_date")
        if sel_date:
            st.divider()
            st.markdown(f"#### 📌 {sel_date} 입금예정 목록")
            day_rows = []
            for _, c in claims_df.iterrows():
                d = safe_date(c["current_due_date"])
                if d and d.isoformat() == sel_date:
                    day_rows.append({
                        "현장명": c["site_name"], "채권종류": c["claim_type"], "청구금액": c["claim_amount"],
                        "상태": display_status(c["status"], c["current_due_date"], today),
                    })
            if day_rows:
                render_html_table(pd.DataFrame(day_rows), money_cols=["청구금액"])
            else:
                st.info("해당 날짜에 예정된 청구가 없습니다.")

# ==========================================================================
# TAB: 리스크 현장 (이력 데이터만)
# ==========================================================================
with tab_risk:
    st.subheader("🚨 리스크 현장")
    st.caption("완납되지 않은 청구 중 지연 3회 이상이거나 지연일수 30일 이상인 건이 있는 현장. (완납 청구는 제외)")
    with engine.connect() as conn:
        claims_df = pd.read_sql("SELECT * FROM claims WHERE status != '완납';", conn)
        history_df = pd.read_sql("SELECT * FROM claim_delay_history;", conn)

    if claims_df.empty:
        st.info("데이터가 없습니다.")
    else:
        today = date.today()
        risk_rows = []
        for _, c in claims_df.iterrows():
            cid = c["id"]
            hist = history_df[(history_df["claim_id"] == cid) & (history_df["event_type"] == "자동지연")] if not history_df.empty else pd.DataFrame()
            delay_count = len(hist)
            delay_days = calc_delay_days(c["original_due_date"], today) if c["status"] != "확인필요" else 0
            sev = claim_severity(delay_count, delay_days)
            if sev == 0:
                continue
            reasons = []
            if delay_count >= 3:
                reasons.append(f"지연 {delay_count}회")
            if delay_days >= 30:
                reasons.append(f"{delay_days}일 지연")
            risk_rows.append({
                "현장명": c["site_name"], "담당자": c["manager"], "채권종류": c["claim_type"],
                "청구금액": c["claim_amount"], "입금예정일": c["current_due_date"],
                "지연횟수": delay_count, "지연일수": delay_days, "등급": SEVERITY_LABEL[sev], "_sev": sev,
                "사유": ", ".join(reasons),
            })

        if not risk_rows:
            st.success("현재 리스크 현장이 없습니다. 👍")
        else:
            risk_df = pd.DataFrame(risk_rows)
            site_sev = risk_df.groupby("현장명")["_sev"].max().reset_index().rename(columns={"_sev": "현장등급"})
            site_sev["현장등급"] = site_sev["현장등급"].map(SEVERITY_LABEL)

            c1, c2, c3 = st.columns(3)
            c1.metric("리스크 현장 수", f"{site_sev['현장명'].nunique()}개")
            c2.metric("심각 등급 현장", f"{(risk_df.groupby('현장명')['_sev'].max() == 3).sum()}개")
            c3.metric("리스크 청구 건수", f"{len(risk_df)}건")

            st.divider()
            st.markdown("#### 현장별 등급 요약")
            render_html_table(site_sev.sort_values("현장등급", ascending=False), left_cols=[])

            st.divider()
            st.markdown("#### 리스크 청구 상세")
            display_risk = risk_df.drop(columns=["_sev"]).sort_values("지연일수", ascending=False)
            render_html_table(display_risk, money_cols=["청구금액"])
            st.download_button("📥 CSV 다운로드", display_risk.to_csv(index=False).encode("utf-8-sig"),
                                file_name=f"리스크현장_{date.today()}.csv", mime="text/csv")

# ==========================================================================
# TAB: 계약현황 (미수내역 전체 - 활성/완불 구분 없이 계약년월 있는 모든 행)
# ==========================================================================
with tab_contract:
    st.subheader("📈 계약현황 (연도별)")
    with engine.connect() as conn:
        sr_df = pd.read_sql("SELECT * FROM site_receivables;", conn)

    if sr_df.empty:
        st.info("데이터가 없습니다.")
    else:
        sr_df["_ym"] = pd.to_datetime(sr_df["contract_yearmonth"], errors="coerce")
        sr_df = sr_df[sr_df["_ym"].notna()]
        sr_df["연도"] = sr_df["_ym"].dt.year
        sr_df["월"] = sr_df["_ym"].dt.month
        sr_df["_total"] = sr_df["contract_amount"] + sr_df["change_amount"]

        yearly = sr_df.groupby("연도").agg(총계약금액=("_total", "sum"), 현장수=("id", "count")).reset_index()
        for branch in sorted(sr_df["branch"].dropna().unique().tolist()):
            b = sr_df[sr_df["branch"] == branch].groupby("연도")["_total"].sum()
            yearly[f"{branch}계약금액"] = yearly["연도"].map(b).fillna(0).astype(int)

        money_cols = [c for c in yearly.columns if "계약금액" in c]
        render_html_table(yearly.sort_values("연도"), money_cols=money_cols, left_cols=[])

        st.divider()
        sel_year = st.selectbox("월별로 보기", sorted(sr_df["연도"].unique().tolist(), reverse=True))
        monthly = sr_df[sr_df["연도"] == sel_year].groupby("월").agg(계약금액=("_total", "sum"), 현장수=("id", "count")).reset_index()
        render_html_table(monthly, money_cols=["계약금액"], left_cols=[])

        st.download_button("📥 연도별 CSV 다운로드", yearly.to_csv(index=False).encode("utf-8-sig"),
                            file_name=f"계약현황_연도별_{date.today()}.csv", mime="text/csv")

# ==========================================================================
# TAB: 관리자 — 엑셀 업로드 2개만
# ==========================================================================
with tab_admin:
    st.subheader("🔐 관리자")
    if not is_admin:
        st.warning("🔒 관리자 전용 메뉴입니다. 왼쪽 사이드바에서 비밀번호를 입력하세요.")
    else:
        st.markdown("업로드하면 **해당 데이터 전체가 지금 올리는 파일 내용으로 갈음**됩니다 (기존 것 삭제 후 새로 채움).")

        # ---------------- 일일수금관리 업로드 ----------------
        st.markdown("### 📂 일일수금관리 업로드 (기성청구현황·캘린더·리스크현장)")
        daily_file = st.file_uploader("일일수금관리 엑셀(.xlsx) 업로드", type=["xlsx", "csv"], key="daily_upload")

        if daily_file is not None and st.button("🚀 일일수금관리 데이터로 전체 갱신", use_container_width=True):
            try:
                xls = pd.ExcelFile(daily_file, engine="openpyxl")
                frames = []
                for sheet in ["이력", "장기미수"]:
                    if sheet in xls.sheet_names:
                        raw = pd.read_excel(xls, sheet_name=sheet, header=1)
                        frames.append(raw)
                if not frames:
                    st.error("'이력' 시트를 찾지 못했습니다.")
                    st.stop()
                raw_df = pd.concat(frames, ignore_index=True)
                raw_df.columns = [str(c).strip() for c in raw_df.columns]

                col_map = {
                    "채권명": "bond_name", "현장명": "site_name", "업체명": "company_name",
                    "담당자": "manager", "기성구분": "claim_type", "미수금액": "claim_amount",
                    "최초예정일": "original_due_date", "입금예정일": "current_due_date",
                    "지연여부": "status_raw", "입금여부": "paid_flag", "입금액": "paid_amount",
                }
                for c in list(raw_df.columns):
                    if "입금상태" in c or "입금일자" in c:
                        col_map[c] = "payment_date_raw"
                raw_df = raw_df.rename(columns={k: v for k, v in col_map.items() if k in raw_df.columns})
                raw_df = raw_df.dropna(subset=["bond_name"]) if "bond_name" in raw_df.columns else raw_df.dropna(subset=["site_name"])
                raw_df["_due_sort"] = pd.to_datetime(raw_df.get("current_due_date"), errors="coerce")

                with engine.connect() as conn:
                    for t in ["claim_delay_history", "payments", "claims"]:
                        conn.execute(text(f"DELETE FROM {t};"))
                    conn.commit()

                    has_bond = "bond_name" in raw_df.columns
                    groups = raw_df.groupby("bond_name") if has_bond else [(i, raw_df.loc[[i]]) for i in raw_df.index]
                    n_claims = 0
                    for _, grp in groups:
                        grp = grp.sort_values("_due_sort", na_position="last")
                        first, last = grp.iloc[0], grp.iloc[-1]
                        site_name = str(first.get("site_name", "")).strip()
                        manager = str(first.get("manager", "") or "")
                        claim_type_v = str(last.get("claim_type", "") or "기성금")
                        claim_amount = parse_amount(last.get("claim_amount", 0))
                        orig_due = safe_date(first.get("original_due_date")) or safe_date(first.get("current_due_date"))
                        orig_due_str = orig_due.isoformat() if orig_due else None

                        payment_events = []
                        for _, r in grp.iterrows():
                            pf = str(r.get("paid_flag", "") or "").strip().upper()
                            pa = parse_amount(r.get("paid_amount", 0))
                            if pf == "Y" and pa > 0:
                                pdate = safe_date(r.get("payment_date_raw")) or safe_date(r.get("current_due_date"))
                                payment_events.append((pdate, pa))
                        total_paid = sum(a for _, a in payment_events)

                        cur_due = safe_date(last.get("current_due_date"))
                        cur_due_str = cur_due.isoformat() if cur_due else None
                        status_raw = str(last.get("status_raw", "") or "")

                        if total_paid >= claim_amount and claim_amount > 0:
                            status = "완납"
                        elif total_paid > 0:
                            status = "일부입금"
                        elif cur_due_str is None:
                            status = "확인필요"
                        else:
                            status = "입금대기"

                        res = conn.execute(text("""
                            INSERT INTO claims (site_name, manager, claim_type, claim_date, original_due_date, current_due_date, claim_amount, status)
                            VALUES (:sn, :mg, :ctype, :cdate, :odue, :cdue, :amt, :status)
                        """), {"sn": site_name, "mg": manager, "ctype": claim_type_v, "cdate": orig_due_str,
                               "odue": orig_due_str, "cdue": cur_due_str, "amt": claim_amount, "status": status})
                        claim_id = res.lastrowid
                        n_claims += 1

                        prev_due = orig_due
                        for _, r in grp.iterrows():
                            this_due = safe_date(r.get("current_due_date"))
                            if this_due and prev_due and this_due != prev_due:
                                conn.execute(text("""
                                    INSERT INTO claim_delay_history (claim_id, event_type, old_due_date, new_due_date, delay_days, reason)
                                    VALUES (:cid, '자동지연', :old, :new, :ddays, '엑셀 이력 가져오기')
                                """), {"cid": claim_id, "old": prev_due.isoformat(), "new": this_due.isoformat(), "ddays": (this_due - prev_due).days})
                            if this_due:
                                prev_due = this_due

                        for pdate, pamt in payment_events:
                            pdate_final = pdate or cur_due or date.today()
                            conn.execute(text("INSERT INTO payments (claim_id, payment_date, payment_amount) VALUES (:cid,:pd,:pa)"),
                                         {"cid": claim_id, "pd": pdate_final.isoformat(), "pa": pamt})
                        if status == "완납" and payment_events:
                            last_pay_date = max(p[0] for p in payment_events if p[0]) if any(p[0] for p in payment_events) else cur_due
                            delay_days = 0
                            event_type = "입금완료(정상)"
                            if orig_due and last_pay_date and last_pay_date > orig_due:
                                delay_days = (last_pay_date - orig_due).days
                                event_type = "입금완료(지연)"
                            conn.execute(text("""
                                INSERT INTO claim_delay_history (claim_id, event_type, payment_date, delay_days, reason)
                                VALUES (:cid, :etype, :pd, :dd, '엑셀 이력 가져오기')
                            """), {"cid": claim_id, "etype": event_type, "pd": last_pay_date.isoformat() if last_pay_date else None, "dd": delay_days})
                    conn.commit()
                st.success(f"✅ 완료! 청구 {n_claims}건 반영 (기존 데이터는 전부 새 데이터로 갈음됨)")
                st.rerun()
            except Exception as e:
                st.error(f"❌ 처리 오류: {e}")

        st.divider()

        # ---------------- 현장별 미수관리 업로드 ----------------
        st.markdown("### 📂 현장별 미수관리 업로드 (현장별 미수현황·완불현장·계약현황)")
        recv_file = st.file_uploader("현장별 미수관리 엑셀(.xlsx) 업로드", type=["xlsx"], key="recv_upload")

        if recv_file is not None and st.button("🚀 현장별 미수관리 데이터로 전체 갱신", use_container_width=True):
            try:
                import openpyxl
                wb = openpyxl.load_workbook(recv_file, data_only=True)
                if "미수내역" not in wb.sheetnames:
                    st.error("'미수내역' 시트를 찾지 못했습니다.")
                    st.stop()
                ws = wb["미수내역"]

                def hdr_row_idx():
                    for i, row in enumerate(ws.iter_rows(min_row=1, max_row=10, values_only=True)):
                        if any(v == "공사명" for v in row):
                            return i + 1
                    return 4

                start_row = hdr_row_idx() + 1
                rows = list(ws.iter_rows(min_row=start_row, values_only=True))

                def to_date_s(v):
                    d = safe_date(v)
                    return d.isoformat() if d else None

                def is_blank(r):
                    return all(v is None for v in r)

                with engine.connect() as conn:
                    for t in ["site_receivable_details", "site_receivables"]:
                        conn.execute(text(f"DELETE FROM {t};"))
                    conn.commit()

                    n_sites = 0
                    i = 0
                    while i < len(rows):
                        r = rows[i]
                        if r[7]:  # 공사명
                            n_sites += 1
                            c_flag = r[2]
                            is_active = 1
                            status_label = "활성"
                            if isinstance(c_flag, str) and c_flag.strip() != "":
                                is_active = 0
                                status_label = c_flag.strip()

                            contract_date = to_date_s(r[11])
                            ym = to_date_s(r[10])
                            if not contract_date:
                                contract_date = ym

                            res = conn.execute(text("""
                                INSERT INTO site_receivables
                                (site_name, company_name, manager, branch, contract_code, contract_date, start_date,
                                 completion_date, contract_yearmonth, contract_amount, change_amount, total_paid,
                                 unpaid_balance, progress_rate, invoice_progress_rate, invoice_issue_rate, is_active, status_label)
                                VALUES (:sn,:cn,:mg,:br,:cc,:cd,:sd,:ed,:ym,:ca,:cha,:tp,:ub,:pr,:ipr,:iir,:ia,:sl)
                            """), {
                                "sn": r[7], "cn": r[8] or "", "mg": r[24] or "", "br": r[4] or "",
                                "cc": r[3] or "", "cd": contract_date, "sd": to_date_s(r[12]), "ed": to_date_s(r[13]),
                                "ym": ym,
                                "ca": parse_amount(r[16]) * 1000, "cha": parse_amount(r[15]) * 1000,
                                "tp": parse_amount(r[17]) * 1000, "ub": parse_amount(r[18]) * 1000,
                                "pr": float(r[19]) if isinstance(r[19], (int, float)) else 0,
                                "ipr": float(r[20]) if isinstance(r[20], (int, float)) else 0,
                                "iir": float(r[21]) if isinstance(r[21], (int, float)) else 0,
                                "ia": is_active, "sl": status_label,
                            })
                            site_id = res.lastrowid

                            j = i + 1
                            while j < len(rows) and not is_blank(rows[j]) and rows[j][7] is None:
                                d = rows[j]
                                # 변경계약: col11=날짜, col12=문서종류, col15=금액
                                if isinstance(d[11], datetime) and isinstance(d[15], (int, float)) and d[15]:
                                    conn.execute(text("""
                                        INSERT INTO site_receivable_details (site_receivable_id, detail_type, detail_date, amount, note)
                                        VALUES (:sid,'변경계약',:dt,:amt,:note)
                                    """), {"sid": site_id, "dt": to_date_s(d[11]), "amt": int(d[15]) * 1000, "note": d[12] or ""})
                                # 계산서: col28=발행일, col29=발행액
                                if isinstance(d[28], datetime) and isinstance(d[29], (int, float)) and d[29]:
                                    conn.execute(text("""
                                        INSERT INTO site_receivable_details (site_receivable_id, detail_type, detail_date, amount, note)
                                        VALUES (:sid,'계산서',:dt,:amt,'')
                                    """), {"sid": site_id, "dt": to_date_s(d[28]), "amt": int(d[29]) * 1000})
                                # 입금: col31=입금일, col32=입금액
                                if isinstance(d[31], datetime) and isinstance(d[32], (int, float)) and d[32]:
                                    conn.execute(text("""
                                        INSERT INTO site_receivable_details (site_receivable_id, detail_type, detail_date, amount, note)
                                        VALUES (:sid,'입금',:dt,:amt,'')
                                    """), {"sid": site_id, "dt": to_date_s(d[31]), "amt": int(d[32]) * 1000})
                                j += 1
                            i = j
                        else:
                            i += 1
                    conn.commit()
                st.success(f"✅ 완료! 현장 {n_sites}개 반영 (기존 데이터는 전부 새 데이터로 갈음됨)")
                st.rerun()
            except Exception as e:
                st.error(f"❌ 처리 오류: {e}")