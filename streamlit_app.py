import streamlit as st
from sqlalchemy import create_engine, text
import pandas as pd
from datetime import datetime, date, timedelta
import io

st.set_page_config(page_title="건설 현장 정산 & 지연 관리 시스템", layout="wide")
st.title("🏗️ 건설 현장 정산 & 지연 관리 시스템")

# ⚠️ 관리자 비밀번호
ADMIN_PASSWORD = "chdan1576**"

CLAIM_TYPES = ["선급금", "기성금", "중도금", "잔금", "추가금", "정산금", "AS", "시공부자재"]
STATUS_OPTIONS = ["입금대기", "일부입금", "완납", "확인필요"]

engine = create_engine("sqlite:///construction_v5.db")

# --------------------------------------------------------------------------
# DB 초기화
# --------------------------------------------------------------------------
with engine.connect() as conn:
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS sites (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            site_name TEXT NOT NULL UNIQUE,
            company_name TEXT,
            manager TEXT,
            contract_amount INTEGER DEFAULT 0,
            additional_amount INTEGER DEFAULT 0,
            progress_rate REAL DEFAULT 0,
            contract_date TEXT,
            start_date TEXT,
            completion_date TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """))
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS claims (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            site_id INTEGER,
            claim_type TEXT DEFAULT '기성금',
            claim_date TEXT,
            original_due_date TEXT,
            current_due_date TEXT,
            claim_amount INTEGER DEFAULT 0,
            status TEXT DEFAULT '입금대기',
            delay_reason TEXT,
            last_flagged_due_date TEXT,
            FOREIGN KEY(site_id) REFERENCES sites(id) ON DELETE CASCADE
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
            reason TEXT,
            FOREIGN KEY(claim_id) REFERENCES claims(id) ON DELETE CASCADE
        );
    """))
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            site_id INTEGER,
            claim_id INTEGER,
            payment_date TEXT,
            payment_amount INTEGER DEFAULT 0,
            FOREIGN KEY(site_id) REFERENCES sites(id) ON DELETE CASCADE
        );
    """))
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS tax_invoices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            site_id INTEGER,
            claim_type TEXT,
            issue_date TEXT,
            invoice_amount INTEGER DEFAULT 0,
            FOREIGN KEY(site_id) REFERENCES sites(id) ON DELETE CASCADE
        );
    """))
    conn.commit()


# --------------------------------------------------------------------------
# 공용 함수
# --------------------------------------------------------------------------
def parse_amount(v):
    if pd.isna(v):
        return 0
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v).strip().replace(",", "").replace("원", "").replace(" ", "")
    if s in ("", "-"):
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
    return f"{int(x):,}"


def fmt_money_cols(df, cols):
    df = df.copy()
    for c in cols:
        if c in df.columns:
            df[c] = df[c].apply(fmt_money)
    return df


def normalize_company(c):
    if not c:
        return c
    return str(c).replace("㈜", "").replace("(주)", "").strip()


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


def load_all():
    with engine.connect() as conn:
        sites_df = pd.read_sql("SELECT * FROM sites;", conn)
        claims_df = pd.read_sql("SELECT * FROM claims;", conn)
        payments_df = pd.read_sql("SELECT * FROM payments;", conn)
        invoices_df = pd.read_sql("SELECT * FROM tax_invoices;", conn)
        history_df = pd.read_sql("SELECT * FROM claim_delay_history;", conn)
    return sites_df, claims_df, payments_df, invoices_df, history_df


def run_daily_delay_check():
    """예정일이 지났는데 아직 완납이 안 된 청구를 자동으로 '지연' 이력에 한 번만 쌓는다."""
    today_str = date.today().isoformat()
    with engine.connect() as conn:
        open_claims = conn.execute(text("""
            SELECT id, current_due_date, original_due_date, last_flagged_due_date, status
            FROM claims
            WHERE status IN ('입금대기', '일부입금')
        """)).fetchall()

        for cid, cur_due, orig_due, last_flag, status in open_claims:
            due_d = safe_date(cur_due)
            if due_d is None:
                continue
            if due_d >= date.today():
                continue
            if last_flag == cur_due:
                continue  # 이 예정일에 대해선 이미 지연 카운팅 했음

            delay_days = calc_delay_days(orig_due, date.today())
            conn.execute(text("""
                INSERT INTO claim_delay_history (claim_id, event_type, old_due_date, new_due_date, delay_days, reason)
                VALUES (:cid, '자동지연', :due, :due, :ddays, '예정일 경과 자동 감지')
            """), {"cid": cid, "due": cur_due, "ddays": delay_days})
            conn.execute(text("UPDATE claims SET last_flagged_due_date=:due WHERE id=:cid"),
                         {"due": cur_due, "cid": cid})
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

tab_status, tab_progress, tab_calendar, tab_risk, tab_admin = st.tabs([
    "🏢 공사현황",
    "📊 기성현황",
    "📅 입금 캘린더",
    "🚨 리스크 현장",
    "🔐 관리자"
])

# ==========================================================================
# TAB: 공사현황
# ==========================================================================
with tab_status:
    st.subheader("🏢 공사현황")
    sites_df, claims_df, payments_df, invoices_df, history_df = load_all()

    if sites_df.empty:
        st.info("등록된 현장이 없습니다. '🔐 관리자' 탭에서 엑셀을 업로드해 현장을 먼저 만들어주세요.")
    else:
        as_claim_ids = set(claims_df[claims_df["claim_type"] == "AS"]["id"].tolist()) if not claims_df.empty else set()

        rows = []
        for _, s in sites_df.iterrows():
            sid = s["id"]
            total_amount = (s["contract_amount"] or 0) + (s["additional_amount"] or 0)

            claim_sum = 0
            if not claims_df.empty:
                claim_sum = claims_df[(claims_df["site_id"] == sid) & (claims_df["claim_type"] != "AS")]["claim_amount"].sum()

            inv_sum = 0
            if not invoices_df.empty:
                inv_sum = invoices_df[(invoices_df["site_id"] == sid) & (invoices_df["claim_type"] != "AS")]["invoice_amount"].sum()

            pay_sum = 0
            if not payments_df.empty:
                site_pay = payments_df[payments_df["site_id"] == sid]
                if as_claim_ids:
                    site_pay = site_pay[~site_pay["claim_id"].isin(as_claim_ids)]
                pay_sum = site_pay["payment_amount"].sum()

            # 미수잔액/수금율은 계약금액이 아니라 "실제 청구된 금액" 기준 (계약금액 미입력이어도 음수로 안 나옴)
            unpaid = claim_sum - pay_sum
            collect_rate = (pay_sum / claim_sum * 100) if claim_sum > 0 else 0

            rows.append({
                "id": sid, "현장명": s["site_name"],
                "계약일": s["contract_date"] or "", "착공일": s["start_date"] or "", "준공일": s["completion_date"] or "",
                "계약금액": s["contract_amount"] or 0, "추가계약": s["additional_amount"] or 0,
                "총공사금액": total_amount, "총청구금액": claim_sum,
                "계산서발행액": inv_sum, "총입금액": pay_sum, "미수잔액": unpaid,
                "공정율(%)": s["progress_rate"] or 0, "수금율(%)": round(collect_rate, 1),
            })

        status_df = pd.DataFrame(rows)
        status_df["_sort"] = pd.to_datetime(status_df["계약일"], errors="coerce")
        status_df = status_df.sort_values("_sort", na_position="last").drop(columns=["_sort"]).reset_index(drop=True)
        for dcol in ["계약일", "착공일", "준공일"]:
            status_df[dcol] = pd.to_datetime(status_df[dcol], errors="coerce")

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("현장 수", f"{len(status_df)}개")
        c2.metric("총 공사금액", f"{status_df['총공사금액'].sum():,} 원")
        c3.metric("총 입금액", f"{status_df['총입금액'].sum():,} 원")
        c4.metric("총 미수잔액", f"{status_df['미수잔액'].sum():,} 원")

        st.divider()
        st.caption("💡 계약일/착공일/준공일/계약금액/추가계약/공정율(%) 만 직접 수정 가능합니다. 나머지는 관리자 탭 데이터로 자동 계산됩니다. (계약일 기준 정렬)")

        display_df = fmt_money_cols(status_df, ["계약금액", "추가계약", "총공사금액", "총청구금액", "계산서발행액", "총입금액", "미수잔액"])

        edited = st.data_editor(
            display_df.drop(columns=["id"]),
            use_container_width=True,
            disabled=["현장명", "총공사금액", "총청구금액", "계산서발행액", "총입금액", "미수잔액", "수금율(%)"],
            column_config={
                "계약일": st.column_config.DateColumn(format="YYYY-MM-DD"),
                "착공일": st.column_config.DateColumn(format="YYYY-MM-DD"),
                "준공일": st.column_config.DateColumn(format="YYYY-MM-DD"),
                "총청구금액": st.column_config.TextColumn(help="지금까지 실제로 청구(기성)한 금액 합계"),
                "미수잔액": st.column_config.TextColumn(help="총청구금액 - 총입금액 (계약금액 기준이 아닙니다)"),
            },
            key="status_editor"
        )

        if st.button("💾 저장", use_container_width=True):
            with engine.connect() as conn:
                for i, row in edited.iterrows():
                    site_id = int(status_df.iloc[i]["id"])
                    cd = safe_date(row["계약일"]); sd = safe_date(row["착공일"]); ed = safe_date(row["준공일"])
                    conn.execute(text("""
                        UPDATE sites SET contract_amount=:ca, additional_amount=:aa, progress_rate=:pr,
                               contract_date=:cd, start_date=:sd, completion_date=:ed
                        WHERE id=:sid
                    """), {"ca": parse_amount(row["계약금액"]), "aa": parse_amount(row["추가계약"]),
                           "pr": float(row["공정율(%)"]), "sid": site_id,
                           "cd": cd.isoformat() if cd else None, "sd": sd.isoformat() if sd else None,
                           "ed": ed.isoformat() if ed else None})
                conn.commit()
            st.success("저장되었습니다.")
            st.rerun()

        # AS 합산 현황 (별도 집계, 편집 불가)
        if as_claim_ids:
            as_claims = claims_df[claims_df["claim_type"] == "AS"]
            as_claim_sum = as_claims["claim_amount"].sum()
            as_inv_sum = invoices_df[invoices_df["claim_type"] == "AS"]["invoice_amount"].sum() if not invoices_df.empty else 0
            as_pay_sum = payments_df[payments_df["claim_id"].isin(as_claim_ids)]["payment_amount"].sum() if not payments_df.empty else 0
            as_unpaid = as_claim_sum - as_pay_sum
            st.divider()
            st.markdown("#### 🧾 AS 합산 현황")
            a1, a2, a3, a4 = st.columns(4)
            a1.metric("AS 총 청구금액", f"{as_claim_sum:,} 원")
            a2.metric("AS 계산서발행액", f"{as_inv_sum:,} 원")
            a3.metric("AS 총입금액", f"{as_pay_sum:,} 원")
            a4.metric("AS 미수잔액", f"{as_unpaid:,} 원")

        csv_data = status_df.drop(columns=["id"]).to_csv(index=False).encode("utf-8-sig")
        st.download_button("📥 공사현황 CSV 다운로드", data=csv_data, file_name=f"공사현황_{date.today()}.csv", mime="text/csv")

# ==========================================================================
# TAB: 기성현황 (현장별 / 담당자별 / 거래업체별)
# ==========================================================================
with tab_progress:
    st.subheader("📊 기성현황")
    sites_df, claims_df, payments_df, invoices_df, history_df = load_all()
    view_mode = st.radio("보기 기준", ["현장별", "담당자별", "거래업체별"], horizontal=True)
    st.divider()

    if claims_df.empty:
        st.info("등록된 청구 내역이 없습니다.")
    else:
        today = date.today()
        merged = claims_df.merge(sites_df[["id", "site_name", "manager", "company_name"]],
                                  left_on="site_id", right_on="id", suffixes=("", "_site"))

        # ---- claim 단위 상세 계산 (공통으로 재사용) ----
        claim_rows = []
        for _, c in merged.iterrows():
            cid = c["id"]
            paid = payments_df[payments_df["claim_id"] == cid]["payment_amount"].sum() if not payments_df.empty else 0
            unpaid = (c["claim_amount"] or 0) - paid
            collect_rate = (paid / c["claim_amount"] * 100) if c["claim_amount"] else 0

            hist = history_df[(history_df["claim_id"] == cid) & (history_df["event_type"] == "자동지연")] if not history_df.empty else pd.DataFrame()
            delay_count = len(hist)

            if c["status"] == "완납":
                pay_rows = payments_df[payments_df["claim_id"] == cid] if not payments_df.empty else pd.DataFrame()
                ref_date = safe_date(pay_rows.iloc[-1]["payment_date"]) if not pay_rows.empty else today
                ref_date = ref_date or today
            else:
                ref_date = today
            delay_days = calc_delay_days(c["original_due_date"], ref_date) if c["status"] != "확인필요" else 0

            claim_rows.append({
                "현장명": c["site_name"], "담당자": c["manager"], "거래업체명": c["company_name"],
                "채권종류": c["claim_type"], "입금예정일": c["current_due_date"],
                "총청구금액": c["claim_amount"], "총입금액": paid, "미수잔액": unpaid,
                "수금율(%)": round(collect_rate, 1), "지연횟수": delay_count, "총지연일수": delay_days,
                "상태": display_status(c["status"], c["current_due_date"], today),
            })
        claim_df_all = pd.DataFrame(claim_rows)

        if view_mode == "현장별":
            f1, f2 = st.columns(2)
            site_filter = f1.selectbox("현장 필터", ["전체"] + sorted(claim_df_all["현장명"].unique().tolist()))
            type_filter = f2.selectbox("채권종류 필터", ["전체"] + CLAIM_TYPES)
            disp = claim_df_all.drop(columns=["거래업체명"]).copy()
            if site_filter != "전체":
                disp = disp[disp["현장명"] == site_filter]
            if type_filter != "전체":
                disp = disp[disp["채권종류"] == type_filter]
            csv_data_bysite = disp.to_csv(index=False).encode("utf-8-sig")
            disp = fmt_money_cols(disp, ["총청구금액", "총입금액", "미수잔액"])
            st.dataframe(disp, use_container_width=True)
            st.download_button("📥 CSV 다운로드", csv_data_bysite,
                                file_name=f"현장별_기성현황_{date.today()}.csv", mime="text/csv")

        elif view_mode == "담당자별":
            managers = sorted(claim_df_all["담당자"].dropna().unique().tolist())
            rows = []
            for m in managers:
                mc = claim_df_all[claim_df_all["담당자"] == m]
                total_claim = mc["총청구금액"].sum()
                total_paid = mc["총입금액"].sum()
                unpaid = total_claim - total_paid
                completed = len(mc[mc["상태"] == "완납"])
                total_cnt = len(mc)
                row = {
                    "담당자": m, "총 채권수": total_cnt, "총 채권금액": total_claim, "총 수금액": total_paid,
                    "미수잔액": unpaid,
                    "완납률(%)": round(completed / total_cnt * 100, 1) if total_cnt else 0,
                    "수금률(%)": round(total_paid / total_claim * 100, 1) if total_claim else 0,
                    "미수율(%)": round(unpaid / total_claim * 100, 1) if total_claim else 0,
                }
                for ct in CLAIM_TYPES:
                    row[ct] = len(mc[mc["채권종류"] == ct])
                rows.append(row)
            manager_df = pd.DataFrame(rows)
            csv_data_mgr = manager_df.to_csv(index=False).encode("utf-8-sig")
            st.dataframe(fmt_money_cols(manager_df, ["총 채권금액", "총 수금액", "미수잔액"]), use_container_width=True)
            st.download_button("📥 CSV 다운로드", csv_data_mgr,
                                file_name=f"담당자별_수금현황_{date.today()}.csv", mime="text/csv")

        else:  # 거래업체별
            claim_df_all["_norm_company"] = claim_df_all["거래업체명"].apply(normalize_company)
            companies = sorted([c for c in claim_df_all["_norm_company"].dropna().unique().tolist() if c])
            rows = []
            for co in companies:
                cc = claim_df_all[claim_df_all["_norm_company"] == co]
                total_claim = cc["총청구금액"].sum()
                total_paid = cc["총입금액"].sum()
                unpaid = total_claim - total_paid
                inv_sum = 0
                site_names_for_co = sites_df[sites_df["company_name"].apply(normalize_company) == co]["id"].tolist()
                if not invoices_df.empty and site_names_for_co:
                    inv_sum = invoices_df[invoices_df["site_id"].isin(site_names_for_co)]["invoice_amount"].sum()
                rows.append({
                    "거래업체명": co, "총계약금": total_claim, "미수금": unpaid,
                    "입금액": total_paid, "계산서발행액": inv_sum
                })
            company_df = pd.DataFrame(rows)
            csv_data_co = company_df.to_csv(index=False).encode("utf-8-sig")
            st.dataframe(fmt_money_cols(company_df, ["총계약금", "미수금", "입금액", "계산서발행액"]), use_container_width=True)
            st.download_button("📥 CSV 다운로드", csv_data_co,
                                file_name=f"거래업체별_현황_{date.today()}.csv", mime="text/csv")

# ==========================================================================
# TAB: 입금 캘린더 (외부 패키지 없이 순수 스트림릿으로 직접 그림)
# ==========================================================================
with tab_calendar:
    st.subheader("📅 입금 캘린더")
    sites_df, claims_df, payments_df, invoices_df, history_df = load_all()

    if claims_df.empty:
        st.info("등록된 청구가 없습니다.")
    else:
        import calendar as pycal

        today = date.today()
        merged = claims_df.merge(sites_df[["id", "site_name"]], left_on="site_id", right_on="id", suffixes=("", "_site"))
        merged = merged[merged["current_due_date"].notna() & (merged["current_due_date"] != "")]

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

        st.caption("🔴 지연  🟢 입금완료  (날짜를 누르면 그날 전체 목록이 아래 뜹니다)")

        yr, mo = st.session_state.cal_year, st.session_state.cal_month

        day_entries = {}
        for _, c in merged.iterrows():
            d = safe_date(c["current_due_date"])
            if d and d.year == yr and d.month == mo:
                st_disp = display_status(c["status"], c["current_due_date"], today)
                if c["status"] == "완납":
                    color = "#2ecc71"
                elif "지연" in st_disp:
                    color = "#e74c3c"
                else:
                    color = "#888888"
                site_short = c["site_name"][:8] + ("…" if len(c["site_name"]) > 8 else "")
                label = f"{site_short} {c['claim_type']}"
                day_entries.setdefault(d.day, []).append({"label": label, "color": color})

        cal = pycal.Calendar(firstweekday=6)  # 일요일 시작
        weeks = cal.monthdayscalendar(yr, mo)
        weekday_labels = ["일", "월", "화", "수", "목", "금", "토"]

        header_cols = st.columns(7)
        for i, lab in enumerate(weekday_labels):
            header_cols[i].markdown(f"<div style='text-align:center;font-weight:bold'>{lab}</div>", unsafe_allow_html=True)

        MAX_SHOWN = 3
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
                        html = ""
                        for e in entries[:MAX_SHOWN]:
                            html += (f"<div style='font-size:11px;color:{e['color']};white-space:nowrap;"
                                     f"overflow:hidden;text-overflow:ellipsis' title='{e['label']}'>● {e['label']}</div>")
                        if len(entries) > MAX_SHOWN:
                            html += f"<div style='font-size:10px;color:#888'>+{len(entries) - MAX_SHOWN}건 더</div>"
                        st.markdown(html, unsafe_allow_html=True)

        sel_date = st.session_state.get("cal_selected_date")
        if sel_date:
            st.divider()
            st.markdown(f"#### 📌 {sel_date} 입금예정 목록")
            day_rows = []
            for _, c in merged.iterrows():
                d = safe_date(c["current_due_date"])
                if d and d.isoformat() == sel_date:
                    day_rows.append({
                        "현장명": c["site_name"], "채권종류": c["claim_type"], "청구금액": c["claim_amount"],
                        "상태": display_status(c["status"], c["current_due_date"], today),
                        "지연사유": c["delay_reason"] or "-",
                    })
            if day_rows:
                st.dataframe(fmt_money_cols(pd.DataFrame(day_rows), ["청구금액"]), use_container_width=True)
            else:
                st.info("해당 날짜에 예정된 청구가 없습니다.")

# ==========================================================================
# TAB: 리스크 현장
# ==========================================================================
with tab_risk:
    st.subheader("🚨 리스크 현장")
    st.caption("완납되지 않은 청구 중 지연 3회 이상이거나 지연일수 30일 이상인 건이 있는 현장을 모았습니다. (완납된 청구는 제외)")
    sites_df, claims_df, payments_df, invoices_df, history_df = load_all()

    if claims_df.empty:
        st.info("등록된 청구가 없습니다.")
    else:
        today = date.today()
        merged = claims_df.merge(sites_df[["id", "site_name", "manager"]], left_on="site_id", right_on="id", suffixes=("", "_site"))
        open_claims = merged[merged["status"] != "완납"]

        risk_rows = []
        for _, c in open_claims.iterrows():
            cid = c["id"]
            hist = history_df[(history_df["claim_id"] == cid) & (history_df["event_type"] == "자동지연")] if not history_df.empty else pd.DataFrame()
            delay_count = len(hist)
            delay_days = calc_delay_days(c["original_due_date"], today) if c["status"] != "확인필요" else 0
            sev = claim_severity(delay_count, delay_days)
            if sev == 0:
                continue
            risk_rows.append({
                "현장명": c["site_name"], "담당자": c["manager"], "채권종류": c["claim_type"],
                "청구금액": c["claim_amount"], "입금예정일": c["current_due_date"],
                "지연횟수": delay_count, "지연일수": delay_days, "등급": SEVERITY_LABEL[sev], "_sev": sev,
                "지연사유": c["delay_reason"] or "-",
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
            st.dataframe(site_sev.sort_values("현장등급", ascending=False), use_container_width=True)

            st.divider()
            st.markdown("#### 리스크 청구 상세")
            display_risk = risk_df.drop(columns=["_sev"]).sort_values("지연일수", ascending=False)
            csv_data_risk = display_risk.to_csv(index=False).encode("utf-8-sig")
            st.dataframe(fmt_money_cols(display_risk, ["청구금액"]), use_container_width=True)
            st.download_button("📥 CSV 다운로드", csv_data_risk,
                                file_name=f"리스크현장_{date.today()}.csv", mime="text/csv")

# ==========================================================================
# TAB: 관리자
# ==========================================================================
with tab_admin:
    st.subheader("🔐 관리자")

    if not is_admin:
        st.warning("🔒 관리자 전용 메뉴입니다. 왼쪽 사이드바에서 비밀번호를 입력하세요.")
    else:
        action = st.radio(
            "작업 선택",
            ["📂 엑셀 일괄 등록", "🏢 공사현황 정보 일괄 등록", "📝 신규 현장/청구 등록",
             "💰 입금 처리", "🗓️ 예정일 연기", "🕒 지연 이력 조회", "📄 계산서 발행 관리", "🛠️ 삭제/병합"],
            horizontal=True
        )
        st.divider()

        # ---------------- 엑셀 일괄 등록 ----------------
        if action == "📂 엑셀 일괄 등록":
            st.caption(
                "원본 엑셀 '이력' 시트를 xlsx 그대로 올리거나 CSV로 저장해서 올리세요. "
                "채권명·현장명·업체명·담당자·기성구분·미수금액(청구금액)·최초예정일·입금예정일·입금여부·입금액·입금상태(입금일자) 인식합니다. "
                "AS 청구도 이제 원래 현장 그대로 들어갑니다 (공사현황에서만 별도 합산됩니다)."
            )
            uploaded_file = st.file_uploader("엑셀(.xlsx) 또는 CSV 업로드", type=["xlsx", "csv"], key="bulk_upload")

            if uploaded_file is not None:
                try:
                    def find_header_row(df_noheader):
                        for i in range(min(10, len(df_noheader))):
                            row_vals = [str(v) for v in df_noheader.iloc[i].tolist()]
                            if any(("채권명" in v) or ("현장명" in v) for v in row_vals):
                                return i
                        return 0

                    if uploaded_file.name.endswith(".csv"):
                        raw_bytes = uploaded_file.read()
                        raw_df = None
                        for enc in ["utf-8-sig", "cp949", "euc-kr", "utf-8"]:
                            try:
                                no_header = pd.read_csv(io.BytesIO(raw_bytes), encoding=enc, header=None)
                                hdr_row = find_header_row(no_header)
                                raw_df = pd.read_csv(io.BytesIO(raw_bytes), encoding=enc, header=hdr_row)
                                break
                            except (UnicodeDecodeError, UnicodeError):
                                continue
                        if raw_df is None:
                            st.error("❌ 인코딩 인식 불가. CSV UTF-8로 다시 저장해주세요.")
                            st.stop()
                    else:
                        no_header = pd.read_excel(uploaded_file, engine="openpyxl", header=None)
                        hdr_row = find_header_row(no_header)
                        uploaded_file.seek(0)
                        raw_df = pd.read_excel(uploaded_file, engine="openpyxl", header=hdr_row)

                    raw_df.columns = [str(c).strip() for c in raw_df.columns]

                    def norm(s):
                        return str(s).replace("\n", "").replace(" ", "").strip()

                    col_lookup = {norm(c): c for c in raw_df.columns}

                    def find_col(*keywords):
                        for norm_name, orig_name in col_lookup.items():
                            if all(kw in norm_name for kw in keywords):
                                return orig_name
                        return None

                    rename_map = {}
                    for kw, target in [("채권명", "bond_name"), ("현장명", "site_name"), ("업체명", "company_name"),
                                        ("담당자", "manager"), ("기성구분", "claim_type"),
                                        ("입금예정일", "current_due_date"), ("최초예정일", "original_due_date"),
                                        ("지연여부", "status_raw"), ("입금여부", "paid_flag"), ("입금액", "paid_amount")]:
                        c = find_col(kw)
                        if c:
                            rename_map[c] = target
                    amt_col = find_col("미수금액") or find_col("청구금액")
                    if amt_col:
                        rename_map[amt_col] = "claim_amount"
                    pay_date_col = find_col("입금상태") or find_col("입금일자")
                    if pay_date_col and pay_date_col not in rename_map:
                        rename_map[pay_date_col] = "payment_date_raw"

                    raw_df = raw_df.rename(columns=rename_map)

                    required = ["site_name", "claim_type", "claim_amount", "current_due_date"]
                    missing = [c for c in required if c not in raw_df.columns]
                    if missing:
                        st.error(f"❌ 필수 컬럼 누락: {missing}")
                        st.write("현재 인식된 컬럼:", list(raw_df.columns))
                        st.dataframe(raw_df.head(5), use_container_width=True)
                    else:
                        raw_df = raw_df.dropna(subset=["site_name"])
                        raw_df["_due_sort"] = pd.to_datetime(raw_df["current_due_date"], errors="coerce")

                        st.write("📋 미리보기:")
                        st.dataframe(raw_df.head(30), use_container_width=True)

                        has_bond = "bond_name" in raw_df.columns
                        n_bonds = raw_df["bond_name"].nunique() if has_bond else len(raw_df)
                        st.info(f"총 {len(raw_df)}행 / 실제 청구(채권) {n_bonds}건 등록 예정 (현장 수 {raw_df['site_name'].nunique()}개)")

                        if st.button("🚀 현장 + 청구 + 입금/지연 이력 일괄 등록 실행", use_container_width=True):
                            group_key = "bond_name" if has_bond else raw_df.index
                            groups = raw_df.groupby(group_key) if has_bond else [(i, raw_df.loc[[i]]) for i in raw_df.index]

                            with engine.connect() as conn:
                                for _, grp in groups:
                                    grp = grp.sort_values("_due_sort", na_position="last")
                                    first = grp.iloc[0]
                                    last = grp.iloc[-1]

                                    site_name = str(first["site_name"]).strip()
                                    company_name = str(first.get("company_name", "") or "")
                                    manager = str(first.get("manager", "") or "")

                                    existing = conn.execute(text("SELECT id FROM sites WHERE site_name=:sn"), {"sn": site_name}).fetchone()
                                    if existing:
                                        site_id = existing[0]
                                    else:
                                        res = conn.execute(text("""
                                            INSERT INTO sites (site_name, company_name, manager) VALUES (:sn, :cn, :mg)
                                        """), {"sn": site_name, "cn": company_name, "mg": manager})
                                        site_id = res.lastrowid

                                    claim_amount = parse_amount(last.get("claim_amount", 0))
                                    orig_due = safe_date(first.get("original_due_date")) or safe_date(first.get("current_due_date"))
                                    orig_due_str = orig_due.isoformat() if orig_due else None

                                    # 그룹 안의 '모든' 행을 훑어서, 입금여부=Y + 입금액>0 인 행은 전부
                                    # 별개의 입금 이벤트로 취급 (한 청구가 여러 번 나눠 입금된 경우 다 잡아내기 위함)
                                    payment_events = []
                                    for _, r in grp.iterrows():
                                        r_paid_flag = str(r.get("paid_flag", "") or "").strip().upper()
                                        r_paid_amount = parse_amount(r.get("paid_amount", 0))
                                        if r_paid_flag == "Y" and r_paid_amount > 0:
                                            r_pay_date = safe_date(r.get("payment_date_raw")) or safe_date(r.get("current_due_date"))
                                            payment_events.append((r_pay_date, r_paid_amount))

                                    total_paid_in_group = sum(a for _, a in payment_events)

                                    cur_due = safe_date(last.get("current_due_date"))
                                    cur_due_str = cur_due.isoformat() if cur_due else None
                                    status_raw = str(last.get("status_raw", "") or "")

                                    if total_paid_in_group >= claim_amount and claim_amount > 0:
                                        status = "완납"
                                    elif total_paid_in_group > 0:
                                        status = "일부입금"
                                    elif cur_due_str is None:
                                        status = "확인필요"
                                    else:
                                        status = "입금대기"

                                    res = conn.execute(text("""
                                        INSERT INTO claims (site_id, claim_type, claim_date, original_due_date, current_due_date, claim_amount, status)
                                        VALUES (:s_id, :ctype, :c_date, :o_due, :c_due, :amt, :status)
                                    """), {"s_id": site_id, "ctype": last["claim_type"], "c_date": orig_due_str,
                                           "o_due": orig_due_str, "c_due": cur_due_str, "amt": claim_amount, "status": status})
                                    claim_id = res.lastrowid

                                    # 이력 안에서 예정일이 바뀐 구간들을 '자동지연' 이력으로 복원
                                    prev_due = orig_due
                                    for _, r in grp.iterrows():
                                        this_due = safe_date(r.get("current_due_date"))
                                        if this_due and prev_due and this_due != prev_due:
                                            conn.execute(text("""
                                                INSERT INTO claim_delay_history (claim_id, event_type, old_due_date, new_due_date, delay_days, reason)
                                                VALUES (:cid, '자동지연', :old, :new, :ddays, '엑셀 이력 데이터 가져오기')
                                            """), {"cid": claim_id, "old": prev_due.isoformat(), "new": this_due.isoformat(),
                                                   "ddays": (this_due - prev_due).days})
                                        if this_due:
                                            prev_due = this_due

                                    # 모든 입금 이벤트를 각각 별개의 payments 레코드로 등록
                                    last_pay_date = None
                                    for pdate, pamt in payment_events:
                                        pdate_final = pdate or cur_due or date.today()
                                        conn.execute(text("""
                                            INSERT INTO payments (site_id, claim_id, payment_date, payment_amount)
                                            VALUES (:sid, :cid, :pdate, :pamt)
                                        """), {"sid": site_id, "cid": claim_id, "pdate": pdate_final.isoformat(), "pamt": pamt})
                                        last_pay_date = pdate_final

                                    if status == "완납" and last_pay_date:
                                        delay_days = 0
                                        event_type = "입금완료(정상)"
                                        if orig_due and last_pay_date > orig_due:
                                            delay_days = (last_pay_date - orig_due).days
                                            event_type = "입금완료(지연)"
                                        conn.execute(text("""
                                            INSERT INTO claim_delay_history (claim_id, event_type, old_due_date, new_due_date, payment_date, delay_days, reason)
                                            VALUES (:cid, :etype, :old, :old, :pdate, :ddays, '엑셀 이력 데이터 가져오기')
                                        """), {"cid": claim_id, "etype": event_type, "old": cur_due_str,
                                               "pdate": last_pay_date.isoformat(), "ddays": delay_days})
                                conn.commit()
                            st.success("✅ 현장 + 청구 + 입금/지연 이력까지 일괄 등록 완료!")
                            st.rerun()
                except Exception as e:
                    st.error(f"❌ 파일 처리 오류: {e}")

        # ---------------- 공사현황 정보 일괄 등록 ----------------
        elif action == "🏢 공사현황 정보 일괄 등록":
            st.caption("계약금액, 추가계약, 계약일, 착공일, 준공일, 공정율을 엑셀로 한 번에 입력합니다.")
            with engine.connect() as conn:
                cur_sites = pd.read_sql(
                    "SELECT site_name, contract_amount, additional_amount, contract_date, start_date, completion_date, progress_rate FROM sites ORDER BY site_name;",
                    conn
                )
            template_df = cur_sites.rename(columns={
                "site_name": "현장명", "contract_amount": "계약금액", "additional_amount": "추가계약",
                "contract_date": "계약일", "start_date": "착공일", "completion_date": "준공일", "progress_rate": "공정율"
            })
            st.download_button("📥 현재 현장 목록 템플릿 다운로드", data=template_df.to_csv(index=False).encode("utf-8-sig"),
                                file_name="공사현황_입력템플릿.csv", mime="text/csv", use_container_width=True)

            st.divider()
            site_info_file = st.file_uploader("작성한 템플릿(엑셀/CSV) 업로드", type=["xlsx", "csv"], key="site_info_upload")

            if site_info_file is not None:
                try:
                    if site_info_file.name.endswith(".csv"):
                        raw_bytes = site_info_file.read()
                        info_df = None
                        for enc in ["utf-8-sig", "cp949", "euc-kr", "utf-8"]:
                            try:
                                info_df = pd.read_csv(io.BytesIO(raw_bytes), encoding=enc)
                                break
                            except (UnicodeDecodeError, UnicodeError):
                                continue
                        if info_df is None:
                            st.error("❌ 인코딩 인식 불가.")
                            st.stop()
                    else:
                        info_df = pd.read_excel(site_info_file, engine="openpyxl")

                    info_df.columns = [str(c).strip() for c in info_df.columns]
                    if "현장명" not in info_df.columns:
                        st.error("❌ '현장명' 컬럼이 없습니다.")
                    else:
                        st.dataframe(info_df.head(20), use_container_width=True)
                        if st.button("🚀 공사현황 정보 일괄 반영", use_container_width=True):
                            with engine.connect() as conn:
                                updated, created = 0, 0
                                for _, row in info_df.iterrows():
                                    site_name = str(row["현장명"]).strip()
                                    if not site_name or site_name == "nan":
                                        continue
                                    existing = conn.execute(text("SELECT id FROM sites WHERE site_name=:sn"), {"sn": site_name}).fetchone()
                                    if not existing:
                                        res = conn.execute(text("INSERT INTO sites (site_name) VALUES (:sn)"), {"sn": site_name})
                                        site_id = res.lastrowid
                                        created += 1
                                    else:
                                        site_id = existing[0]
                                        updated += 1

                                    cd = safe_date(row.get("계약일")); sd = safe_date(row.get("착공일")); ed = safe_date(row.get("준공일"))
                                    conn.execute(text("""
                                        UPDATE sites SET contract_amount=:ca, additional_amount=:aa, progress_rate=:pr,
                                               contract_date=:cd, start_date=:sd, completion_date=:ed
                                        WHERE id=:sid
                                    """), {"ca": parse_amount(row.get("계약금액", 0)), "aa": parse_amount(row.get("추가계약", 0)),
                                           "pr": float(row.get("공정율", 0) or 0),
                                           "cd": cd.isoformat() if cd else None, "sd": sd.isoformat() if sd else None,
                                           "ed": ed.isoformat() if ed else None, "sid": site_id})
                                conn.commit()
                            st.success(f"✅ 반영 완료! (기존 {updated}건 갱신, 신규 {created}건 생성)")
                            st.rerun()
                except Exception as e:
                    st.error(f"❌ 파일 처리 오류: {e}")

        # ---------------- 신규 현장/청구 등록 ----------------
        elif action == "📝 신규 현장/청구 등록":
            st.markdown("#### 🏢 신규 현장 등록")
            with st.form("site_form", clear_on_submit=True):
                col1, col2 = st.columns(2)
                site_name = col1.text_input("현장명 *")
                company_name = col1.text_input("거래업체명")
                manager = col2.text_input("담당자")
                if st.form_submit_button("현장 등록"):
                    if site_name:
                        with engine.connect() as conn:
                            try:
                                conn.execute(text("INSERT INTO sites (site_name, company_name, manager) VALUES (:sn, :cn, :mg)"),
                                             {"sn": site_name, "cn": company_name, "mg": manager})
                                conn.commit()
                                st.success("등록 완료!")
                                st.rerun()
                            except Exception as e:
                                st.error(f"등록 실패 (중복 현장명일 수 있음): {e}")
                    else:
                        st.warning("현장명은 필수입니다.")

            st.divider()
            st.markdown("#### 📋 신규 청구 등록")
            with engine.connect() as conn:
                sites = pd.read_sql("SELECT id, site_name FROM sites ORDER BY site_name;", conn)
            if not sites.empty:
                site_dict = dict(zip(sites["site_name"], sites["id"]))
                sel_site = st.selectbox("현장 선택", list(site_dict.keys()))
                with st.form("claim_form", clear_on_submit=True):
                    c1, c2, c3 = st.columns(3)
                    claim_type = c1.selectbox("채권 종류", CLAIM_TYPES)
                    claim_date_v = c2.date_input("청구일")
                    due_date_v = c3.date_input("입금 예정일")
                    claim_amount_v = st.number_input("청구 금액(원)", min_value=0, step=100000)
                    if st.form_submit_button("청구 등록"):
                        with engine.connect() as conn:
                            conn.execute(text("""
                                INSERT INTO claims (site_id, claim_type, claim_date, original_due_date, current_due_date, claim_amount, status)
                                VALUES (:sid, :ctype, :cdate, :odue, :cdue, :amt, '입금대기')
                            """), {"sid": site_dict[sel_site], "ctype": claim_type, "cdate": str(claim_date_v),
                                   "odue": str(due_date_v), "cdue": str(due_date_v), "amt": claim_amount_v})
                            conn.commit()
                        st.success("청구 등록 완료!")
                        st.rerun()
            else:
                st.info("현장을 먼저 등록해주세요.")

        # ---------------- 입금 처리 ----------------
        elif action == "💰 입금 처리":
            st.caption("현장을 먼저 골라서 청구 목록을 좁힌 다음, 처리할 청구를 선택하세요. 나눠서 입금돼도(일부입금) 같은 청구에 계속 쌓입니다.")
            with engine.connect() as conn:
                open_claims = pd.read_sql("""
                    SELECT c.id, s.site_name, c.claim_type, c.claim_amount, c.current_due_date, c.status
                    FROM claims c JOIN sites s ON c.site_id = s.id
                    WHERE c.status != '완납'
                """, conn)
            if not open_claims.empty:
                open_claims["_sort"] = pd.to_datetime(open_claims["current_due_date"], errors="coerce")
                open_claims = open_claims.sort_values("_sort", na_position="last").drop(columns=["_sort"])

            if open_claims.empty:
                st.info("미완납 청구가 없습니다.")
            else:
                with engine.connect() as conn:
                    paid_map = dict(pd.read_sql("SELECT claim_id, SUM(payment_amount) as p FROM payments GROUP BY claim_id;", conn).values)

                site_options = ["전체"] + sorted(open_claims["site_name"].unique().tolist())
                site_pick = st.selectbox("현장 선택 (좁혀서 찾기)", site_options, key="pay_site_pick")
                filtered = open_claims if site_pick == "전체" else open_claims[open_claims["site_name"] == site_pick]

                if filtered.empty:
                    st.info("해당 현장에 미완납 청구가 없습니다.")
                else:
                    options = {}
                    for r in filtered.itertuples():
                        already_paid = paid_map.get(r.id, 0) or 0
                        remain = r.claim_amount - already_paid
                        options[f"#{r.id} | {r.site_name} | {r.claim_type} | 청구 {r.claim_amount:,}원 (남은 {remain:,}원) | 예정일 {r.current_due_date} | ({r.status})"] = r.id

                    with st.form("payment_process_form"):
                        pick = st.selectbox("입금 처리할 청구 선택", list(options.keys()))
                        pay_date_v = st.date_input("실제 입금일", value=date.today())
                        pay_amount_v = st.number_input("입금 금액(원)", min_value=0, step=100000)
                        if st.form_submit_button("✅ 입금 처리"):
                            claim_id = options[pick]
                            with engine.connect() as conn:
                                claim_row = conn.execute(
                                    text("SELECT site_id, claim_amount, original_due_date FROM claims WHERE id=:cid"), {"cid": claim_id}
                                ).fetchone()
                                site_id, claim_amount, orig_due = claim_row

                                conn.execute(text("""
                                    INSERT INTO payments (site_id, claim_id, payment_date, payment_amount)
                                    VALUES (:sid, :cid, :pdate, :pamt)
                                """), {"sid": site_id, "cid": claim_id, "pdate": str(pay_date_v), "pamt": pay_amount_v})

                                total_paid = conn.execute(
                                    text("SELECT SUM(payment_amount) FROM payments WHERE claim_id=:cid"), {"cid": claim_id}
                                ).fetchone()[0] or 0

                                orig_due_d = safe_date(orig_due)
                                if total_paid >= claim_amount:
                                    new_status = "완납"
                                    delay_days = 0
                                    event_type = "입금완료(정상)"
                                    if orig_due_d and pay_date_v > orig_due_d:
                                        delay_days = (pay_date_v - orig_due_d).days
                                        event_type = "입금완료(지연)"
                                    conn.execute(text("""
                                        INSERT INTO claim_delay_history (claim_id, event_type, payment_date, delay_days, reason)
                                        VALUES (:cid, :etype, :pdate, :ddays, '입금 처리')
                                    """), {"cid": claim_id, "etype": event_type, "pdate": str(pay_date_v), "ddays": delay_days})
                                else:
                                    new_status = "일부입금"

                                conn.execute(text("UPDATE claims SET status=:status WHERE id=:cid"), {"status": new_status, "cid": claim_id})
                                conn.commit()
                            st.success(f"입금 처리 완료! (상태: {new_status}, 누적입금 {total_paid:,}원 / 청구 {claim_amount:,}원)")
                            st.rerun()

        # ---------------- 예정일 연기 ----------------
        elif action == "🗓️ 예정일 연기":
            st.caption("현장을 먼저 골라서 청구 목록을 좁힌 다음, 연기할 청구를 선택하세요.")
            with engine.connect() as conn:
                open_claims = pd.read_sql("""
                    SELECT c.id, s.site_name, c.claim_type, c.claim_amount, c.current_due_date, c.status
                    FROM claims c JOIN sites s ON c.site_id = s.id
                    WHERE c.status != '완납'
                """, conn)
            if not open_claims.empty:
                open_claims["_sort"] = pd.to_datetime(open_claims["current_due_date"], errors="coerce")
                open_claims = open_claims.sort_values("_sort", na_position="last").drop(columns=["_sort"])

            if open_claims.empty:
                st.info("미완납 청구가 없습니다.")
            else:
                site_options = ["전체"] + sorted(open_claims["site_name"].unique().tolist())
                site_pick = st.selectbox("현장 선택 (좁혀서 찾기)", site_options, key="postpone_site_pick")
                filtered = open_claims if site_pick == "전체" else open_claims[open_claims["site_name"] == site_pick]

                if filtered.empty:
                    st.info("해당 현장에 미완납 청구가 없습니다.")
                else:
                    options = {
                        f"#{r.id} | {r.site_name} | {r.claim_type} | {r.claim_amount:,}원 | 현재 예정일 {r.current_due_date} | ({r.status})": r.id
                        for r in filtered.itertuples()
                    }
                    with st.form("postpone_form"):
                        pick = st.selectbox("연기할 청구 선택", list(options.keys()))
                        new_due = st.date_input("새 입금 예정일")
                        new_status = st.selectbox("상태 변경", ["입금대기", "일부입금", "확인필요"])
                        reason = st.text_input("연기 사유")
                        if st.form_submit_button("🗓️ 예정일 변경 저장"):
                            claim_id = options[pick]
                            with engine.connect() as conn:
                                old_due = conn.execute(text("SELECT current_due_date FROM claims WHERE id=:cid"), {"cid": claim_id}).fetchone()[0]
                                conn.execute(text("""
                                    UPDATE claims SET current_due_date=:new_due, status=:status, delay_reason=:reason, last_flagged_due_date=NULL
                                    WHERE id=:cid
                                """), {"new_due": str(new_due), "status": new_status, "reason": reason, "cid": claim_id})

                                old_due_d = safe_date(old_due)
                                ddays = (new_due - old_due_d).days if old_due_d else 0
                                conn.execute(text("""
                                    INSERT INTO claim_delay_history (claim_id, event_type, old_due_date, new_due_date, delay_days, reason)
                                    VALUES (:cid, '연기', :old, :new, :ddays, :reason)
                                """), {"cid": claim_id, "old": old_due, "new": str(new_due), "ddays": ddays, "reason": reason})
                                conn.commit()
                            st.success("예정일이 변경되었습니다. (지연횟수는 이 예정일도 지나야 다시 카운트됩니다)")
                            st.rerun()

        # ---------------- 지연 이력 조회 ----------------
        elif action == "🕒 지연 이력 조회":
            with engine.connect() as conn:
                hist = pd.read_sql("""
                    SELECT h.id, s.site_name, c.claim_type, c.claim_amount, h.event_type,
                           h.changed_at, h.old_due_date, h.new_due_date, h.payment_date, h.delay_days, h.reason
                    FROM claim_delay_history h
                    JOIN claims c ON h.claim_id = c.id
                    JOIN sites s ON c.site_id = s.id
                    ORDER BY h.changed_at DESC;
                """, conn)
            if hist.empty:
                st.info("아직 지연/연기/입금 이력이 없습니다.")
            else:
                site_pick = st.selectbox("현장 필터", ["전체"] + sorted(hist["site_name"].unique().tolist()))
                disp = hist if site_pick == "전체" else hist[hist["site_name"] == site_pick]
                st.dataframe(fmt_money_cols(disp, ["claim_amount"]), use_container_width=True)
                st.divider()
                st.markdown("#### 📊 청구별 누적 지연 횟수")
                agg = hist[hist["event_type"] == "자동지연"].groupby(["site_name", "claim_type"]).agg(
                    지연횟수=("event_type", "count"), 최대지연일수=("delay_days", "max")
                ).reset_index()
                st.dataframe(agg, use_container_width=True)

        # ---------------- 계산서 발행 관리 ----------------
        elif action == "📄 계산서 발행 관리":
            with engine.connect() as conn:
                sites = pd.read_sql("SELECT id, site_name FROM sites ORDER BY site_name;", conn)
            if sites.empty:
                st.info("현장을 먼저 등록해주세요.")
            else:
                site_dict = dict(zip(sites["site_name"], sites["id"]))
                sel_site = st.selectbox("현장 선택", list(site_dict.keys()))
                with st.form("invoice_form", clear_on_submit=True):
                    col1, col2, col3 = st.columns(3)
                    inv_type = col1.selectbox("기성종류", CLAIM_TYPES)
                    inv_date_v = col2.date_input("발행일")
                    inv_amount_v = col3.number_input("발행 금액(원)", min_value=0, step=100000)
                    if st.form_submit_button("📄 계산서 발행 등록"):
                        with engine.connect() as conn:
                            conn.execute(text("""
                                INSERT INTO tax_invoices (site_id, claim_type, issue_date, invoice_amount)
                                VALUES (:sid, :ctype, :idate, :amt)
                            """), {"sid": site_dict[sel_site], "ctype": inv_type, "idate": str(inv_date_v), "amt": inv_amount_v})
                            conn.commit()
                        st.success("계산서 발행 내역이 등록되었습니다. (공사현황에 자동 반영됨)")
                        st.rerun()

                st.divider()
                with engine.connect() as conn:
                    inv_list = pd.read_sql("""
                        SELECT i.id, s.site_name, i.claim_type, i.issue_date, i.invoice_amount
                        FROM tax_invoices i JOIN sites s ON i.site_id = s.id
                        ORDER BY i.issue_date DESC;
                    """, conn)
                st.dataframe(fmt_money_cols(inv_list, ["invoice_amount"]), use_container_width=True)
                del_id = st.number_input("삭제할 계산서 ID", min_value=1, step=1)
                if st.button("❌ 계산서 삭제"):
                    with engine.connect() as conn:
                        conn.execute(text("DELETE FROM tax_invoices WHERE id=:id;"), {"id": del_id})
                        conn.commit()
                    st.success("삭제 완료!")
                    st.rerun()

        # ---------------- 삭제/병합 ----------------
        elif action == "🛠️ 삭제/병합":
            mod_type = st.radio("항목 선택", ["현장 삭제", "현장 병합", "청구 삭제", "입금내역 삭제", "⚠️ 전체 데이터 초기화"], horizontal=True)
            with engine.connect() as conn:
                if mod_type == "⚠️ 전체 데이터 초기화":
                    st.error("모든 현장/청구/입금/계산서/이력 데이터가 삭제됩니다. 되돌릴 수 없습니다.")
                    confirm = st.checkbox("정말로 전체 삭제하겠습니다")
                    if st.button("🗑️ 전체 초기화 실행", disabled=not confirm):
                        for t in ["claim_delay_history", "payments", "tax_invoices", "claims", "sites"]:
                            conn.execute(text(f"DELETE FROM {t};"))
                        conn.commit()
                        st.success("전체 초기화 완료!")
                        st.rerun()

                elif mod_type == "현장 삭제":
                    sites = pd.read_sql("SELECT id, site_name, company_name, manager FROM sites;", conn)
                    st.dataframe(sites, use_container_width=True)
                    del_id = st.number_input("삭제할 현장 ID", min_value=1, step=1)
                    if st.button("❌ 현장 삭제 (관련 청구/입금/계산서 함께 삭제)"):
                        conn.execute(text("DELETE FROM sites WHERE id=:id;"), {"id": del_id})
                        conn.commit()
                        st.success("삭제 완료!")
                        st.rerun()

                elif mod_type == "현장 병합":
                    sites = pd.read_sql("SELECT id, site_name FROM sites ORDER BY site_name;", conn)
                    st.dataframe(sites, use_container_width=True)
                    col1, col2 = st.columns(2)
                    from_id = col1.number_input("병합될(없어질) 현장 ID", min_value=1, step=1, key="mfrom")
                    to_id = col2.number_input("남을 현장 ID", min_value=1, step=1, key="mto")
                    if st.button("🔀 병합 실행"):
                        conn.execute(text("UPDATE claims SET site_id=:to WHERE site_id=:frm;"), {"to": to_id, "frm": from_id})
                        conn.execute(text("UPDATE payments SET site_id=:to WHERE site_id=:frm;"), {"to": to_id, "frm": from_id})
                        conn.execute(text("UPDATE tax_invoices SET site_id=:to WHERE site_id=:frm;"), {"to": to_id, "frm": from_id})
                        conn.execute(text("DELETE FROM sites WHERE id=:frm;"), {"frm": from_id})
                        conn.commit()
                        st.success("병합 완료!")
                        st.rerun()

                elif mod_type == "청구 삭제":
                    claims = pd.read_sql("SELECT c.id, s.site_name, c.claim_type, c.claim_amount FROM claims c JOIN sites s ON c.site_id=s.id;", conn)
                    st.dataframe(fmt_money_cols(claims, ["claim_amount"]), use_container_width=True)
                    del_id = st.number_input("삭제할 청구 ID", min_value=1, step=1)
                    if st.button("❌ 청구 삭제"):
                        conn.execute(text("DELETE FROM claims WHERE id=:id;"), {"id": del_id})
                        conn.commit()
                        st.success("삭제 완료!")
                        st.rerun()

                elif mod_type == "입금내역 삭제":
                    payments = pd.read_sql("SELECT p.id, s.site_name, p.payment_date, p.payment_amount FROM payments p JOIN sites s ON p.site_id=s.id;", conn)
                    st.dataframe(fmt_money_cols(payments, ["payment_amount"]), use_container_width=True)
                    del_id = st.number_input("삭제할 입금 ID", min_value=1, step=1)
                    if st.button("❌ 입금내역 삭제"):
                        conn.execute(text("DELETE FROM payments WHERE id=:id;"), {"id": del_id})
                        conn.commit()
                        st.success("삭제 완료!")
                        st.rerun()