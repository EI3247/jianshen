#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""健身记录 - 零依赖单文件服务
功能: 按日期记训练(动作手写,逐组记重量x次数) / 训练休息打卡 /
      身体数据独立记录(体重体脂胸臂腰腿) / 趋势曲线 / 多成员共用
"""
import os, re, sys, json, hmac, hashlib, sqlite3, threading, time, socket, secrets, string
import urllib.request, urllib.error, calendar as calmod
from datetime import date as dtdate, timedelta
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, quote

# 密码优先用环境变量指定; 不指定则首次启动随机生成一个, 打印在启动日志里。
# 仓库里因此不含任何具体密码。
PASSWORD = os.environ.get("ACCESS_PASSWORD", "").strip()
SUPER_PASSWORD = os.environ.get("SUPER_PASSWORD", "").strip()


def _gen_password():
    """随机密码(首次启动没有环境变量时用)"""
    return "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(10))


PORT = int(os.environ.get("PORT", "8091"))
DATA_DIR = os.environ.get("DATA_DIR", "/data")
DB_PATH = os.path.join(DATA_DIR, "jianshen.db")
COOKIE_NAME = "js_auth"
COOKIE_MAX_AGE = 90 * 24 * 3600
SALT = "jianshen-v1"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INDEX_PATH = os.path.join(BASE_DIR, "index.html")

DB_LOCK = threading.Lock()
_db = None


def db():
    global _db
    if _db is None:
        os.makedirs(DATA_DIR, exist_ok=True)
        _db = sqlite3.connect(DB_PATH, check_same_thread=False)
        _db.row_factory = sqlite3.Row
        _db.execute("PRAGMA journal_mode=WAL")
        _db.executescript("""
        CREATE TABLE IF NOT EXISTS members(id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL, created_at TEXT DEFAULT (date('now','localtime')),
            top_text TEXT DEFAULT '');
        CREATE TABLE IF NOT EXISTS days(member_id INTEGER NOT NULL, date TEXT NOT NULL,
            type TEXT DEFAULT 'training', note TEXT DEFAULT '', plan_idx INTEGER,
            feel TEXT DEFAULT '',
            PRIMARY KEY(member_id, date));
        CREATE TABLE IF NOT EXISTS exercises(id INTEGER PRIMARY KEY AUTOINCREMENT,
            member_id INTEGER NOT NULL, date TEXT NOT NULL, name TEXT NOT NULL, ord INTEGER DEFAULT 0,
            reps_target TEXT DEFAULT '');
        CREATE INDEX IF NOT EXISTS idx_ex_date ON exercises(member_id, date);
        CREATE TABLE IF NOT EXISTS sets(id INTEGER PRIMARY KEY AUTOINCREMENT,
            exercise_id INTEGER NOT NULL, set_no INTEGER NOT NULL, weight REAL, reps REAL,
            name TEXT DEFAULT '');
        CREATE INDEX IF NOT EXISTS idx_set_ex ON sets(exercise_id);
        CREATE TABLE IF NOT EXISTS body(member_id INTEGER NOT NULL, date TEXT NOT NULL,
            weight REAL, bodyfat REAL, chest REAL, shoulder REAL, arm REAL, waist REAL, leg REAL,
            intake REAL, protein REAL,
            PRIMARY KEY(member_id, date));
        CREATE TABLE IF NOT EXISTS intake(member_id INTEGER NOT NULL, date TEXT NOT NULL,
            kcal REAL, protein REAL,
            PRIMARY KEY(member_id, date));
        CREATE TABLE IF NOT EXISTS intake_log(id INTEGER PRIMARY KEY AUTOINCREMENT,
            member_id INTEGER NOT NULL, date TEXT NOT NULL,
            kcal REAL, protein REAL, note TEXT DEFAULT '', ts INTEGER DEFAULT 0);
        CREATE INDEX IF NOT EXISTS idx_ilog ON intake_log(member_id, date);
        CREATE TABLE IF NOT EXISTS intake_tpl(id INTEGER PRIMARY KEY AUTOINCREMENT,
            member_id INTEGER NOT NULL, name TEXT DEFAULT '',
            kcal REAL, protein REAL, ord INTEGER DEFAULT 0);
        CREATE INDEX IF NOT EXISTS idx_tpl ON intake_tpl(member_id, ord);
        CREATE TABLE IF NOT EXISTS activity_log(member_id INTEGER NOT NULL, from_date TEXT NOT NULL,
            activity REAL NOT NULL,
            PRIMARY KEY(member_id, from_date));
        CREATE TABLE IF NOT EXISTS plans(member_id INTEGER PRIMARY KEY,
            cycle_len INTEGER DEFAULT 4, start_date TEXT DEFAULT (date('now','localtime')));
        CREATE TABLE IF NOT EXISTS plan_days(member_id INTEGER NOT NULL, idx INTEGER NOT NULL,
            label TEXT DEFAULT '', type TEXT DEFAULT 'training',
            PRIMARY KEY(member_id, idx));
        CREATE TABLE IF NOT EXISTS plan_exercises(member_id INTEGER NOT NULL, plan_idx INTEGER NOT NULL,
            part TEXT NOT NULL, act TEXT NOT NULL DEFAULT '',
            ord INTEGER DEFAULT 0, aord INTEGER DEFAULT 0,
            sets_n INTEGER DEFAULT 3, reps TEXT DEFAULT '',
            PRIMARY KEY(member_id, plan_idx, part, act));
        CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT DEFAULT '');
        CREATE TABLE IF NOT EXISTS sessions(member_id INTEGER NOT NULL, date TEXT NOT NULL,
            start_ts INTEGER DEFAULT 0, end_ts INTEGER DEFAULT 0,
            PRIMARY KEY(member_id, date));
        CREATE TABLE IF NOT EXISTS skips(member_id INTEGER NOT NULL, date TEXT NOT NULL,
            PRIMARY KEY(member_id, date));
        """)
        try:
            _db.execute("ALTER TABLE days ADD COLUMN plan_idx INTEGER")
        except sqlite3.OperationalError:
            pass
        try:
            _db.execute("ALTER TABLE days ADD COLUMN adj INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        for stmt in ("ALTER TABLE exercises ADD COLUMN reps_target TEXT DEFAULT ''",
                     "ALTER TABLE plan_exercises ADD COLUMN sets_n INTEGER DEFAULT 3",
                     "ALTER TABLE plan_exercises ADD COLUMN reps TEXT DEFAULT ''",
                     "ALTER TABLE plan_exercises ADD COLUMN act TEXT DEFAULT ''",
                     "ALTER TABLE plan_exercises ADD COLUMN kind TEXT DEFAULT 'strength'",
                     "ALTER TABLE plan_exercises ADD COLUMN mins REAL",
                     "ALTER TABLE plan_exercises ADD COLUMN hr REAL",
                     "ALTER TABLE sets ADD COLUMN mins REAL",
                     "ALTER TABLE sets ADD COLUMN hr REAL",
                     "ALTER TABLE members ADD COLUMN top_text TEXT DEFAULT ''",
                     "ALTER TABLE days ADD COLUMN feel TEXT DEFAULT ''",
                     "ALTER TABLE sets ADD COLUMN name TEXT DEFAULT ''",
                     "ALTER TABLE body ADD COLUMN intake REAL",
                     "ALTER TABLE body ADD COLUMN protein REAL",
                     "ALTER TABLE body ADD COLUMN shoulder REAL",
                     "ALTER TABLE members ADD COLUMN activity REAL DEFAULT 1.375",
                     "ALTER TABLE members ADD COLUMN height REAL",
                     "ALTER TABLE members ADD COLUMN birth TEXT DEFAULT ''",
                     "ALTER TABLE members ADD COLUMN sex TEXT DEFAULT ''",
                     "ALTER TABLE members ADD COLUMN prot_train REAL DEFAULT 2.0",
                     "ALTER TABLE members ADD COLUMN prot_rest REAL DEFAULT 1.6",
                     "ALTER TABLE members ADD COLUMN kcal_off_train REAL DEFAULT 0",
                     "ALTER TABLE members ADD COLUMN kcal_off_rest REAL DEFAULT 0",
                     "ALTER TABLE members ADD COLUMN bmr_custom REAL"):
            try:
                _db.execute(stmt)
            except sqlite3.OperationalError:
                pass
        # 摄入从 body 表拆到独立的 intake 表(幂等, 老数据自动搬一次)
        try:
            _db.execute("""INSERT OR REPLACE INTO intake(member_id,date,kcal,protein)
                SELECT member_id,date,intake,protein FROM body
                WHERE intake IS NOT NULL OR protein IS NOT NULL""")
        except sqlite3.OperationalError:
            pass
        # 活动系数改成按生效日期记(幂等): 老的单值当成最早生效
        try:
            _db.execute("""INSERT OR REPLACE INTO activity_log(member_id,from_date,activity)
                SELECT id, '0001-01-01', COALESCE(activity,1.375) FROM members""")
        except sqlite3.OperationalError:
            pass
        if _db.execute("SELECT COUNT(*) c FROM members").fetchone()["c"] == 0:
            _db.execute("INSERT INTO members(name) VALUES('我')")
        for k, v in (("access_password", PASSWORD), ("super_password", SUPER_PASSWORD)):
            if _db.execute("SELECT COUNT(*) c FROM settings WHERE k=?", (k,)).fetchone()["c"] == 0:
                _db.execute("INSERT INTO settings(k,v) VALUES(?,?)", (k, v or _gen_password()))
        _db.commit()
        _migrate_v2()
        _migrate_v3()
        _migrate_v4()
        _cleanup_orphans()
        try:                      # 把 WAL 并回主库, 免得 -wal 一直涨(备份 .db 单文件才安全)
            _db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.OperationalError:
            pass
    return _db


def _cleanup_orphans():
    """清掉"成员已被删、数据还留在表里"的孤儿行(早期版本删成员时漏清过几张表)。

    幂等, 每次启动跑一遍; 数据量小, 开销可忽略。
    """
    d = db()
    for t in ("activity_log", "body", "days", "exercises", "intake", "intake_log",
              "intake_tpl", "plan_days", "plan_exercises", "plans", "sessions", "skips"):
        try:
            d.execute(f"DELETE FROM {t} WHERE member_id NOT IN (SELECT id FROM members)")
        except sqlite3.OperationalError:
            pass
    try:
        d.execute("DELETE FROM sets WHERE exercise_id NOT IN (SELECT id FROM exercises)")
    except sqlite3.OperationalError:
        pass
    d.commit()


def _migrate_v2():
    """v1 -> v2 一次性数据迁移

    旧逻辑: skips 表里的日期"不占位", 后面整体顺延, 日历一直在漂.
    新逻辑: 计划从开始日严格循环(训练日/休息日都占位);
            人工调整记在 days.adj: 训练日改休息=+1(当天计划顺延一天),
            休息日改锻炼=-1(整体提前一天).
    """
    if db().execute("SELECT COUNT(*) c FROM settings WHERE k='migrated_v2'").fetchone()["c"]:
        return
    rows = db().execute("SELECT member_id,date FROM skips").fetchall()
    for r in rows:
        mid, ds = r["member_id"], r["date"]
        base = base_slot(mid, ds)
        adj = 0
        if base is not None:
            pd = db().execute("SELECT type FROM plan_days WHERE member_id=? AND idx=?",
                              (mid, base)).fetchone()
            # 那天计划本来就是休息日 -> 只是打个休息卡, 不产生位移
            if pd is None or pd["type"] != "rest":
                adj = 1
        db().execute("""INSERT INTO days(member_id,date,type,note,plan_idx,adj)
                        VALUES(?,?,'rest','',?,?)
                        ON CONFLICT(member_id,date) DO UPDATE SET type='rest', adj=excluded.adj""",
                     (mid, ds, base, adj))
    db().execute("DELETE FROM skips")
    db().execute("INSERT OR REPLACE INTO settings(k,v) VALUES('migrated_v2','1')")
    db().commit()


def _migrate_v3():
    """plan_exercises: 一行=部位+一个动作模板 -> 一行=部位+动作(一个部位可挂多个动作)

    旧结构: name(部位) / act(动作模板) / ord
    新结构: part(部位) / act(动作) / ord(部位序) / aord(动作在部位内序)
    """
    if db().execute("SELECT COUNT(*) c FROM settings WHERE k='migrated_v3'").fetchone()["c"]:
        return
    have_old = False
    try:
        db().execute("ALTER TABLE plan_exercises RENAME TO plan_exercises_v2")
        have_old = True
    except sqlite3.OperationalError:
        pass
    db().execute("""CREATE TABLE IF NOT EXISTS plan_exercises(
        member_id INTEGER NOT NULL, plan_idx INTEGER NOT NULL,
        part TEXT NOT NULL, act TEXT NOT NULL DEFAULT '',
        ord INTEGER DEFAULT 0, aord INTEGER DEFAULT 0,
        sets_n INTEGER DEFAULT 3, reps TEXT DEFAULT '',
        PRIMARY KEY(member_id, plan_idx, part, act))""")
    if have_old:
        try:
            db().execute("""INSERT OR REPLACE INTO plan_exercises
                (member_id,plan_idx,part,act,ord,aord,sets_n,reps)
                SELECT member_id,plan_idx,name,COALESCE(act,''),COALESCE(ord,0),0,
                       COALESCE(sets_n,3),COALESCE(reps,'')
                FROM plan_exercises_v2""")
        except sqlite3.OperationalError:
            pass
        db().execute("DROP TABLE plan_exercises_v2")
    db().execute("INSERT OR REPLACE INTO settings(k,v) VALUES('migrated_v3','1')")
    db().commit()


def _migrate_v4():
    """摄入: 一天一行(intake, 覆盖式) -> 一笔记一行(intake_log, 按次叠加)

    老表的每个日期搬成一笔(ts=0), 之后所有读写都走 intake_log。
    """
    if db().execute("SELECT COUNT(*) c FROM settings WHERE k='migrated_v4'").fetchone()["c"]:
        return
    rows = db().execute("""SELECT member_id,date,kcal,protein FROM intake
                           WHERE kcal IS NOT NULL OR protein IS NOT NULL""").fetchall()
    for r in rows:
        db().execute("""INSERT INTO intake_log(member_id,date,kcal,protein,note,ts)
                        VALUES(?,?,?,?,'',0)""",
                     (r["member_id"], r["date"], r["kcal"], r["protein"]))
    db().execute("INSERT OR REPLACE INTO settings(k,v) VALUES('migrated_v4','1')")
    db().commit()


def is_train_day(mid, ds):
    """这天算训练日吗 —— 只看实际记录, 不看计划排期

    用户 2026-09-19 明确要求: 计划排了训练但没真的练(改休息/跳过/按错),
    当天就该按休息日算。所以只有两种算训练日:
      ① days 表有记录且 type='training'
      ② 有训练 session(开始过计时)
    其余(包括"计划里排了但没任何记录")一律休息日。
    """
    ds = str(ds)[:10]
    d = q("SELECT type FROM days WHERE member_id=? AND date=?", (mid, ds), one=True)
    if d:
        return d["type"] == "training"
    s = q("SELECT start_ts FROM sessions WHERE member_id=? AND date=?", (mid, ds), one=True)
    return bool(s and (s.get("start_ts") or 0))


# ---------- 计划槽位 ----------
def plan_info(mid):
    """(计划起始日, 周期天数)"""
    p = q("SELECT cycle_len,start_date FROM plans WHERE member_id=?", (mid,), one=True)
    if not p:
        return None
    try:
        sd = dtdate.fromisoformat(str(p["start_date"])[:10])
        cl = max(1, min(14, int(p["cycle_len"])))
    except (ValueError, TypeError):
        return None
    return sd, cl


def base_slot(mid, date):
    """严格从开始日循环的槽位(不含人工位移)"""
    pi = plan_info(mid)
    if not pi:
        return None
    sd, cl = pi
    try:
        dd = dtdate.fromisoformat(str(date)[:10])
    except (ValueError, TypeError):
        return None
    if dd < sd:
        return None
    return (dd.toordinal() - sd.toordinal()) % cl


def offset_before(mid, date):
    """这天之前累计的人工位移(+1 顺延 / -1 提前)"""
    r = q("SELECT COALESCE(SUM(adj),0) s FROM days WHERE member_id=? AND date<?",
          (mid, str(date)[:10]), one=True)
    return int((r or {}).get("s") or 0)


def slot_at(mid, date):
    """这天实际该练的槽位 = 严格槽位 - 累计位移"""
    b = base_slot(mid, date)
    if b is None:
        return None
    cl = plan_info(mid)[1]
    return (b - offset_before(mid, date)) % cl


def act_rest(mid, date):
    """把某天记为休息(记完不能删)
    - 计划本来就是休息日 -> 记休息, 计划不动
    - 计划是训练日(有事练不了) -> 当天计划顺延到第二天(adj=+1)
    """
    base = slot_at(mid, date)
    pd = q("SELECT type,label FROM plan_days WHERE member_id=? AND idx=?",
           (mid, base), one=True) if base is not None else None
    old = q("SELECT adj FROM days WHERE member_id=? AND date=?", (mid, date), one=True)
    adj = 0 if (base is None or (pd and pd["type"] == "rest")) else 1
    if old is not None and int(old.get("adj") or 0) == -1:
        adj = 0      # 撤销之前的"提前"
    q("DELETE FROM sets WHERE exercise_id IN (SELECT id FROM exercises WHERE member_id=? AND date=?)",
      (mid, date), commit=True)
    q("DELETE FROM exercises WHERE member_id=? AND date=?", (mid, date), commit=True)
    q("DELETE FROM sessions WHERE member_id=? AND date=?", (mid, date), commit=True)
    q("""INSERT INTO days(member_id,date,type,note,plan_idx,adj) VALUES(?,?,'rest','',?,?)
         ON CONFLICT(member_id,date) DO UPDATE SET type='rest', note='',
         plan_idx=excluded.plan_idx, adj=excluded.adj""",
      (mid, date, base, adj), commit=True)
    return {"type": "rest", "plan_idx": base, "adj": adj,
            "label": (pd or {}).get("label") or ""}, None


def last_sets(mid, name, before_date):
    """同一动作在这天之前最近一次的记录 -> (日期, [{weight,reps,set_no}...])"""
    r = q("""SELECT e.id, e.date FROM exercises e
             WHERE e.member_id=? AND e.name=? AND e.date<?
             ORDER BY e.date DESC, e.id DESC LIMIT 1""", (mid, name, before_date), one=True)
    if not r:
        return None, []
    return r["date"], q("SELECT set_no,weight,reps FROM sets WHERE exercise_id=? ORDER BY set_no,id",
                        (r["id"],))


def last_act_sets(mid, part, act, before_date):
    """同一 部位+动作 在这天之前最近一次的组 -> (日期, [{set_no,weight,reps}...])"""
    act = act or ""
    r = q("""SELECT e.id, e.date FROM exercises e
             WHERE e.member_id=? AND e.name=? AND e.date<?
               AND EXISTS(SELECT 1 FROM sets s WHERE s.exercise_id=e.id
                          AND COALESCE(s.name,'')=?)
             ORDER BY e.date DESC, e.id DESC LIMIT 1""",
          (mid, part, before_date, act), one=True)
    if not r:
        return None, []
    return r["date"], q("""SELECT set_no,weight,reps FROM sets
                          WHERE exercise_id=? AND COALESCE(name,'')=?
                          ORDER BY set_no,id""", (r["id"], act))


def act_train(mid, date, force_idx=None):
    """把某天记为训练
    - 计划是训练日 -> 直接用当天槽位的计划
    - 计划是休息日(想练) -> 用第二天的计划, 之后整体提前一天(adj=-1)
    - force_idx: 指定槽位, 不产生位移
    """
    pi = plan_info(mid)
    if not pi:
        return None, "还没定计划"
    sd, cl = pi
    adj = 0
    if force_idx is not None:
        try:
            idx = int(force_idx) % cl
        except (TypeError, ValueError):
            return None, "计划序号无效"
    else:
        base = slot_at(mid, date)
        if base is None:
            return None, "这天在计划开始日之前"
        pd = q("SELECT * FROM plan_days WHERE member_id=? AND idx=?", (mid, base), one=True)
        if pd and pd["type"] == "rest":
            idx = None
            for step in range(1, cl + 1):
                j = (base + step) % cl
                pj = q("SELECT * FROM plan_days WHERE member_id=? AND idx=?", (mid, j), one=True)
                if pj and pj["type"] == "training":
                    idx = j
                    break
            adj = -1
        else:
            idx = base
    label = ""
    parts = []          # [(部位, [计划项, ...]), ...] 保持顺序
    if idx is not None:
        pd2 = q("SELECT label FROM plan_days WHERE member_id=? AND idx=?", (mid, idx), one=True)
        label = (pd2 or {}).get("label") or ""
        pexs = q("""SELECT part, COALESCE(act,'') act, COALESCE(kind,'strength') kind,
                           COALESCE(sets_n,3) sets_n, COALESCE(reps,'') reps, mins, hr
                    FROM plan_exercises WHERE member_id=? AND plan_idx=?
                    ORDER BY ord, aord, rowid""", (mid, idx))
        pm = {}
        for pe in pexs:
            if pe["part"] not in pm:
                pm[pe["part"]] = []
                parts.append((pe["part"], pm[pe["part"]]))
            pm[pe["part"]].append(pe)
    # 上次同一 部位+动作 的数据(预填用), 必须在拿 DB_LOCK 之前查
    prevs = {}
    for pname0, acts0 in parts:
        for pe0 in acts0:
            if (pe0.get("kind") or "strength") == "cardio":
                continue            # 有氧的时长/心率由计划预设带出, 不取"上次数据"
            prevs[(pname0, pe0["act"])] = last_act_sets(mid, pname0, pe0["act"], date)
    q("""INSERT INTO days(member_id,date,type,note,plan_idx,adj) VALUES(?,?,'training',?,?,?)
         ON CONFLICT(member_id,date) DO UPDATE SET type='training', note=excluded.note,
         plan_idx=excluded.plan_idx, adj=excluded.adj""",
      (mid, date, label, idx, adj), commit=True)
    mx = q("SELECT COALESCE(MAX(ord),-1) AS m FROM exercises WHERE member_id=? AND date=?",
           (mid, date), one=True)
    base_ord = (mx["m"] or -1) + 1
    added = []
    with DB_LOCK:
        ei = 0
        for pname, acts in parts:
            row = db().execute("SELECT id FROM exercises WHERE member_id=? AND date=? AND name=?",
                               (mid, date, pname)).fetchone()
            if row:
                eid = row["id"]
            else:
                cur = db().execute(
                    "INSERT INTO exercises(member_id,date,name,ord,reps_target) VALUES(?,?,?,?,?)",
                    (mid, date, pname, base_ord + ei, ""))
                eid = cur.lastrowid
                ei += 1
                added.append(pname)
            nxt = db().execute("SELECT COALESCE(MAX(set_no),0) AS m FROM sets WHERE exercise_id=?",
                               (eid,)).fetchone()["m"] or 0
            for pe in acts:
                act = str(pe["act"] or "")[:50]
                dup = db().execute(
                    "SELECT COUNT(*) c FROM sets WHERE exercise_id=? AND COALESCE(name,'')=?",
                    (eid, act)).fetchone()["c"]
                if dup:                     # 这个动作今天已经有了, 不重复生成
                    continue
                if (pe.get("kind") or "strength") == "cardio":
                    # 有氧: 一条记录, 只用时长/心率
                    nxt += 1
                    db().execute("""INSERT INTO sets(exercise_id,set_no,weight,reps,name,mins,hr)
                                    VALUES(?,?,NULL,NULL,?,?,?)""",
                                 (eid, nxt, act, pe.get("mins"), pe.get("hr")))
                    continue
                try:
                    nsets = max(1, min(10, int(pe["sets_n"] or 3)))
                except (TypeError, ValueError):
                    nsets = 3
                _, prev = prevs.get((pname, act), (None, []))
                for k in range(1, nsets + 1):
                    w = rp = None
                    if k - 1 < len(prev):   # 自动带上次的重量/次数
                        w, rp = prev[k - 1]["weight"], prev[k - 1]["reps"]
                    nxt += 1
                    db().execute(
                        "INSERT INTO sets(exercise_id,set_no,weight,reps,name) VALUES(?,?,?,?,?)",
                        (eid, nxt, w, rp, act))
        db().commit()
    return {"type": "training", "plan_idx": idx, "label": label, "adj": adj,
            "added": added}, None


def q(sql, args=(), one=False, commit=False):
    with DB_LOCK:
        c = db().execute(sql, args)
        if commit:
            db().commit()
        rows = c.fetchall()
    if one:
        return dict(rows[0]) if rows else None
    return [dict(r) for r in rows]


def make_token(pwd):
    return hmac.new(SALT.encode(), pwd.encode(), hashlib.sha256).hexdigest()


def get_setting(k, default=""):
    r = q("SELECT v FROM settings WHERE k=?", (k,), one=True)
    return r["v"] if r else default


def check_auth(cookie):
    m = re.search(rf"{COOKIE_NAME}=([^;]+)", cookie or "")
    if not m:
        return False
    cur = get_setting("access_password", PASSWORD)
    return hmac.compare_digest(m.group(1), make_token(cur))


def check_super(pwd):
    cur = get_setting("super_password", SUPER_PASSWORD)
    return bool(pwd) and hmac.compare_digest(str(pwd), cur)


def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------- AI 问答(OpenAI 兼容接口, 零依赖) ----------
AI_TIMEOUT = 30

AI_FOOD_PROMPT = """你是营养数据助手。用户会描述他吃或喝的东西，可能是口语、简称，也可能是一顿饭里的好几样。
请估算这些食物合计的 热量(kcal) 和 蛋白质(g)。
只输出一个 JSON 对象，不要任何解释、不要 markdown 代码块：
{"kcal": 数字, "protein": 数字, "note": "简短名称(不超过12个字)"}
如果用户描述的东西完全不是食物或饮料，输出 {"error": "简短原因"}。
数字只写阿拉伯数字，不要带单位。"""


def ai_get(k):
    return str(get_setting(k, "") or "").strip()


def ai_ready():
    """接口地址 / key / 模型名 三个都配了才算可用"""
    return bool(ai_get("ai_base") and ai_get("ai_key") and ai_get("ai_model"))


def ai_chat(messages, timeout=AI_TIMEOUT):
    """调用 OpenAI 兼容的 /chat/completions, 返回 (文本, 错误信息)"""
    base, key, model = ai_get("ai_base"), ai_get("ai_key"), ai_get("ai_model")
    if not base:
        return None, "还没配置 AI 接口地址"
    if not key:
        return None, "还没配置 API Key"
    if not model:
        return None, "还没配置模型名"
    base = base.rstrip("/")
    if not re.search(r"/v\d+(/|$)", base):     # 用户只填了域名时自动补 /v1
        base += "/v1"
    payload = json.dumps({"model": model, "messages": messages,
                          "temperature": 0.2, "stream": False}).encode("utf-8")
    req = urllib.request.Request(base + "/chat/completions", data=payload, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer " + key})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:200]
        return None, f"接口返回 {e.code}：{body}"
    except urllib.error.URLError as e:
        return None, f"连不上接口：{str(e.reason)[:80]}"
    except (TimeoutError, socket.timeout):
        return None, "接口超时了"
    except Exception as e:
        return None, f"调用失败：{type(e).__name__}"
    try:
        return str(data["choices"][0]["message"]["content"]), None
    except (KeyError, IndexError, TypeError):
        return None, "接口返回的格式不认识"


def _extract_json(s):
    """从模型输出里抠出第一个能解析的 JSON 对象"""
    s = str(s or "").strip()
    cands = [s] + re.findall(r"\{[^{}]*\}", s, re.S)
    for c in cands:
        try:
            v = json.loads(c)
            if isinstance(v, dict):
                return v
        except ValueError:
            continue
    return None


# ---------- 热量收支 ----------
MET_STRENGTH = 5.0          # 力量训练 MET(含组间休息); 额外消耗按 (MET-1)×体重×时长 算
BMR_FALLBACK_PER_KG = 22.0  # 缺体脂率时按体重粗估
# 有氧: 项目名 -> MET(用户选方案A: 按项目固定系数, 心率只记录不参与计算)
CARDIO_MET = (("动感单车", 8.5), ("跑步", 8.0), ("慢跑", 8.0), ("快走", 4.3),
              ("椭圆", 7.0), ("划船", 8.5), ("跳绳", 11.0), ("游泳", 7.0),
              ("爬楼", 9.0), ("登山", 8.0), ("有氧操", 6.5), ("单车", 6.8), ("hiit", 9.0))
CARDIO_MET_DEFAULT = 6.0


def cardio_met(name):
    """按有氧项目名匹配 MET(子串匹配, 长的关键词在前), 匹配不到用默认"""
    s = str(name or "").strip().lower()
    for k, v in CARDIO_MET:
        if k in s:
            return v
    return CARDIO_MET_DEFAULT
ACTIVITY_CHOICES = (("1.2", "久坐"), ("1.375", "轻度"), ("1.55", "中度"), ("1.725", "较高"))


def energy_rows(mid):
    """每天的 基础代谢 / 训练消耗 / 总消耗 / 摄入 / 缺口

    BMR 三档:
      1. 有体重+体脂 -> Katch-McArdle: 370 + 21.6×去脂体重(最准)
      2. 没有体脂, 但有身高+出生日期+性别(个人档案) -> Mifflin-St Jeor
      3. 都没有 -> 22×体重 粗估
    体重/体脂取当天, 当天没有就用之前最近一次。
    训练额外消耗 = (MET_STRENGTH - 1) × 体重 × 当天 session 时长(小时)。
    减那个 1 MET 是扣掉训练时段的基础代谢 —— 它已经含在下面的 BMR×活动系数 里,
    按 MET 全值加会重复计入(2026-09-19 用户要求改成净消耗口径)。
    总消耗 = BMR × 当天生效的活动系数 + 训练额外消耗 —— 活动系数按 activity_log 的生效日期取,
    所以改它只影响今天及以后, 不会篡改历史记录。
    """
    sv = {r["date"]: r for r in q("SELECT * FROM sessions WHERE member_id=?", (mid,))}
    # 有氧: sets.mins 有值的那些(有氧一个项目一条, weight/reps 为空)
    cmap = {}
    for r in q("""SELECT e.date d, COALESCE(s.name,'') nm, s.mins m, COALESCE(s.hr,0) hr
                  FROM exercises e JOIN sets s ON s.exercise_id=e.id
                  WHERE e.member_id=? AND s.mins IS NOT NULL AND s.mins>0""", (mid,)):
        cmap.setdefault(r["d"], []).append(r)
    alog = q("SELECT from_date,activity FROM activity_log WHERE member_id=? ORDER BY from_date", (mid,))

    def act_on(ds):
        v = 1.375
        for r in alog:
            if str(r["from_date"]) <= ds:
                v = r["activity"]
            else:
                break
        return float(v or 1.375)

    prof = q("""SELECT COALESCE(height,0) h, COALESCE(birth,'') b, COALESCE(sex,'') s,
                       COALESCE(prot_train,2.0) pt, COALESCE(prot_rest,1.6) pr,
                       COALESCE(kcal_off_train,0) kt, COALESCE(kcal_off_rest,0) kr,
                       bmr_custom bc
                FROM members WHERE id=?""", (mid,), one=True) or {}
    bmap = {r["date"]: r for r in q("SELECT * FROM body WHERE member_id=? ORDER BY date", (mid,))}
    imap = {r["date"]: r for r in q("""SELECT date, SUM(kcal) k, SUM(protein) p, COUNT(*) n
                                       FROM intake_log WHERE member_id=? GROUP BY date""", (mid,))}
    wdays = {r["date"] for r in q("SELECT DISTINCT date FROM days WHERE member_id=?", (mid,))}
    dates = sorted(set(bmap) | set(imap) | set(wdays) | {dtdate.today().isoformat()})
    out = []
    last_w = last_bf = None
    for ds in dates:
        b = bmap.get(ds) or {}
        w, bf = b.get("weight"), b.get("bodyfat")
        if w:
            last_w = w
        if bf is not None:
            last_bf = bf
        uw = w if w else last_w
        ubf = bf if bf is not None else last_bf
        age = None
        if prof.get("b"):
            try:
                age = (dtdate.fromisoformat(ds) - dtdate.fromisoformat(str(prof["b"])[:10])).days // 365
            except (ValueError, TypeError):
                age = None
        if prof.get("bc"):
            bmr, src = float(prof["bc"]), "custom"          # 用户自定义基础代谢, 优先
        elif uw and ubf is not None:
            bmr, src = 370.0 + 21.6 * (uw * (1 - ubf / 100.0)), "katch"
        elif uw and prof.get("h") and age and age > 0 and prof.get("s") in ("m", "f"):
            bmr = 10 * uw + 6.25 * float(prof["h"]) - 5 * age + (5 if prof["s"] == "m" else -161)
            src = "mifflin"
        elif uw:
            bmr, src = BMR_FALLBACK_PER_KG * uw, "rough"
        else:
            bmr, src = 0.0, "none"
        s = sv.get(ds) or {}
        sec = max(0, int(s.get("end_ts") or 0) - int(s.get("start_ts") or 0))
        train = (MET_STRENGTH - 1.0) * (uw or 0) * (sec / 3600.0) if sec > 0 else 0.0
        # 有氧: 每个项目按 (MET-1)×体重×分钟 累加
        ck = 0.0
        cmin = 0.0
        for cr in cmap.get(ds, []):
            mm = float(cr["m"] or 0)
            ck += (cardio_met(cr["nm"]) - 1.0) * (uw or 0) * (mm / 60.0)
            cmin += mm
        activity = act_on(ds)
        tdee = bmr * activity + train + ck
        it = imap.get(ds) or {}
        intake, prot = it.get("k"), it.get("p")
        n = int(it.get("n") or 0)
        tday = is_train_day(mid, ds)
        # 目标: 蛋白 = 体重 × g/kg(训练日/休息日各一档); 热量 = 消耗 + 偏移
        gpkg = float((prof.get("pt") if tday else prof.get("pr")) or 0)
        prot_target = round(uw * gpkg) if (uw and gpkg) else None
        kcal_off = float((prof.get("kt") if tday else prof.get("kr")) or 0)
        kcal_target = round(tdee + kcal_off) if (tdee > 0 or kcal_off) else None
        # 达成状态: 热量 ±10%(至少±100), 蛋白 ±15%(至少±10)
        kcal_state = kcal_diff = None
        if intake is not None and kcal_target:
            kcal_diff = round(float(intake) - kcal_target)
            tol = max(100.0, 0.10 * kcal_target)
            kcal_state = "ok" if abs(kcal_diff) <= tol else ("low" if kcal_diff < 0 else "high")
        prot_state = prot_diff = None
        if prot is not None and prot_target:
            prot_diff = round(float(prot) - prot_target, 1)
            tol_p = max(10.0, 0.15 * prot_target)
            prot_state = "ok" if abs(prot_diff) <= tol_p else ("low" if prot_diff < 0 else "high")
        out.append({
            "date": ds, "is_train": tday,
            "bmr": round(bmr), "bmr_src": src,
            "train_kcal": round(train), "train_sec": sec,
            "cardio_kcal": round(ck), "cardio_min": round(cmin),
            "tdee": round(tdee), "activity": activity,
            "kg": (round(uw, 1) if uw else None),
            "intake": (round(float(intake)) if intake is not None else None),
            "protein": (round(float(prot), 1) if prot is not None else None),
            "n": n,
            "kcal_target": kcal_target, "kcal_off": round(kcal_off), "kcal_diff": kcal_diff,
            "kcal_state": kcal_state,
            "protein_target": prot_target, "g_per_kg": (round(gpkg, 2) if gpkg else None),
            "prot_diff": prot_diff, "prot_state": prot_state,
            "balance": (round(tdee - float(intake)) if intake is not None else None),
        })
    out.reverse()          # 新的在前
    return out[:90]        # 只回最近 90 天(前端最多用 14 天), 免得用久了接口越传越大


def member_dump(mid):
    """把一个成员的全部数据打包成 dict(导出用)"""
    workouts = []
    for d in q("SELECT * FROM days WHERE member_id=? ORDER BY date", (mid,)):
        exs = q("""SELECT name,ord,COALESCE(reps_target,'') reps_target FROM exercises
                   WHERE member_id=? AND date=? ORDER BY ord,id""", (mid, d["date"]))
        for e in exs:
            e["sets"] = q("""SELECT set_no,weight,reps,COALESCE(name,'') name,mins,hr
                             FROM sets WHERE exercise_id IN
                             (SELECT id FROM exercises WHERE member_id=? AND date=? AND name=?)
                             ORDER BY set_no,id""", (mid, d["date"], e["name"]))
        sv = q("SELECT * FROM sessions WHERE member_id=? AND date=?", (mid, d["date"]), one=True) or {}
        st, en = sv.get("start_ts") or 0, sv.get("end_ts") or 0
        workouts.append({"date": d["date"], "type": d["type"], "note": d["note"],
                         "feel": d.get("feel") or "", "plan_idx": d.get("plan_idx"),
                         "adj": d.get("adj") or 0,
                         "start_ts": st, "end_ts": en,
                         "duration_sec": (max(0, en - st) if (st and en) else 0),
                         "exercises": exs})
    mrow = q("SELECT * FROM members WHERE id=?", (mid,), one=True) or {}
    return {"member": {k: mrow.get(k) for k in ("id", "name", "created_at", "top_text",
                                                "activity", "height", "birth", "sex",
                                                "prot_train", "prot_rest",
                                                "kcal_off_train", "kcal_off_rest",
                                                "bmr_custom")},
            "activity_log": q("""SELECT from_date,activity FROM activity_log
                                 WHERE member_id=? ORDER BY from_date""", (mid,)),
            "plan": q("SELECT * FROM plans WHERE member_id=?", (mid,), one=True),
            "plan_days": q("SELECT * FROM plan_days WHERE member_id=? ORDER BY idx", (mid,)),
            "plan_exercises": q("""SELECT * FROM plan_exercises WHERE member_id=?
                                   ORDER BY plan_idx,ord""", (mid,)),
            "workouts": workouts,
            "intake": q("""SELECT id,date,kcal,protein,COALESCE(note,'') note,COALESCE(ts,0) ts
                           FROM intake_log WHERE member_id=? ORDER BY date,id""", (mid,)),
            "intake_tpl": q("""SELECT name,COALESCE(kcal,'') kcal,COALESCE(protein,'') protein
                               FROM intake_tpl WHERE member_id=? ORDER BY ord,id""", (mid,)),
            "body": q("""SELECT date,weight,bodyfat,chest,shoulder,arm,waist,leg
                         FROM body WHERE member_id=? ORDER BY date""", (mid,))}


def _csvq(s):
    """CSV 字段转义"""
    s = "" if s is None else str(s)
    if any(c in s for c in ',"\r\n'):
        return '"' + s.replace('"', '""') + '"'
    return s


class Handler(BaseHTTPRequestHandler):
    server_version = "Jianshen/1.0"
    timeout = 30

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"),
                                           self.address_string(), fmt % args))

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        if extra:
            for k, v in extra.items():
                self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _ok(self, **kw):
        d = {"ok": True}
        d.update(kw)
        self._send(200, json.dumps(d, ensure_ascii=False))

    def _err(self, msg, code=400):
        self._send(code, json.dumps({"ok": False, "error": msg}, ensure_ascii=False))

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
        except ValueError:
            n = 0
        if n <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return {}

    def _authed(self):
        m = re.search(rf"{COOKIE_NAME}=([^;]+)", self.headers.get("Cookie", ""))
        return check_auth(m.group(0) if m else "")

    # ---------- GET ----------
    def do_GET(self):
        u = urlparse(self.path)
        path = u.path
        if path == "/":
            if self._authed():
                try:
                    with open(INDEX_PATH, encoding="utf-8") as f:
                        self._send(200, f.read().encode("utf-8"), "text/html; charset=utf-8")
                except OSError:
                    self._send(500, b"index.html missing")
            else:
                self._send(200, LOGIN_PAGE.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/api/logout":
            self._send(200, b'{"ok":true}', extra={
                "Set-Cookie": f"{COOKIE_NAME}=; Max-Age=0; Path=/; HttpOnly"})
            return
        # ---- PWA: 免登录 ----
        if path in ("/manifest.webmanifest", "/manifest.json"):
            mf = {
                "name": "健身记录", "short_name": "健身记录",
                "start_url": "/", "scope": "/", "display": "standalone",
                "background_color": "#f2f3f7", "theme_color": "#17202e",
                "icons": [{"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
                          {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png",
                           "purpose": "any maskable"}],
            }
            self._send(200, json.dumps(mf, ensure_ascii=False).encode("utf-8"),
                       "application/manifest+json; charset=utf-8")
            return
        if path in ("/icon-192.png", "/icon-512.png", "/apple-touch-icon.png", "/favicon.ico"):
            fn = "icon-512.png" if path == "/icon-512.png" else "icon-192.png"
            try:
                with open(os.path.join(BASE_DIR, fn), "rb") as f:
                    self._send(200, f.read(), "image/png")
            except OSError:
                self._err("图标缺失", 404)
            return
        if not self._authed():
            self._err("未登录", 401)
            return
        qs = parse_qs(u.query)
        g = lambda k, d="": (qs.get(k) or [d])[0]

        if path == "/api/members":
            self._ok(members=q("SELECT * FROM members ORDER BY id"))
            return
        if path == "/api/plan":
            mid = g("member_id")
            plan = q("SELECT * FROM plans WHERE member_id=?", (mid,), one=True)
            if not plan:
                plan = {"member_id": int(mid), "cycle_len": 4,
                        "start_date": dtdate.today().isoformat()}
                q("INSERT INTO plans(member_id,cycle_len,start_date) VALUES(?,?,?)",
                  (mid, 4, plan["start_date"]), commit=True)
            days = q("SELECT * FROM plan_days WHERE member_id=? ORDER BY idx", (mid,))
            exs = q("""SELECT plan_idx, part, part AS name, COALESCE(act,'') act,
                              COALESCE(kind,'strength') kind, mins, hr,
                              COALESCE(ord,0) ord, COALESCE(aord,0) aord,
                              COALESCE(sets_n,3) sets_n, COALESCE(reps,'') reps
                       FROM plan_exercises WHERE member_id=? ORDER BY plan_idx,ord,aord,rowid""", (mid,))
            self._ok(plan=plan, days=days, exercises=exs)
            return
        if path == "/api/calendar":
            from datetime import date as _d
            mid = g("member_id")
            try:
                y, m = int(g("year")), int(g("month"))
            except ValueError:
                self._err("参数错误")
                return
            _, last = calmod.monthrange(y, m)
            recs = q("""SELECT date,type,note,plan_idx,COALESCE(adj,0) adj,COALESCE(feel,'') feel
                        FROM days WHERE member_id=?
                        AND date>=? AND date<=?""",
                     (mid, f"{y:04d}-{m:02d}-01", f"{y:04d}-{m:02d}-{last:02d}"))
            pmap = {r["date"]: r for r in recs}
            plan = q("SELECT * FROM plans WHERE member_id=?", (mid,), one=True)
            pds = {r["idx"]: r for r in q("SELECT * FROM plan_days WHERE member_id=?", (mid,))}
            out = []
            try:
                sd = _d.fromisoformat(plan["start_date"]) if plan else None
                cl = max(1, int(plan["cycle_len"])) if plan else 0
            except (ValueError, TypeError):
                sd, cl = None, 0
            # 本月之前的累计人工位移
            pr = q("SELECT COALESCE(SUM(adj),0) s FROM days WHERE member_id=? AND date<?",
                   (mid, f"{y:04d}-{m:02d}-01"), one=True)
            run = int((pr or {}).get("s") or 0)
            # 预填充: 从开始日严格循环, 训练日和休息日都占位(休息日也排进日历)
            for dd in range(1, last + 1):
                ds = f"{y:04d}-{m:02d}-{dd:02d}"
                cell = {"date": ds}
                if ds in pmap:
                    cell["type"] = pmap[ds]["type"]
                    cell["note"] = pmap[ds]["note"]
                    if pmap[ds].get("plan_idx") is not None:
                        cell["plan_idx"] = pmap[ds]["plan_idx"]
                    if pmap[ds].get("adj"):
                        cell["adj"] = pmap[ds]["adj"]
                    if pmap[ds].get("feel"):
                        cell["feel"] = pmap[ds]["feel"]
                elif sd and cl and _d(y, m, dd) >= sd:
                    idx = (_d(y, m, dd).toordinal() - sd.toordinal() - run) % cl
                    pd = pds.get(idx)
                    if pd:
                        cell["plan"] = pd["label"]
                        cell["plan_type"] = pd["type"]
                        cell["plan_idx"] = idx
                run += int((pmap.get(ds) or {}).get("adj") or 0)
                out.append(cell)
            self._ok(cells=out, first_weekday=calmod.monthrange(y, m)[0], last_day=last)
            return
        if path == "/api/days":
            mid = g("member_id")
            rows = q("""SELECT d.*, (SELECT COUNT(*) FROM exercises e
                       WHERE e.member_id=d.member_id AND e.date=d.date) AS ex_count
                       FROM days d WHERE d.member_id=? ORDER BY d.date DESC LIMIT 60""", (mid,))
            self._ok(days=rows)
            return
        if path == "/api/day":
            mid, date = g("member_id"), g("date")
            day = q("SELECT * FROM days WHERE member_id=? AND date=?", (mid, date), one=True)
            exs = q("SELECT * FROM exercises WHERE member_id=? AND date=? ORDER BY ord,id", (mid, date))
            for e in exs:
                e["sets"] = q("SELECT * FROM sets WHERE exercise_id=? ORDER BY set_no,id", (e["id"],))
                seen = []
                for s in e["sets"]:
                    a = s.get("name") or ""
                    if a not in seen:
                        seen.append(a)
                lm = {}
                for a in seen:
                    ld, ls = last_act_sets(mid, e["name"], a, date)
                    if ld:
                        lm[a] = {"date": ld, "sets": ls}
                e["last_by_act"] = lm
            sess = q("SELECT * FROM sessions WHERE member_id=? AND date=?", (mid, date), one=True)
            self._ok(day=day, exercises=exs, session=sess)
            return
        if path == "/api/body":
            mid = g("member_id")
            rows = q("SELECT * FROM body WHERE member_id=? ORDER BY date DESC LIMIT 90", (mid,))
            self._ok(records=rows, energy=energy_rows(mid))
            return
        if path == "/api/intake":
            mid, date = g("member_id"), str(g("date"))[:10]
            rows = q("""SELECT id,date,kcal,protein,COALESCE(note,'') note,COALESCE(ts,0) ts
                        FROM intake_log WHERE member_id=? AND date=? ORDER BY id""", (mid, date))
            self._ok(items=rows, n=len(rows),
                     kcal=round(sum(float(r["kcal"]) for r in rows if r["kcal"] is not None), 1)
                          if any(r["kcal"] is not None for r in rows) else None,
                     protein=round(sum(float(r["protein"]) for r in rows if r["protein"] is not None), 1)
                          if any(r["protein"] is not None for r in rows) else None)
            return
        if path == "/api/tpl":
            mid = g("member_id")
            self._ok(items=q("""SELECT id,name,COALESCE(kcal,'') kcal,COALESCE(protein,'') protein
                                FROM intake_tpl WHERE member_id=? ORDER BY ord,id""", (mid,)))
            return
        if path == "/api/ai/config":
            self._ok(base=ai_get("ai_base"), model=ai_get("ai_model"),
                     has_key=bool(ai_get("ai_key")), ready=ai_ready())
            return
        if path == "/api/exercise_names":
            mid = g("member_id")
            rows = q("SELECT DISTINCT name FROM exercises WHERE member_id=? ORDER BY name", (mid,))
            self._ok(names=[r["name"] for r in rows])
            return
        if path == "/api/act_names":
            mid = g("member_id")
            rows = q("""SELECT DISTINCT COALESCE(s.name,'') name FROM sets s
                        JOIN exercises e ON e.id=s.exercise_id
                        WHERE e.member_id=? AND COALESCE(s.name,'')<>'' ORDER BY name""", (mid,))
            self._ok(names=[r["name"] for r in rows])
            return
        if path == "/api/trend":
            mid, name = g("member_id"), g("name")
            rows = q("""SELECT e.date, MAX(s.weight) AS max_w,
                        SUM(s.weight*s.reps) AS vol, SUM(s.reps) AS reps
                        FROM exercises e JOIN sets s ON s.exercise_id=e.id
                        WHERE e.member_id=? AND COALESCE(s.name,'')=? GROUP BY e.date ORDER BY e.date""",
                     (mid, name))
            self._ok(points=rows)
            return
        if path == "/api/volume":
            mid = g("member_id")
            rows = q("""SELECT e.date, SUM(s.weight*s.reps) AS vol
                        FROM exercises e JOIN sets s ON s.exercise_id=e.id
                        WHERE e.member_id=? GROUP BY e.date ORDER BY e.date DESC LIMIT 30""", (mid,))
            rows.reverse()
            self._ok(points=rows)
            return
        if path == "/api/pr":
            mid = g("member_id")
            rows = q("""SELECT COALESCE(s.name,'') name, e.date date, s.weight w, s.reps r
                        FROM exercises e JOIN sets s ON s.exercise_id=e.id
                        WHERE e.member_id=? AND s.weight IS NOT NULL AND s.weight>0
                          AND COALESCE(s.name,'')<>''
                        ORDER BY name, s.weight DESC, e.date ASC, s.set_no ASC""", (mid,))
            best, e1 = {}, {}
            for r in rows:
                n, v, rp = r["name"], float(r["w"] or 0), float(r["r"] or 0)
                if n not in best:
                    best[n] = {"name": n, "weight": v, "reps": rp, "date": r["date"]}
                rm = v * (1 + rp / 30.0) if rp else v          # Epley 估算 1RM
                if n not in e1 or rm > e1[n]["e1rm"]:
                    e1[n] = {"e1rm": round(rm, 1), "date": r["date"], "weight": v, "reps": rp}
            out = []
            for n, b in best.items():
                b["e1rm"] = e1[n]["e1rm"]
                b["e1rm_date"] = e1[n]["date"]
                b["e1rm_weight"] = e1[n]["weight"]
                b["e1rm_reps"] = e1[n]["reps"]
                out.append(b)
            out.sort(key=lambda x: -x["weight"])
            self._ok(pr=out)
            return
        if path == "/api/stats":
            mid = g("member_id")
            try:
                nw = max(1, min(26, int(g("weeks", "6") or 6)))
            except ValueError:
                nw = 6
            today = dtdate.today()
            monday = today - timedelta(days=today.weekday())
            start = monday - timedelta(days=7 * (nw - 1))
            s0 = start.isoformat()
            vq = q("""SELECT e.date d, COALESCE(SUM(s.weight*s.reps),0) v,
                             COUNT(CASE WHEN s.weight IS NOT NULL OR s.reps IS NOT NULL THEN 1 END) c
                      FROM exercises e JOIN sets s ON s.exercise_id=e.id
                      WHERE e.member_id=? AND e.date>=? GROUP BY e.date""", (mid, s0))
            vmap = {r["d"]: (r["v"] or 0, r["c"] or 0) for r in vq}
            dset = {r["date"] for r in q("SELECT date,type FROM days WHERE member_id=? AND date>=?",
                                         (mid, s0)) if r["type"] == "training"}
            smap = {r["date"]: max(0, (r["end_ts"] or 0) - (r["start_ts"] or 0))
                    for r in q("SELECT date,start_ts,end_ts FROM sessions WHERE member_id=? AND date>=?",
                               (mid, s0))}
            weeks = []
            for k in range(nw):
                w0 = start + timedelta(days=7 * k)
                ds = [(w0 + timedelta(days=i)).isoformat() for i in range(7)]
                weeks.append({
                    "start": w0.isoformat(),
                    "end": (w0 + timedelta(days=6)).isoformat(),
                    "training": sum(1 for d in ds if d in dset),
                    "sets": int(sum(vmap.get(d, (0, 0))[1] for d in ds)),
                    "vol": round(sum(vmap.get(d, (0, 0))[0] for d in ds)),
                    "dur": int(sum(smap.get(d, 0) for d in ds)),
                })
            self._ok(weeks=weeks, this_week=weeks[-1],
                     last_week=weeks[-2] if nw > 1 else None, today=today.isoformat())
            return
        if path == "/api/export":
            mid = g("member_id")
            what = g("what", "training")
            fmt = g("fmt", "csv")
            mem = q("SELECT name FROM members WHERE id=?", (mid,), one=True) or {"name": "member"}
            mname = str(mem["name"])
            stamp = time.strftime("%Y%m%d")
            if fmt == "json":
                stamp2 = {"export_version": 2, "app": "健身记录",
                          "exported_at": time.strftime("%Y-%m-%d %H:%M:%S")}
                if what == "allm":                     # 所有成员
                    data = dict(stamp2, members=[member_dump(m["id"])
                                                 for m in q("SELECT id FROM members ORDER BY id")])
                    fn = f"健身记录_全员_{stamp}.json"
                else:
                    data = dict(stamp2, **member_dump(mid))
                    fn = f"健身记录_{mname}_{stamp}.json"
                body_txt = json.dumps(data, ensure_ascii=False, indent=1)
                ctype = "application/json; charset=utf-8"
            elif what == "intake":
                lines = ["日期,时间,热量kcal,蛋白g,备注"]
                for b in q("""SELECT date,kcal,protein,COALESCE(note,'') note,COALESCE(ts,0) ts
                              FROM intake_log WHERE member_id=? ORDER BY date,id""", (mid,)):
                    hhmm = time.strftime("%H:%M", time.localtime(b["ts"])) if b["ts"] else ""
                    lines.append(",".join(_csvq(x) for x in (
                        b["date"], hhmm,
                        "" if b["kcal"] is None else b["kcal"],
                        "" if b["protein"] is None else b["protein"],
                        b["note"])))
                body_txt = "\ufeff" + "\r\n".join(lines) + "\r\n"
                fn, ctype = f"健身记录_摄入_{mname}_{stamp}.csv", "text/csv; charset=utf-8"
            elif what == "body":
                lines = ["日期,体重kg,体脂%,胸围cm,肩宽cm,臂围cm,腰围cm,腿围cm"]
                for b in q("SELECT * FROM body WHERE member_id=? ORDER BY date", (mid,)):
                    lines.append(",".join(str(b[k]) if b[k] is not None else ""
                                          for k in ("date", "weight", "bodyfat",
                                                    "chest", "shoulder", "arm", "waist", "leg")))
                body_txt = "\ufeff" + "\r\n".join(lines) + "\r\n"
                fn, ctype = f"健身记录_身体_{mname}_{stamp}.csv", "text/csv; charset=utf-8"
            else:
                FEEL_CN = {"good": "很好", "ok": "一般", "tired": "疲惫", "hurt": "不适"}
                lines = ["日期,当天类型,备注,状态,开始,结束,时长分,部位,组号,动作,重量kg,次数,容量kg,有氧分,最高心率"]
                dm = {r["date"]: r for r in q("SELECT * FROM days WHERE member_id=? ORDER BY date", (mid,))}
                sm = {r["date"]: r for r in q("SELECT * FROM sessions WHERE member_id=?", (mid,))}
                rows = q("""SELECT e.date d, e.name nm, s.set_no sn, s.weight w, s.reps r,
                                   COALESCE(s.name,'') snm, s.mins m, s.hr h
                            FROM exercises e JOIN sets s ON s.exercise_id=e.id
                            WHERE e.member_id=? ORDER BY e.date, e.ord, e.id, s.set_no, s.id""", (mid,))
                byday = {}
                for r in rows:
                    byday.setdefault(r["d"], []).append(r)
                for ds in sorted(dm.keys()):           # 含没有动作的日子(休息日/空训练日)
                    d0 = dm[ds]
                    sv = sm.get(ds) or {}
                    st, en = sv.get("start_ts") or 0, sv.get("end_ts") or 0
                    base = [ds, ("训练" if d0["type"] == "training" else
                                 ("休息" if d0["type"] == "rest" else str(d0["type"]))),
                            _csvq(d0.get("note")), FEEL_CN.get((d0.get("feel") or ""), ""),
                            (time.strftime("%H:%M", time.localtime(st)) if st else ""),
                            (time.strftime("%H:%M", time.localtime(en)) if en else ""),
                            (str(round((en - st) / 60)) if (st and en and en > st) else "")]
                    rs = byday.get(ds) or []
                    if not rs:
                        lines.append(",".join(base + ["", "", "", "", "", "", "", ""]))
                    for r in rs:
                        vol = round((r["w"] or 0) * (r["r"] or 0))
                        lines.append(",".join(base + [_csvq(r["nm"]), str(r["sn"]), _csvq(r["snm"]),
                                                      "" if r["w"] is None else str(r["w"]),
                                                      "" if r["r"] is None else str(r["r"]), str(vol),
                                                      "" if r["m"] is None else str(r["m"]),
                                                      "" if r["h"] is None else str(r["h"])]))
                body_txt = "\ufeff" + "\r\n".join(lines) + "\r\n"
                fn, ctype = f"健身记录_训练_{mname}_{stamp}.csv", "text/csv; charset=utf-8"
            self._send(200, body_txt.encode("utf-8"), ctype, extra={
                "Content-Disposition": "attachment; filename=\"jianshen_export\"; "
                                       "filename*=UTF-8''" + quote(fn)})
            return
        self._err("404", 404)

    # ---------- POST ----------
    def do_POST(self):
        u = urlparse(self.path)
        path = u.path
        if path == "/api/login":
            pwd = str(self._body().get("password", ""))
            cur = get_setting("access_password", PASSWORD)
            if pwd == cur:
                self._send(200, b'{"ok":true}', extra={"Set-Cookie":
                    f"{COOKIE_NAME}={make_token(cur)}; Max-Age={COOKIE_MAX_AGE}; Path=/; HttpOnly; SameSite=Lax"})
            else:
                self._err("密码错误", 401)
            return
        if not self._authed():
            self._err("未登录", 401)
            return
        d = self._body()

        if path == "/api/members/add":
            name = str(d.get("name", "")).strip()[:20]
            if not name:
                self._err("名字不能为空")
                return
            try:
                q("INSERT INTO members(name) VALUES(?)", (name,), commit=True)
            except sqlite3.IntegrityError:
                self._err("名字已存在")
                return
            self._ok(members=q("SELECT * FROM members ORDER BY id"))
            return
        if path == "/api/members/delete":
            if not check_super(d.get("super_password")):
                self._err("管理密码错误", 403)
                return
            q("DELETE FROM members WHERE id=?", (d.get("id"),), commit=True)
            mid = d.get("id")
            q("DELETE FROM days WHERE member_id=?", (mid,), commit=True)
            q("DELETE FROM sets WHERE exercise_id IN (SELECT id FROM exercises WHERE member_id=?)",
              (mid,), commit=True)
            q("DELETE FROM exercises WHERE member_id=?", (mid,), commit=True)
            q("DELETE FROM body WHERE member_id=?", (mid,), commit=True)
            q("DELETE FROM plans WHERE member_id=?", (mid,), commit=True)
            q("DELETE FROM plan_days WHERE member_id=?", (mid,), commit=True)
            q("DELETE FROM plan_exercises WHERE member_id=?", (mid,), commit=True)
            q("DELETE FROM sessions WHERE member_id=?", (mid,), commit=True)
            q("DELETE FROM skips WHERE member_id=?", (mid,), commit=True)
            q("DELETE FROM intake_log WHERE member_id=?", (mid,), commit=True)
            q("DELETE FROM intake WHERE member_id=?", (mid,), commit=True)
            q("DELETE FROM intake_tpl WHERE member_id=?", (mid,), commit=True)
            q("DELETE FROM activity_log WHERE member_id=?", (mid,), commit=True)
            self._ok(members=q("SELECT * FROM members ORDER BY id"))
            return
        if path == "/api/members/activity":
            try:
                av = float(d.get("activity", 1.375))
            except (TypeError, ValueError):
                self._err("活动系数必须是数字")
                return
            av = min(2.5, max(1.0, av))
            mid2 = d.get("id")
            fd = str(d.get("from_date") or dtdate.today().isoformat())[:10]
            if not mid2 or not re.match(r"^\d{4}-\d{2}-\d{2}$", fd):
                self._err("参数错误")
                return
            q("""INSERT INTO activity_log(member_id,from_date,activity) VALUES(?,?,?)
                 ON CONFLICT(member_id,from_date) DO UPDATE SET activity=excluded.activity""",
              (mid2, fd, av), commit=True)
            q("UPDATE members SET activity=? WHERE id=?", (av, mid2), commit=True)
            self._ok(activity=av, from_date=fd)
            return
        if path == "/api/members/profile":
            mid2 = d.get("id")
            if not mid2:
                self._err("参数错误")
                return
            hv = d.get("height")
            try:
                hv = float(hv) if hv not in (None, "") else None
            except (TypeError, ValueError):
                self._err("身高必须是数字")
                return
            bv = str(d.get("birth") or "").strip()[:10]
            if bv and not re.match(r"^\d{4}-\d{2}-\d{2}$", bv):
                self._err("出生日期格式不对")
                return
            sv2 = d.get("sex") if d.get("sex") in ("m", "f") else ""
            q("UPDATE members SET height=?, birth=?, sex=? WHERE id=?",
              (hv, bv, sv2, mid2), commit=True)
            self._ok()
            return
        if path == "/api/members/targets":
            mid2 = d.get("id")
            if not mid2:
                self._err("参数错误")
                return
            out = {}
            spec = (("prot_train", 0.5, 4.0, 2.0, "训练日蛋白 g/kg"),
                    ("prot_rest", 0.5, 4.0, 1.6, "休息日蛋白 g/kg"),
                    ("kcal_off_train", -1500, 1500, 0, "训练日热量调整"),
                    ("kcal_off_rest", -1500, 1500, 0, "休息日热量调整"))
            for k, lo, hi, df, cn in spec:
                v = d.get(k)
                if v in (None, ""):
                    v = df
                else:
                    try:
                        v = float(v)
                    except (TypeError, ValueError):
                        self._err(f"{cn}必须是数字")
                        return
                    v = min(hi, max(lo, v))
                out[k] = v
            bm = d.get("bmr_custom")
            if bm in (None, ""):
                bmv = None
            else:
                try:
                    bmv = float(bm)
                except (TypeError, ValueError):
                    self._err("基础代谢必须是数字")
                    return
                bmv = min(6000, max(600, bmv))
            out["bmr_custom"] = bmv
            q("""UPDATE members SET prot_train=?, prot_rest=?, kcal_off_train=?, kcal_off_rest=?,
                 bmr_custom=? WHERE id=?""",
              (out["prot_train"], out["prot_rest"], out["kcal_off_train"], out["kcal_off_rest"],
               bmv, mid2), commit=True)
            self._ok(**out)
            return
        if path == "/api/ai/config":
            if not check_super(d.get("super_password")):
                self._err("管理密码错误", 403)
                return
            for k, sk in (("base", "ai_base"), ("model", "ai_model")):
                if k in d:
                    q("INSERT OR REPLACE INTO settings(k,v) VALUES(?,?)",
                      (sk, str(d.get(k) or "").strip()[:300]), commit=True)
            if d.get("key"):            # 留空 = 不改动已保存的 key
                q("INSERT OR REPLACE INTO settings(k,v) VALUES('ai_key',?)",
                  (str(d["key"]).strip()[:400],), commit=True)
            if d.get("clear_key"):
                q("INSERT OR REPLACE INTO settings(k,v) VALUES('ai_key','')", commit=True)
            self._ok(ready=ai_ready())
            return
        if path == "/api/ai/test":
            txt, err = ai_chat([{"role": "user", "content": "只回复两个字：正常"}], timeout=20)
            if err:
                self._err(err)
                return
            self._ok(reply=str(txt or "").strip()[:60])
            return
        if path == "/api/ai/ask":
            txtq = str(d.get("text") or "").strip()[:200]
            if not txtq:
                self._err("先写要吃的东西")
                return
            out, err = ai_chat([{"role": "system", "content": AI_FOOD_PROMPT},
                                {"role": "user", "content": txtq}])
            if err:
                self._err(err)
                return
            obj = _extract_json(out)
            if not obj:
                self._err("模型没返回能识别的内容，再问一次试试")
                return
            if obj.get("error"):
                self._err(str(obj["error"])[:60])
                return

            def _n(v):
                try:
                    return round(float(v), 1)
                except (TypeError, ValueError):
                    return None
            kc, pr = _n(obj.get("kcal")), _n(obj.get("protein"))
            if kc is None and pr is None:
                self._err("模型没给出热量和蛋白")
                return
            self._ok(kcal=kc, protein=pr, note=str(obj.get("note") or txtq)[:20])
            return
        if path == "/api/members/top_text":
            mid = d.get("id")
            if not mid:
                self._err("参数错误")
                return
            txt = str(d.get("text", "") or "").strip()[:60]
            q("UPDATE members SET top_text=? WHERE id=?", (txt, mid), commit=True)
            self._ok(text=txt)
            return
        if path == "/api/members/rename":
            if not check_super(d.get("super_password")):
                self._err("管理密码错误", 403)
                return
            name = str(d.get("name", "")).strip()[:20]
            if not name:
                self._err("名字不能为空")
                return
            try:
                q("UPDATE members SET name=? WHERE id=?", (name, d.get("id")), commit=True)
            except sqlite3.IntegrityError:
                self._err("名字已存在")
                return
            self._ok(members=q("SELECT * FROM members ORDER BY id"))
            return
        if path == "/api/settings/passwords":
            if not check_super(d.get("super_password")):
                self._err("管理密码错误", 403)
                return
            na = str(d.get("access_password", "")).strip()
            ns = str(d.get("super_new", "")).strip()
            if na:
                q("UPDATE settings SET v=? WHERE k='access_password'", (na,), commit=True)
            if ns:
                q("UPDATE settings SET v=? WHERE k='super_password'", (ns,), commit=True)
            self._ok(changed=bool(na or ns))
            return
        if path == "/api/plan":
            mid = d.get("member_id")
            try:
                cl = int(d.get("cycle_len", 4))
            except (TypeError, ValueError):
                self._err("周期必须是数字")
                return
            cl = min(14, max(1, cl))
            sd = str(d.get("start_date", ""))[:10]
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", sd):
                self._err("起始日期格式错误")
                return
            q("""INSERT INTO plans(member_id,cycle_len,start_date) VALUES(?,?,?)
                 ON CONFLICT(member_id) DO UPDATE SET cycle_len=excluded.cycle_len,
                 start_date=excluded.start_date""", (mid, cl, sd), commit=True)
            items = d.get("days") or []
            for it in items:
                try:
                    idx = int(it.get("idx"))
                except (TypeError, ValueError):
                    continue
                label = str(it.get("label", "")).strip()[:20]
                typ = it.get("type") if it.get("type") in ("training", "rest") else "training"
                if typ == "rest" and not label:
                    label = "休"
                q("""INSERT INTO plan_days(member_id,idx,label,type) VALUES(?,?,?,?)
                     ON CONFLICT(member_id,idx) DO UPDATE SET label=excluded.label, type=excluded.type""",
                  (mid, idx, label, typ), commit=True)
            q("DELETE FROM plan_days WHERE member_id=? AND idx>=?", (mid, cl), commit=True)
            q("DELETE FROM plan_exercises WHERE member_id=?", (mid,), commit=True)
            pexs = d.get("exercises") or []
            for pe in pexs:
                try:
                    pidx = int(pe.get("plan_idx"))
                except (TypeError, ValueError):
                    continue
                if pidx >= cl:
                    continue
                nm = str(pe.get("part", pe.get("name", ""))).strip()[:50]
                if not nm:
                    continue
                try:
                    oo = int(pe.get("ord", 0))
                except (TypeError, ValueError):
                    oo = 0
                try:
                    ao = int(pe.get("aord", 0))
                except (TypeError, ValueError):
                    ao = 0
                try:
                    sn_n = max(1, min(10, int(pe.get("sets_n", 3) or 3)))
                except (TypeError, ValueError):
                    sn_n = 3
                rp = str(pe.get("reps", "") or "").strip()[:20]
                ac = str(pe.get("act", "") or "").strip()[:50]
                kd = "cardio" if pe.get("kind") == "cardio" else "strength"

                def _num(v):
                    try:
                        return float(v) if v not in (None, "") else None
                    except (TypeError, ValueError):
                        return None
                mn, hr = _num(pe.get("mins")), _num(pe.get("hr"))
                q("""INSERT OR REPLACE INTO plan_exercises
                     (member_id,plan_idx,part,act,ord,aord,sets_n,reps,kind,mins,hr)
                     VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                  (mid, pidx, nm, ac, oo, ao, sn_n, rp, kd, mn, hr), commit=True)
            self._ok()
            return
        if path == "/api/plan/copy":
            src, dst = d.get("from_member_id"), d.get("to_member_id")
            try:
                src, dst = int(src), int(dst)
            except (TypeError, ValueError):
                self._err("参数错误")
                return
            if src == dst:
                self._err("不能复制给自己")
                return
            if not q("SELECT id FROM members WHERE id=?", (src,), one=True):
                self._err("来源成员不存在", 404)
                return
            if not q("SELECT id FROM members WHERE id=?", (dst,), one=True):
                self._err("目标成员不存在", 404)
                return
            with DB_LOCK:
                for t in ("plans", "plan_days", "plan_exercises"):
                    db().execute(f"DELETE FROM {t} WHERE member_id=?", (dst,))
                p = db().execute("SELECT cycle_len,start_date FROM plans WHERE member_id=?",
                                 (src,)).fetchone()
                if p:
                    db().execute("INSERT INTO plans(member_id,cycle_len,start_date) VALUES(?,?,?)",
                                 (dst, p["cycle_len"], p["start_date"]))
                for r in db().execute("SELECT idx,label,type FROM plan_days WHERE member_id=?",
                                      (src,)).fetchall():
                    db().execute("INSERT INTO plan_days(member_id,idx,label,type) VALUES(?,?,?,?)",
                                 (dst, r["idx"], r["label"], r["type"]))
                for r in db().execute("""SELECT plan_idx,part,act,ord,aord,sets_n,reps,kind,mins,hr
                                         FROM plan_exercises WHERE member_id=?""", (src,)).fetchall():
                    db().execute("""INSERT INTO plan_exercises
                        (member_id,plan_idx,part,act,ord,aord,sets_n,reps,kind,mins,hr)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                        (dst, r["plan_idx"], r["part"], r["act"], r["ord"], r["aord"],
                         r["sets_n"], r["reps"], r["kind"], r["mins"], r["hr"]))
                db().commit()
            self._ok()
            return
        if path == "/api/session/start":
            mid, date = d.get("member_id"), str(d.get("date", ""))[:10]
            if not mid or not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
                self._err("参数错误")
                return
            now = int(time.time())
            q("""INSERT INTO sessions(member_id,date,start_ts,end_ts) VALUES(?,?,?,0)
                 ON CONFLICT(member_id,date) DO UPDATE SET start_ts=excluded.start_ts, end_ts=0""",
              (mid, date, now), commit=True)
            self._ok(start_ts=now)
            return
        if path == "/api/session/set":
            mid, date = d.get("member_id"), str(d.get("date", ""))[:10]
            if not mid or not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
                self._err("参数错误")
                return

            def to_ts(v):
                v = str(v or "").strip()
                if not v:
                    return None
                mm2 = re.match(r"^(\d{1,2}):(\d{2})$", v)
                if not mm2:
                    return None
                hh, mi2 = int(mm2.group(1)), int(mm2.group(2))
                if hh > 23 or mi2 > 59:
                    return None
                try:
                    return int(time.mktime(time.strptime(
                        f"{date} {hh:02d}:{mi2:02d}", "%Y-%m-%d %H:%M")))
                except (ValueError, OverflowError):
                    return None

            st, en = to_ts(d.get("start")), to_ts(d.get("end"))
            if st is None:
                self._err("开始时间格式不对")
                return
            if en is not None and en < st:
                self._err("结束时间不能早于开始时间")
                return
            q("""INSERT INTO sessions(member_id,date,start_ts,end_ts) VALUES(?,?,?,?)
                 ON CONFLICT(member_id,date) DO UPDATE SET start_ts=excluded.start_ts,
                 end_ts=excluded.end_ts""", (mid, date, st, en or 0), commit=True)
            self._ok(start_ts=st, end_ts=en or 0)
            return
        if path == "/api/session/finish":
            mid, date = d.get("member_id"), str(d.get("date", ""))[:10]
            if not mid or not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
                self._err("参数错误")
                return
            now = int(time.time())
            q("UPDATE sessions SET end_ts=? WHERE member_id=? AND date=?", (now, mid, date), commit=True)
            self._ok(end_ts=now)
            return
        if path in ("/api/day/start_from_plan", "/api/day/train"):
            mid, date = d.get("member_id"), str(d.get("date", ""))[:10]
            if not mid or not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
                self._err("参数错误")
                return
            force = d.get("plan_idx")
            if force in ("", None):
                force = None
            r, err = act_train(mid, date, force)
            if err:
                self._err(err)
                return
            self._ok(**r)
            return
        if path == "/api/day":
            mid, date = d.get("member_id"), str(d.get("date", ""))[:10]
            typ = d.get("type", "training") if d.get("type") in ("training", "rest") else "training"
            note = str(d.get("note", ""))[:500]
            if not mid or not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
                self._err("参数错误")
                return
            q("""INSERT INTO days(member_id,date,type,note) VALUES(?,?,?,?)
                 ON CONFLICT(member_id,date) DO UPDATE SET type=excluded.type, note=excluded.note""",
              (mid, date, typ, note), commit=True)
            self._ok()
            return
        if path in ("/api/day/rest", "/api/day/skip_rest"):
            mid, date = d.get("member_id"), str(d.get("date", ""))[:10]
            if not mid or not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
                self._err("参数错误")
                return
            r, err = act_rest(mid, date)
            if err:
                self._err(err)
                return
            self._ok(**r)
            return
        if path == "/api/day/feel":
            mid, date = d.get("member_id"), str(d.get("date", ""))[:10]
            if not mid or not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
                self._err("参数错误")
                return
            feel = str(d.get("feel", "") or "").strip()[:10]
            if feel and feel not in ("good", "ok", "tired", "hurt"):
                self._err("状态值无效")
                return
            q("""INSERT INTO days(member_id,date,type,feel) VALUES(?,?,'training',?)
                 ON CONFLICT(member_id,date) DO UPDATE SET feel=excluded.feel""",
              (mid, date, feel), commit=True)
            self._ok(feel=feel)
            return
        if path == "/api/day/delete":
            mid, date = d.get("member_id"), d.get("date")
            q("DELETE FROM sets WHERE exercise_id IN (SELECT id FROM exercises WHERE member_id=? AND date=?)",
              (mid, date), commit=True)
            q("DELETE FROM exercises WHERE member_id=? AND date=?", (mid, date), commit=True)
            q("DELETE FROM days WHERE member_id=? AND date=?", (mid, date), commit=True)
            q("DELETE FROM sessions WHERE member_id=? AND date=?", (mid, date), commit=True)
            self._ok()
            return
        if path == "/api/day/switch":
            mid, date = d.get("member_id"), str(d.get("date", ""))[:10]
            if not mid or not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
                self._err("参数错误")
                return
            day = q("SELECT * FROM days WHERE member_id=? AND date=?", (mid, date), one=True)
            if not day:
                self._err("这天还没记录")
                return
            r, err = (act_rest(mid, date) if day["type"] == "training" else act_train(mid, date))
            if err:
                self._err(err)
                return
            self._ok(**r)
            return
        if path == "/api/exercise/add":
            mid, date = d.get("member_id"), str(d.get("date", ""))[:10]
            name = str(d.get("name", "")).strip()[:50]
            if not name:
                self._err("部位名不能为空")
                return
            q("INSERT INTO days(member_id,date,type) VALUES(?,?,'training') ON CONFLICT(member_id,date) DO NOTHING",
              (mid, date), commit=True)
            mx = q("SELECT COALESCE(MAX(ord),-1)+1 AS n FROM exercises WHERE member_id=? AND date=?",
                   (mid, date), one=True)
            with DB_LOCK:
                cur = db().execute("INSERT INTO exercises(member_id,date,name,ord) VALUES(?,?,?,?)",
                                   (mid, date, name, mx["n"]))
                eid = cur.lastrowid
                db().commit()
            self._ok(id=eid)
            return
        if path == "/api/exercise/rename":
            q("UPDATE exercises SET name=? WHERE id=?", (str(d.get("name", "")).strip()[:50], d.get("id")), commit=True)
            self._ok()
            return
        if path == "/api/exercise/delete":
            eid = d.get("id")
            q("DELETE FROM sets WHERE exercise_id=?", (eid,), commit=True)
            q("DELETE FROM exercises WHERE id=?", (eid,), commit=True)
            self._ok()
            return
        if path == "/api/act/delete":
            eid, nm0 = d.get("exercise_id"), str(d.get("name", "") or "")
            q("DELETE FROM sets WHERE exercise_id=? AND COALESCE(name,'')=?", (eid, nm0), commit=True)
            self._ok()
            return
        if path == "/api/act/rename":
            eid = d.get("exercise_id")
            old = str(d.get("old", "") or "")
            new = str(d.get("new", "") or "").strip()[:50]
            q("UPDATE sets SET name=? WHERE exercise_id=? AND COALESCE(name,'')=?",
              (new, eid, old), commit=True)
            self._ok()
            return
        if path == "/api/cardio/add":
            mid, date = d.get("member_id"), str(d.get("date", ""))[:10]
            if not mid or not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
                self._err("参数错误")
                return
            nm = str(d.get("name") or "").strip()[:50] or "有氧"

            def _cf(v):
                try:
                    return float(v) if v not in (None, "") else None
                except (TypeError, ValueError):
                    return None
            with DB_LOCK:
                row = db().execute(
                    "SELECT id FROM exercises WHERE member_id=? AND date=? AND name='有氧'",
                    (mid, date)).fetchone()
                if row:
                    eid = row["id"]
                else:
                    nx = db().execute("""SELECT COALESCE(MAX(ord),-1)+1 AS n FROM exercises
                                         WHERE member_id=? AND date=?""", (mid, date)).fetchone()["n"] or 0
                    cur = db().execute("""INSERT INTO exercises(member_id,date,name,ord,reps_target)
                                          VALUES(?,?,'有氧',?,'')""", (mid, date, nx))
                    eid = cur.lastrowid
                sn = db().execute("SELECT COALESCE(MAX(set_no),0)+1 AS n FROM sets WHERE exercise_id=?",
                                  (eid,)).fetchone()["n"] or 1
                db().execute("""INSERT INTO sets(exercise_id,set_no,weight,reps,name,mins,hr)
                                VALUES(?,?,NULL,NULL,?,?,?)""",
                             (eid, sn, nm, _cf(d.get("mins")), _cf(d.get("hr"))))
                db().commit()
            self._ok()
            return
        if path == "/api/set/add":
            eid = d.get("exercise_id")
            try:
                w = None if d.get("weight") in (None, "") else float(d.get("weight"))
                r = None if d.get("reps") in (None, "") else float(d.get("reps"))
                mn = None if d.get("mins") in (None, "") else float(d.get("mins"))
                hr = None if d.get("hr") in (None, "") else float(d.get("hr"))
            except (TypeError, ValueError):
                self._err("重量/次数/时长/心率必须是数字")
                return
            nm = str(d.get("name") or "").strip()[:50]
            mx = q("SELECT COALESCE(MAX(set_no),0)+1 AS n FROM sets WHERE exercise_id=?", (eid,), one=True)
            q("""INSERT INTO sets(exercise_id,set_no,weight,reps,name,mins,hr)
                 VALUES(?,?,?,?,?,?,?)""",
              (eid, mx["n"], w, r, nm, mn, hr), commit=True)
            self._ok()
            return
        if path == "/api/set/update":
            try:
                w = None if d.get("weight") in (None, "") else float(d.get("weight"))
                r = None if d.get("reps") in (None, "") else float(d.get("reps"))
                mn = None if d.get("mins") in (None, "") else float(d.get("mins"))
                hr = None if d.get("hr") in (None, "") else float(d.get("hr"))
            except (TypeError, ValueError):
                self._err("重量/次数/时长/心率必须是数字")
                return
            fld, args = [], []
            if "weight" in d or "reps" in d:
                fld += ["weight=?", "reps=?"]
                args += [w, r]
            if "mins" in d:
                fld.append("mins=?")
                args.append(mn)
            if "hr" in d:
                fld.append("hr=?")
                args.append(hr)
            if "name" in d:          # 只在显式传了 name 时才改动作名, 否则会把它清空
                fld.append("name=?")
                args.append(str(d.get("name") or "").strip()[:50])
            if not fld:
                self._err("没有要改的内容")
                return
            q("UPDATE sets SET " + ",".join(fld) + " WHERE id=?",
              (*args, d.get("id")), commit=True)
            self._ok()
            return
        if path == "/api/set/delete":
            q("DELETE FROM sets WHERE id=?", (d.get("id"),), commit=True)
            self._ok()
            return
        if path == "/api/body":
            mid, date = d.get("member_id"), str(d.get("date", ""))[:10]
            if not mid or not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
                self._err("参数错误")
                return
            # 只更新这次真正传了值的字段: 没传 / 传空的都不动(避免把已有的体脂等覆盖掉)
            fields = {}
            for k in ("weight", "bodyfat", "chest", "shoulder", "arm", "waist", "leg"):
                if k not in d or d.get(k) in (None, ""):
                    continue
                try:
                    fields[k] = float(d.get(k))
                except (TypeError, ValueError):
                    self._err(f"{k} 必须是数字")
                    return
            if not fields:
                self._err("没有要更新的身体数据")
                return
            q("INSERT OR IGNORE INTO body(member_id,date) VALUES(?,?)", (mid, date), commit=True)
            q("UPDATE body SET " + ",".join(f"{k}=?" for k in fields) +
              " WHERE member_id=? AND date=?",
              (*fields.values(), mid, date), commit=True)
            self._ok()
            return
        if path in ("/api/intake", "/api/intake/add"):
            mid, date = d.get("member_id"), str(d.get("date", ""))[:10]
            if not mid or not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
                self._err("参数错误")
                return
            vals = {}
            for k in ("kcal", "protein"):
                v = d.get(k)
                if v in (None, ""):
                    vals[k] = None
                else:
                    try:
                        vals[k] = float(v)
                    except (TypeError, ValueError):
                        self._err(f"{k} 必须是数字")
                        return
            if vals["kcal"] is None and vals["protein"] is None:
                self._err("热量和蛋白至少填一项")
                return
            note = str(d.get("note") or "").strip()[:40]
            with DB_LOCK:
                cur = db().execute("""INSERT INTO intake_log(member_id,date,kcal,protein,note,ts)
                                      VALUES(?,?,?,?,?,?)""",
                                   (mid, date, vals["kcal"], vals["protein"], note, int(time.time())))
                db().commit()
                nid = cur.lastrowid
            self._ok(id=nid)
            return
        if path == "/api/intake/update":
            iid = d.get("id")
            row = q("SELECT id FROM intake_log WHERE id=?", (iid,), one=True) if iid else None
            if not row:
                self._err("记录不存在", 404)
                return
            sets, args = [], []
            for k in ("kcal", "protein"):
                if k in d:
                    v = d.get(k)
                    if v in (None, ""):
                        sets.append(f"{k}=NULL")
                    else:
                        try:
                            sets.append(f"{k}=?")
                            args.append(float(v))
                        except (TypeError, ValueError):
                            self._err(f"{k} 必须是数字")
                            return
            if "note" in d:
                sets.append("note=?")
                args.append(str(d.get("note") or "").strip()[:40])
            if not sets:
                self._err("没有要改的内容")
                return
            q("UPDATE intake_log SET " + ",".join(sets) + " WHERE id=?",
              (*args, iid), commit=True)
            self._ok()
            return
        if path == "/api/intake/delete":
            iid = d.get("id")
            if not iid:
                self._err("参数错误")
                return
            q("DELETE FROM intake_log WHERE id=?", (iid,), commit=True)
            self._ok()
            return
        if path == "/api/intake/clear":
            mid, date = d.get("member_id"), str(d.get("date", ""))[:10]
            if not mid or not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
                self._err("参数错误")
                return
            q("DELETE FROM intake_log WHERE member_id=? AND date=?", (mid, date), commit=True)
            self._ok()
            return
        if path == "/api/tpl":
            mid = d.get("member_id")
            items = d.get("items")
            if not mid or not isinstance(items, list):
                self._err("参数错误")
                return
            with DB_LOCK:
                db().execute("DELETE FROM intake_tpl WHERE member_id=?", (mid,))
                for i, it in enumerate(items[:30]):
                    if not isinstance(it, dict):
                        continue
                    nm = str(it.get("name") or "").strip()[:20]
                    vals = []
                    for k in ("kcal", "protein"):
                        v = it.get(k)
                        if v in (None, ""):
                            vals.append(None)
                        else:
                            try:
                                vals.append(float(v))
                            except (TypeError, ValueError):
                                vals.append(None)
                    db().execute("""INSERT INTO intake_tpl(member_id,name,kcal,protein,ord)
                                    VALUES(?,?,?,?,?)""", (mid, nm, vals[0], vals[1], i))
                db().commit()
            self._ok()
            return
        if path == "/api/body/delete":
            q("DELETE FROM body WHERE member_id=? AND date=?", (d.get("member_id"), d.get("date")), commit=True)
            self._ok()
            return
        self._err("404", 404)


LOGIN_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>健身记录 · 登录</title>
<link rel="manifest" href="/manifest.webmanifest">
<meta name="theme-color" content="#17202e">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
<link rel="icon" href="/icon-192.png">
<style>
  body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
       background:#f2f3f7;font-family:"PingFang SC","Microsoft YaHei",sans-serif;color:#1f2329}
  .box{background:#fff;border:1px solid #e3e5ea;border-radius:14px;padding:40px 44px;
       width:300px;text-align:center;box-shadow:0 8px 30px rgba(0,0,0,.08)}
  h1{font-size:20px;margin:0 0 6px}
  p{color:#7a8194;font-size:13px;margin:0 0 24px}
  input{width:100%;box-sizing:border-box;padding:11px 14px;border-radius:8px;border:1px solid #d3d5db;
        background:#f2f3f7;font-size:15px;outline:none;text-align:center;letter-spacing:2px}
  input:focus{border-color:#2563eb;background:#fff}
  button{margin-top:16px;width:100%;padding:11px;border:0;border-radius:8px;background:#2563eb;
         color:#fff;font-size:15px;cursor:pointer}
  button:disabled{opacity:.5}
  .err{color:#e11d48;font-size:13px;min-height:18px;margin-top:10px}
</style>
</head>
<body>
<div class="box">
  <h1>💪 健身记录</h1>
  <p>请输入访问密码</p>
  <input type="password" id="pwd" placeholder="密码" autofocus>
  <button id="btn">进入</button>
  <div class="err" id="err"></div>
</div>
<script>
const inp=document.getElementById('pwd'),btn=document.getElementById('btn'),err=document.getElementById('err');
function go(){if(!inp.value)return;btn.disabled=true;err.textContent='';
  fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({password:inp.value})}).then(r=>{if(r.ok){location.reload();}
    else{btn.disabled=false;err.textContent='密码错误';inp.value='';inp.focus();}})
    .catch(()=>{btn.disabled=false;err.textContent='网络错误';});}
btn.onclick=go;inp.addEventListener('keydown',e=>{if(e.key==='Enter')go();});
</script>
</body>
</html>
"""


def _prep_data_dir():
    """容器里通常以 root 启动: 把数据目录交给运行用户, 然后降权。

    新 clone 的人没有 data/ 目录, docker 会用 root 建它, 非 root 进程就写不了数据库。
    这里自动把属主改过来再降权, 省掉手动 mkdir/chown 那一步。
    """
    if os.getuid() != 0:
        return
    uid = int(os.environ.get("RUN_UID", "1000"))
    gid = int(os.environ.get("RUN_GID", "1000"))
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        os.chown(DATA_DIR, uid, gid)
        for f in os.listdir(DATA_DIR):
            try:
                os.chown(os.path.join(DATA_DIR, f), uid, gid)
            except OSError:
                pass
    except OSError:
        pass
    try:
        os.setgid(gid)
        os.setuid(uid)
    except OSError:
        pass


def main():
    _prep_data_dir()
    db()
    print(f"健身记录启动: http://0.0.0.0:{PORT}  数据: {DB_PATH}", flush=True)
    # 没通过环境变量指定密码时, 把当前密码打出来, 免得第一次用不知道密码
    if not PASSWORD:
        print(f"  访问密码: {get_setting('access_password', '')}", flush=True)
    if not SUPER_PASSWORD:
        print(f"  管理密码: {get_setting('super_password', '')}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
