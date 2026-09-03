"""
Ежедневный мониторинг проектов в Jira
Версия 5.1 (без вкладки Табель, с исправлением SSL и проверками)
"""

import streamlit as st
import requests
import pandas as pd
import json
import os
import calendar
import urllib3
from datetime import datetime, timedelta, timezone
from io import BytesIO

# Отключаем предупреждения о небезопасных SSL-соединениях
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ═══════════════════════════════════════════════════════
# КОНСТАНТЫ
# ═══════════════════════════════════════════════════════
CONFIG_FILE = "config.json"

DONE_STATUSES_LOWER = ["закрыта", "на приёмке", "на приемке"]
EXCLUDED_STATUSES_LOWER = ["пул заданий", "на приёмке", "на приемке", "закрыта"]
IN_PROGRESS_LOWER = "в работе"
BUG_TYPE = "Ошибка (подзадача)"
FEATURE_TYPE = "Фича"


# ═══════════════════════════════════════════════════════
# КОНФИГУРАЦИЯ
# ═══════════════════════════════════════════════════════
def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_config(config):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
    except Exception as e:
        st.error(f"Ошибка сохранения: {e}")


# ═══════════════════════════════════════════════════════
# JIRA API
# ═══════════════════════════════════════════════════════
@st.cache_data(ttl=300, show_spinner=False)
def get_all_issues(jira_url, auth, project_key, jql_extra=""):
    url = f"{jira_url}/rest/api/2/search"
    fields = (
        "summary,status,assignee,priority,created,updated,duedate,"
        "timetracking,fixVersions,issuetype,parent,project"
    )
    jql = f"project = {project_key} {jql_extra} ORDER BY key ASC"
    all_issues = []
    start_at = 0

    while True:
        params = {
            "jql": jql,
            "startAt": start_at,
            "maxResults": 100,
            "fields": fields,
        }
        try:
            resp = requests.get(url, auth=auth, params=params, timeout=60, verify=False)
            resp.raise_for_status()
            data = resp.json()
            all_issues.extend(data["issues"])
            start_at += 100
            if start_at >= data["total"]:
                break
        except Exception as e:
            st.error(f"Ошибка загрузки задач: {e}")
            break
    return all_issues


@st.cache_data(ttl=300, show_spinner=False)
def get_issue_changelog(jira_url, auth, issue_key):
    url = f"{jira_url}/rest/api/2/issue/{issue_key}"
    try:
        resp = requests.get(url, auth=auth, params={"expand": "changelog"}, timeout=30, verify=False)
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return None


def get_all_worklogs_for_issues(jira_url, auth, issues, year, month):
    """Получает worklogs для списка задач за указанный месяц"""
    start_date = f"{year}-{month:02d}-01"
    last_day = calendar.monthrange(year, month)[1]
    end_date = f"{year}-{month:02d}-{last_day:02d}"

    worklogs_by_author_day = {}

    for issue in issues:
        key = issue["key"]
        try:
            url = f"{jira_url}/rest/api/2/issue/{key}/worklog"
            resp = requests.get(url, auth=auth, timeout=30, verify=False)
            if resp.status_code != 200:
                continue
            data = resp.json()
            for wl in data.get("worklogs", []):
                wl_started = wl.get("started", "")
                if not wl_started:
                    continue
                wl_date = wl_started[:10]
                if start_date <= wl_date <= end_date:
                    author = wl["author"]["displayName"]
                    day = int(wl_date[8:10])
                    seconds = wl.get("timeSpentSeconds", 0)
                    if author not in worklogs_by_author_day:
                        worklogs_by_author_day[author] = {}
                    worklogs_by_author_day[author][day] = (
                        worklogs_by_author_day[author].get(day, 0) + seconds
                    )
        except Exception:
            continue

    return worklogs_by_author_day


def get_team_members(issues):
    members = set()
    for issue in issues:
        assignee = issue["fields"].get("assignee")
        if assignee:
            members.add(assignee["displayName"])
    return sorted(members)


# ═══════════════════════════════════════════════════════
# ПРОВЕРКА 1: ЗОНА ВЫПОЛНЕНИЯ
# ═══════════════════════════════════════════════════════
def check_execution_zone(jira_url, issues):
    results = []
    for issue in issues:
        f = issue["fields"]
        tt = f.get("timetracking") or {}
        orig = tt.get("originalEstimateSeconds", 0) or 0
        spent = tt.get("timeSpentSeconds", 0) or 0
        status = f["status"]["name"]

        if orig > 0 and spent > 0 and status.lower() not in DONE_STATUSES_LOWER:
            ratio = spent / orig
            if ratio >= 0.6:
                results.append({
                    "Ключ": issue["key"],
                    "Ссылка": f"{jira_url}/browse/{issue['key']}",
                    "Название": f["summary"],
                    "Исполнитель": f["assignee"]["displayName"] if f.get("assignee") else "—",
                    "Оценка (ч)": round(orig / 3600, 1),
                    "Факт (ч)": round(spent / 3600, 1),
                    "%": round(ratio * 100, 1),
                    "Статус": status,
                })

    return pd.DataFrame(results) if results else pd.DataFrame(
        columns=["Ключ", "Ссылка", "Название", "Исполнитель", "Оценка (ч)", "Факт (ч)", "%", "Статус"]
    )


# ═══════════════════════════════════════════════════════
# ПРОВЕРКА 2: КОРРЕКТНОСТЬ СТАТУСОВ
# ═══════════════════════════════════════════════════════
def check_status_correctness(jira_url, auth, issues, progress_callback=None):
    risks = []
    now = datetime.now(timezone.utc)

    to_check = []
    for issue in issues:
        f = issue["fields"]
        status_lower = f["status"]["name"].lower().strip()
        if f["issuetype"]["name"] == FEATURE_TYPE:
            continue
        if status_lower in EXCLUDED_STATUSES_LOWER:
            continue
        to_check.append(issue)

    total = len(to_check)
    for idx, issue in enumerate(to_check):
        if progress_callback:
            progress_callback((idx + 1) / total)

        key = issue["key"]
        current_status = issue["fields"]["status"]["name"].strip()
        current_status_lower = current_status.lower()
        tt = issue["fields"].get("timetracking") or {}
        orig = tt.get("originalEstimateSeconds", 0) or 0

        full = get_issue_changelog(jira_url, auth, key)
        if not full:
            continue

        histories = full.get("changelog", {}).get("histories", [])

        last_transition_time = None
        for history in histories:
            created_str = history["created"]
            for item in history.get("items", []):
                if item["field"] == "status":
                    to_status = item.get("toString", "").strip().lower()
                    if to_status == current_status_lower:
                        try:
                            t = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
                            if t.tzinfo is None:
                                t = t.replace(tzinfo=timezone.utc)
                            last_transition_time = t
                        except Exception:
                            pass

        if last_transition_time is None:
            created_str = issue["fields"].get("created", "")
            if created_str:
                try:
                    last_transition_time = datetime.fromisoformat(
                        created_str.replace("Z", "+00:00")
                    )
                    if last_transition_time.tzinfo is None:
                        last_transition_time = last_transition_time.replace(tzinfo=timezone.utc)
                except Exception:
                    continue
            else:
                continue

        hours_in_status = (now - last_transition_time).total_seconds() / 3600

        if current_status_lower == IN_PROGRESS_LOWER:
            if orig <= 28800:
                limit = 48
            else:
                limit = (orig / 28800) * 24
        else:
            limit = 24

        if hours_in_status > limit:
            risks.append({
                "Ключ": key,
                "Название": issue["fields"]["summary"],
                "Исполнитель": issue["fields"]["assignee"]["displayName"] if issue["fields"].get("assignee") else "—",
                "Статус": current_status,
                "В статусе (ч)": round(hours_in_status, 1),
                "Лимит (ч)": round(limit, 1),
                "Превышение (ч)": round(hours_in_status - limit, 1),
            })

    return pd.DataFrame(risks) if risks else pd.DataFrame(
        columns=["Ключ", "Название", "Исполнитель", "Статус", "В статусе (ч)", "Лимит (ч)", "Превышение (ч)"]
    )


# ═══════════════════════════════════════════════════════
# ПРОВЕРКА 3: РЕЛИЗЫ
# ═══════════════════════════════════════════════════════
def check_releases(issues, release_budgets):
    releases = {}
    for issue in issues:
        f = issue["fields"]
        versions = f.get("fixVersions") or []
        if not versions:
            continue

        tt = f.get("timetracking") or {}
        spent = tt.get("timeSpentSeconds", 0) or 0

        for v in versions:
            name = v["name"]
            if name not in releases:
                releases[name] = 0
            releases[name] += spent

    results = []
    for name, spent_sec in releases.items():
        x = release_budgets.get(name, 0)
        y = round(spent_sec / 3600, 1)
        ratio = round(x / y, 2) if y > 0 else "—"
        results.append({
            "Релиз": name,
            "ТРЗ по договору (ч)": x,
            "Факт (ч)": y,
            "X / Y": f"{x} / {y} = {ratio}",
        })

    return pd.DataFrame(results) if results else pd.DataFrame(
        columns=["Релиз", "ТРЗ по договору (ч)", "Факт (ч)", "X / Y"]
    )


# ═══════════════════════════════════════════════════════
# ПРОВЕРКА 4: ОШИБКИ
# ═══════════════════════════════════════════════════════
def check_bugs(jira_url, issues):
    open_bugs = []
    bug_hours = {}
    unplanned = 0

    for issue in issues:
        f = issue["fields"]
        itype = f["issuetype"]["name"]
        status_lower = f["status"]["name"].lower()
        tt = f.get("timetracking") or {}
        orig = tt.get("originalEstimateSeconds", 0) or 0
        spent = tt.get("timeSpentSeconds", 0) or 0

        if orig == 0 and spent > 0:
            unplanned += spent

        versions = f.get("fixVersions") or []
        rel = versions[0]["name"] if versions else "Без релиза"

        if itype == BUG_TYPE:
            if spent > 0:
                bug_hours[rel] = bug_hours.get(rel, 0) + spent
            if status_lower not in DONE_STATUSES_LOWER:
                parent = f.get("parent")
                open_bugs.append({
                    "Ключ": issue["key"],
                    "Ссылка": f"{jira_url}/browse/{issue['key']}",
                    "Название": f["summary"],
                    "Релиз": rel,
                    "Исполнитель": f["assignee"]["displayName"] if f.get("assignee") else "—",
                    "Факт (ч)": round(spent / 3600, 1),
                    "Статус": f["status"]["name"],
                    "Родитель": parent["key"] if parent else "—",
                })

    df_bugs = pd.DataFrame(open_bugs) if open_bugs else pd.DataFrame(
        columns=["Ключ", "Ссылка", "Название", "Релиз", "Исполнитель", "Факт (ч)", "Статус", "Родитель"]
    )
    df_stats = pd.DataFrame([
        {"Релиз": k, "Трудозатраты на Ошибки (ч)": round(v / 3600, 1)}
        for k, v in bug_hours.items()
    ]) if bug_hours else pd.DataFrame(columns=["Релиз", "Трудозатраты на Ошибки (ч)"])

    return df_bugs, df_stats, f"Незапланированный объем: {round(unplanned / 3600, 1)} ч"


# ═══════════════════════════════════════════════════════
# ПРОВЕРКА 5: ЧАСЫ ЗА МЕСЯЦ
# ═══════════════════════════════════════════════════════
def check_monthly_hours(issues, worklogs_data, planned_hours):
    team = get_team_members(issues)
    results = []
    for member in team:
        day_data = worklogs_data.get(member, {})
        total_sec = sum(day_data.values())
        fact = round(total_sec / 3600, 1)
        plan = planned_hours.get(member, 0)
        results.append({
            "Сотрудник": member,
            "План (ч)": plan,
            "Факт (ч)": fact,
            "Остаток (ч)": round(plan - fact, 1),
        })
    return pd.DataFrame(results) if results else pd.DataFrame(
        columns=["Сотрудник", "План (ч)", "Факт (ч)", "Остаток (ч)"]
    )


# ═══════════════════════════════════════════════════════
# ПРОВЕРКА 6: НЕЗАНУЛЕННЫЕ
# ═══════════════════════════════════════════════════════
def check_not_zeroed(issues):
    results = []
    for issue in issues:
        f = issue["fields"]
        if f["status"]["name"].lower() != "закрыта":
            continue
        if f["issuetype"]["name"] == FEATURE_TYPE:
            continue
        tt = f.get("timetracking") or {}
        remaining = tt.get("remainingEstimateSeconds", 0) or 0
        if remaining > 0:
            results.append({
                "Ключ": issue["key"],
                "Название": f["summary"],
                "Исполнитель": f["assignee"]["displayName"] if f.get("assignee") else "—",
                "Остаток (ч)": round(remaining / 3600, 1),
            })
    return pd.DataFrame(results) if results else pd.DataFrame(
        columns=["Ключ", "Название", "Исполнитель", "Остаток (ч)"]
    )


# ═══════════════════════════════════════════════════════
# ЭКСПОРТ
# ═══════════════════════════════════════════════════════
def export_to_excel(all_results):
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        for proj, data in all_results.items():
            p = proj[:15]
            data["execution_zone"].to_excel(writer, sheet_name=f"{p}_1_Зона", index=False)
            data["status_correctness"].to_excel(writer, sheet_name=f"{p}_2_Статусы", index=False)
            data["releases"].to_excel(writer, sheet_name=f"{p}_3_Релизы", index=False)
            data["bugs"].to_excel(writer, sheet_name=f"{p}_4_Ошибки", index=False)
            data["monthly_hours"].to_excel(writer, sheet_name=f"{p}_5_Часы", index=False)
            data["not_zeroed"].to_excel(writer, sheet_name=f"{p}_6_Незанул", index=False)
    output.seek(0)
    return output


# ═══════════════════════════════════════════════════════
# ПРИЛОЖЕНИЕ
# ═══════════════════════════════════════════════════════
def main():
    st.set_page_config(page_title="Мониторинг проектов", layout="wide", page_icon="📊")
    st.title("📊 Ежедневный мониторинг проектов")

    config = load_config()

    # ═══════════ SIDEBAR ═══════════
    with st.sidebar:
        st.header("⚙️ Настройки")
        jira_url = st.text_input("URL Jira", value=config.get("jira_url", "https://jira.tomskasu.ru"))
        login = st.text_input("Логин", value=config.get("login", ""))
        password = st.text_input("Пароль", value=config.get("password", ""), type="password")
        st.divider()

        projects = []
        connection_ok = False
        if login and password:
            auth = (login, password)
            try:
                resp = requests.get(
                    f"{jira_url}/rest/api/2/project",
                    auth=auth,
                    timeout=15,
                    verify=False
                )
                if resp.status_code == 200:
                    projects = [p["key"] for p in resp.json()]
                    connection_ok = True
                    st.success(f"✅ Подключено. Проектов: {len(projects)}")
                else:
                    st.error(f"❌ Ошибка Jira: {resp.status_code}")
                    st.text(resp.text[:300])
            except Exception as e:
                st.error(f"❌ Не удалось подключиться: {e}")

        selected_projects = st.multiselect(
            "Проекты", options=projects,
            default=config.get("selected_projects", []),
        )

        if st.button("💾 Сохранить настройки"):
            config.update({
                "jira_url": jira_url,
                "login": login,
                "password": password,
                "selected_projects": selected_projects,
            })
            save_config(config)
            st.success("Сохранено!")

    # ═══════════ ПРОВЕРКИ ДО СОЗДАНИЯ ВКЛАДОК ═══════════
    if not login or not password:
        st.warning("⚠️ Введите логин и пароль в боковой панели")
        st.stop()

    if not connection_ok:
        st.error("❌ Нет подключения к Jira. Проверьте:")
        st.markdown("""
        1. Правильность **URL Jira**
        2. **Логин и пароль**
        3. Доступность сервера Jira из вашего компьютера
        """)
        st.stop()

    if not projects:
        st.warning("⚠️ В Jira не найдено ни одного проекта")
        st.stop()

    if not selected_projects:
        st.warning("⚠️ Выберите хотя бы один проект в боковой панели")
        st.stop()

    # ═══════════ ТЕПЕРЬ БЕЗОПАСНО СОЗДАЁМ ВКЛАДКИ ═══════════
    auth = (login, password)
    tabs = st.tabs(selected_projects)

    if "all_results" not in st.session_state:
        st.session_state.all_results = {}

    for tab, project_key in zip(tabs, selected_projects):
        with tab:
            st.subheader(f"Проект: {project_key}")

            col1, col2 = st.columns(2)
            with col1:
                year = st.number_input("Год", value=datetime.now().year,
                                       min_value=2020, max_value=2030,
                                       key=f"year_{project_key}")
            with col2:
                month = st.number_input("Месяц", value=datetime.now().month,
                                        min_value=1, max_value=12,
                                        key=f"month_{project_key}")

            if st.button(f"🚀 Загрузить задачи {project_key}", key=f"load_{project_key}"):
                with st.spinner("Загрузка..."):
                    issues = get_all_issues(jira_url, auth, project_key)
                if not issues:
                    st.warning("Задачи не найдены")
                    continue
                st.session_state[f"issues_{project_key}"] = issues
                st.success(f"✅ Загружено {len(issues)} задач")
                st.rerun()

            if f"issues_{project_key}" in st.session_state:
                issues = st.session_state[f"issues_{project_key}"]

                all_versions = set()
                for issue in issues:
                    for v in (issue["fields"].get("fixVersions") or []):
                        all_versions.add(v["name"])

                saved_budgets = config.get(f"budgets_{project_key}", {})
                release_budgets = {}
                if all_versions:
                    st.write("**ТРЗ по договору (часов) для каждого релиза:**")
                    cols = st.columns(min(len(all_versions), 4))
                    for idx, ver in enumerate(sorted(all_versions)):
                        with cols[idx % 4]:
                            budget = st.number_input(
                                f"📦 {ver}",
                                min_value=0.0,
                                value=float(saved_budgets.get(ver, 0)),
                                step=1.0,
                                key=f"budget_{project_key}_{ver}"
                            )
                            release_budgets[ver] = budget

                team = get_team_members(issues)
                saved_plans = config.get(f"plans_{project_key}", {})
                planned_hours = {}
                if team:
                    st.write("**План часов на месяц:**")
                    cols = st.columns(min(len(team), 4))
                    for idx, member in enumerate(team):
                        with cols[idx % 4]:
                            plan = st.number_input(
                                f"👤 {member}",
                                min_value=0.0,
                                value=float(saved_plans.get(member, 0)),
                                step=1.0,
                                key=f"plan_{project_key}_{member}"
                            )
                            planned_hours[member] = plan

                if st.button(f"🚀 Запустить проверки {project_key}", key=f"run_{project_key}"):
                    config[f"budgets_{project_key}"] = release_budgets
                    config[f"plans_{project_key}"] = planned_hours
                    save_config(config)

                    results = {}

                    with st.spinner("1/6 Зона выполнения..."):
                        results["execution_zone"] = check_execution_zone(jira_url, issues)

                    progress_bar = st.progress(0)
                    def update_progress(p):
                        progress_bar.progress(p)
                    with st.spinner("2/6 Статусы..."):
                        results["status_correctness"] = check_status_correctness(
                            jira_url, auth, issues, update_progress
                        )
                    progress_bar.empty()

                    with st.spinner("3/6 Релизы..."):
                        results["releases"] = check_releases(issues, release_budgets)

                    with st.spinner("4/6 Ошибки..."):
                        df_bugs, df_stats, unplanned = check_bugs(jira_url, issues)
                        results["bugs"] = df_bugs
                        results["bug_stats"] = df_stats
                        results["unplanned_info"] = unplanned

                    with st.spinner("5/6 Часы за месяц..."):
                        wl_data = get_all_worklogs_for_issues(jira_url, auth, issues, year, month)
                        results["monthly_hours"] = check_monthly_hours(issues, wl_data, planned_hours)

                    with st.spinner("6/6 Незануленные..."):
                        results["not_zeroed"] = check_not_zeroed(issues)

                    st.session_state.all_results[project_key] = results
                    st.success(f"✅ Проверки для {project_key} завершены!")
                    st.rerun()

            if project_key in st.session_state.all_results:
                data = st.session_state.all_results[project_key]

                t1, t2, t3, t4, t5, t6 = st.tabs([
                    "1️⃣ Зона", "2️⃣ Статусы", "3️⃣ Релизы",
                    "4️⃣ Ошибки", "5️⃣ Часы", "6️⃣ Незануленные"
                ])

                with t1:
                    st.write("Задачи с фактом ≥ 60% от оценки:")
                    st.dataframe(data["execution_zone"], use_container_width=True)

                with t2:
                    st.write("Подзадачи, превысившие лимит в статусе:")
                    st.dataframe(data["status_correctness"], use_container_width=True)

                with t3:
                    st.write("ТРЗ по договору и факт по релизам:")
                    st.dataframe(data["releases"], use_container_width=True)

                with t4:
                    st.write("Открытые подзадачи «Ошибка (подзадача)»:")
                    st.dataframe(data["bugs"], use_container_width=True)
                    st.write("**Трудозатраты на Ошибки по релизам:**")
                    st.dataframe(data["bug_stats"], use_container_width=True)
                    st.info(data["unplanned_info"])

                with t5:
                    st.write(f"Часы за {month}/{year}:")
                    st.dataframe(data["monthly_hours"], use_container_width=True)

                with t6:
                    st.write("Подзадачи «Закрыта» с ненулевым остатком:")
                    st.dataframe(data["not_zeroed"], use_container_width=True)

    if st.session_state.all_results:
        st.divider()
        st.subheader("📤 Экспорт")
        excel_data = export_to_excel(st.session_state.all_results)
        st.download_button(
            "📥 Скачать Excel",
            data=excel_data,
            file_name=f"monitoring_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )


if __name__ == "__main__":
    main()