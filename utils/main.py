import os
import random
import json
import time
import uuid
import hashlib
import asyncio
import threading
from pathlib import Path as _Path
from fastapi import FastAPI, Request, UploadFile, File as FastAPIFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel
from openai import OpenAI, AsyncOpenAI

# 数据库依赖（可选，不安装对应包也能启动）
try:
    import lancedb
except ImportError:
    lancedb = None
try:
    from neo4j import GraphDatabase
except ImportError:
    GraphDatabase = None
try:
    from sqlalchemy import create_engine, text
except ImportError:
    create_engine = None
    text = None

PROJECT_ROOT = str(_Path(__file__).resolve().parent.parent)

from utils.config import (
    API_KEY, BASE_URL,
    CHAT_MODEL, ROUTER_MODEL, EMBEDDING_MODEL,
    LANCEDB_URI, TABLE_NAME,
    MYSQL_URI,
    NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD,
    ADMIN_PASSWORD,
)
from utils.feedback import record_feedback, get_stats, clear_feedback

# ==========================================
# 1. 客户端初始化
# ==========================================
if not API_KEY:
    raise RuntimeError("ZUA_API_KEY 未配置，请设置环境变量或 .env 文件")

CHAT_CLIENT = OpenAI(api_key=API_KEY, base_url=BASE_URL)
ASYNC_CHAT_CLIENT = AsyncOpenAI(api_key=API_KEY, base_url=BASE_URL)

# LanceDB（本地向量库，随项目分发）
db = lancedb.connect(LANCEDB_URI) if lancedb else None
try:
    policy_table = db.open_table(TABLE_NAME) if db else None
except Exception:
    print("⚠️ 警告：找不到 LanceDB 表，请确认是否已运行向量化脚本。")
    policy_table = None

# MySQL（可选，未配置时跳过）
sql_engine = None
if create_engine and MYSQL_URI:
    try:
        sql_engine = create_engine(MYSQL_URI)
    except Exception as e:
        print(f"⚠️ MySQL 连接初始化失败: {e}")

# Neo4j（可选，未配置时跳过）
neo4j_driver = None
if GraphDatabase and NEO4J_URI and NEO4J_USER and NEO4J_PASSWORD:
    try:
        neo4j_driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    except Exception as e:
        print(f"⚠️ Neo4j 连接初始化失败: {e}")

# FastAPI
app = FastAPI(title="ZUA AI 招生助手")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ==========================================
# 1.5 会话管理 (持久化 JSON 存储, 30min TTL, 5 rounds)
# ==========================================
SESSIONS: dict[str, list] = {}
SESSION_TTL = 86400  # 24h
MAX_ROUNDS = 5
CLEANUP_INTERVAL = 300
SESSION_FILE = os.path.join(PROJECT_ROOT, "data", "sessions.json")
START_TIME = time.strftime("%Y-%m-%d %H:%M:%S")


def _load_sessions() -> None:
    """从磁盘加载会话到内存。"""
    global SESSIONS
    if not os.path.exists(SESSION_FILE):
        return
    try:
        with open(SESSION_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                SESSIONS = data
    except (json.JSONDecodeError, IOError):
        SESSIONS = {}


def _save_sessions() -> None:
    """将会话数据写入磁盘。"""
    try:
        os.makedirs(os.path.dirname(SESSION_FILE), exist_ok=True)
        with open(SESSION_FILE, "w", encoding="utf-8") as f:
            json.dump(SESSIONS, f, ensure_ascii=False)
    except IOError:
        pass


def _cleanup_expired_sessions() -> None:
    now = time.time()
    expired = [
        sid for sid, hist in SESSIONS.items()
        if not hist or (now - hist[-1].get("_t", 0) > SESSION_TTL)
    ]
    for sid in expired:
        SESSIONS.pop(sid, None)
    if expired:
        _save_sessions()


def _start_cleanup_loop() -> None:
    while True:
        time.sleep(CLEANUP_INTERVAL)
        _cleanup_expired_sessions()


_load_sessions()
threading.Thread(target=_start_cleanup_loop, daemon=True).start()


class ChatRequest(BaseModel):
    model: str = "default"
    messages: list
    temperature: float = 0.2
    stream: bool = True
    session_id: str | None = None


# ==========================================
# 2. 核心：智能意图路由 (Intent Router)
# ==========================================
def detect_intent(query: str) -> str:
    prompt = (
        "判断以下用户问题的意图，只能输出以下四个英文单词之一，绝不输出其他字符：\n"
        "- SCORE : 询问历年录取分数、位次、最低分、能上什么专业（估分）。\n"
        "- POLICY : 询问硕士点、宿舍、食堂、军训、转专业、奖助贷政策、参军退伍、大学英语、日语、俄语、外语等校园生活指南，包括介绍学校。\n"
        "- MAJOR : 询问专业的从属学院、学制、特色、就业去向、选科要求。\n"
        "- OTHER : 闲聊寒暄，或与学校招生完全无关的问题。\n"
        f"用户问题：{query}"
    )
    try:
        response = CHAT_CLIENT.chat.completions.create(
            model=ROUTER_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
        )
        intent = response.choices[0].message.content.strip().upper()
        return intent if intent in {"SCORE", "MAJOR", "POLICY", "OTHER"} else "POLICY"
    except Exception:
        return "POLICY"


# ==========================================
# 3. 三路查询模块 (Retrievers)
# ==========================================

def retrieve_from_lancedb(query: str) -> str:
    if not policy_table:
        return "暂无政策数据库连接。"
    try:
        query_vec = CHAT_CLIENT.embeddings.create(
            input=query, model=EMBEDDING_MODEL
        ).data[0].embedding
        results = policy_table.search(query_vec).limit(3).to_pandas()
        return "\n\n".join(results["text"].tolist())
    except Exception as e:
        return f"向量库查询失败: {e}"


def retrieve_from_mysql(query: str) -> str:
    if not sql_engine:
        return "暂无分数数据库连接。"

    schema_prompt = (
        "你是一个精通 MySQL 的数据分析师。根据用户问题编写查询 SQL。\n"
        "表名: historical_scores\n"
        "字段: year(年份), province(省市), admission_type(录取类型), "
        "subject_category(科类如物理类), major_name(专业名称), "
        "enroll_count(录取人数), min_score(最低分), avg_score(平均分), max_score(最高分)\n\n"
        f'用户问题："{query}"\n\n'
        "【核心铁律】\n"
        "1. 只能返回纯 SQL 语句，绝对不要包含 markdown 标记 (如 ```sql)。\n"
        "2. 必须使用 SELECT *，绝对禁止只 SELECT 单个字段！\n"
        "3. 专业名称匹配必须使用 LIKE '%%关键字%%'（如 LIKE '%%工商管理%%'）。\n"
        "4. 省份匹配必须使用模糊匹配（如 province LIKE '%%北京%%'）。\n"
        "5. 必须加上 LIMIT 100 防止结果过多。\n"
    )
    try:
        response = CHAT_CLIENT.chat.completions.create(
            model=CHAT_MODEL,
            messages=[{"role": "user", "content": schema_prompt}],
            temperature=0.0,
        )
        sql_query = response.choices[0].message.content.strip()
        sql_query = sql_query.replace("```sql", "").replace("```", "").strip()

        if not sql_query.upper().startswith("SELECT"):
            return "生成的 SQL 语句不合法，已拒绝执行。"

        print(f"\n🛠️ 生成的 SQL: {sql_query}")

        with sql_engine.connect() as conn:
            result = conn.execute(text(sql_query))
            rows = result.fetchall()
            print(f"📦 MySQL 实际返回的行数: {len(rows)}")

            if len(rows) == 0:
                print("⚠️ 警告：SQL 执行成功，但返回 0 条数据！")
                return f"数据库中未查询到相关记录。执行的 SQL 为: {sql_query}"

            context = "查询到的完整录取分数数据如下：\n"
            for row in rows:
                row_dict = dict(row._mapping)
                for k, v in row_dict.items():
                    row_dict[k] = str(v) if v is not None else "无"
                context += str(row_dict) + "\n"

            print(f"📄 最终喂给大模型的上下文数据: \n{context}")
            return context

    except Exception as e:
        print(f"❌ MySQL 查询代码运行报错: {e}")
        return f"分数数据库查询出错，原因: {e}"


def retrieve_from_neo4j(query: str) -> str:
    if not neo4j_driver:
        return "暂无图谱数据库连接。"

    schema_prompt = (
        "你是一个精通 Neo4j Cypher 语言的知识图谱专家。根据用户问题编写查询语句。\n"
        "图谱 Schema:\n"
        "- 节点: College(name), Major(name, duration), Feature(name)\n"
        "- 关系: (College)-[:CONTAINS]->(Major), (Major)-[:HAS_FEATURE]->(Feature)\n\n"
        f'用户问题："{query}"\n\n'
        "要求：\n"
        "1. 只能返回可以执行的纯 Cypher 语句，绝对不要包含 markdown 标记 (如 ```cypher)。\n"
        "2. 对 name 属性使用正则或 CONTAINS 进行模糊匹配。\n"
        "3. 必须返回具体的节点属性，如 RETURN c.name, m.name, f.name LIMIT 50。\n"
    )
    try:
        response = CHAT_CLIENT.chat.completions.create(
            model=CHAT_MODEL,
            messages=[{"role": "user", "content": schema_prompt}],
            temperature=0.0,
        )
        cypher_query = response.choices[0].message.content.strip()
        cypher_query = cypher_query.replace("```cypher", "").replace("```", "").strip()
        print(f"🛠️ 生成的 Cypher: {cypher_query}")

        with neo4j_driver.session() as session:
            result = session.run(cypher_query)
            records = [record.data() for record in result]

            if not records:
                return "未查询到相关的专业或图谱记录。"

            context = "查询到的专业图谱数据如下：\n"
            for rec in records:
                context += str(rec) + "\n"
            return context
    except Exception as e:
        return f"图谱数据库查询出错，原因: {e}"


# ==========================================
# 4. 图片关键词配置
# ==========================================
IMAGE_CONFIG = {
    "canting": {"keywords": ["餐厅", "食堂", "吃饭", "美食", "饭菜"], "max": 3},
    "ditu": {"keywords": ["地图", "位置", "校区", "多大"], "max": 2},
    "fengjing": {"keywords": ["风景", "校园环境", "漂亮", "图书馆", "操场"], "max": 8},
    "qinshi": {"keywords": ["寝室", "宿舍", "几人间", "床", "空调"], "max": 5},
    "shangyejie": {
        "keywords": ["商业街", "超市", "购物", "买东西", "生活费"], "max": 5,
    },
}


def pick_image_instruction(query: str) -> str:
    selected_folder = None
    max_imgs = 0
    for folder, cfg in IMAGE_CONFIG.items():
        if any(kw in query for kw in cfg["keywords"]):
            selected_folder = folder
            max_imgs = cfg["max"]
            break

    if selected_folder:
        sample_size = min(2, max_imgs)
        nums = random.sample(range(1, max_imgs + 1), sample_size)
        img_lines = [
            f"![{selected_folder}实拍](/{selected_folder}/{n}.jpg)"
            for n in nums
        ]
        img_str = "  ".join(img_lines)
        return (
            "3. 【强制要求】请务必在回答的最末尾，"
            f"原样附加以下实拍图片代码，绝对不要修改路径：\n\n{img_str}\n"
        )
    return (
        "3. 【纯文本指令】本次回答【绝对禁止】输出任何形式的图片 Markdown 语法"
        "（如 `![...](...)` 或 `<img>`）。即使检索资料中含有类似 "
        "`zua_aviation_major`、`logo` 等图片标识符，也必须丢弃，只能输出文字！"
    )


# ==========================================
# 5. API 接口：最终生成与流式返回
# ==========================================
@app.post("/v1/chat/completions")
async def chat_endpoint(request: ChatRequest):
    session_id = request.session_id or str(uuid.uuid4())
    now = time.time()

    current_query = request.messages[-1]["content"]

    intent = detect_intent(current_query)
    print(f"\n🎯 用户意图识别为: {intent} | 问题: {current_query}")

    retrieved_context = ""
    if intent == "POLICY":
        retrieved_context = retrieve_from_lancedb(current_query)
    elif intent == "SCORE":
        retrieved_context = retrieve_from_mysql(current_query)
    elif intent == "MAJOR":
        retrieved_context = retrieve_from_neo4j(current_query)
    elif intent == "OTHER":
        retrieved_context = "这是一个与学校招生无关的问题，直接拒绝回答。"

    image_instruction = pick_image_instruction(current_query)

    system_prompt = (
        "你是郑州航空工业管理学院（ZUA）官方 Web 端智能招生助手。\n"
        "你的回答代表学校官方立场，必须客观、严谨、不卑不亢、惜字如金。\n\n"
        "【核心铁律】\n"
        "1. 必须 100% 基于以下检索到的官方资料回答问题(最高优先级)。"
        "严禁你利用自己的预训练知识自行编造、猜测分数或政策。"
        "这一条最重要不要忘记，不要编造。\n"
        "2. 结构化排版：遇到多条分数对比，必须使用 Markdown 表格输出；"
        "遇到多条特色介绍，必须使用 Markdown 无序列表。\n"
        "3. 如果下方提供了图片 Markdown 代码，请务必在回答的最后原样输出它，"
        "不要修改图片路径。\n"
        f"{image_instruction}\n"
        '4. 如果下方提示"未查询到相关记录"，回复：'
        '"抱歉，本助手暂未查到该信息..."。\n'
        '5. 当问道航空特色专业时回答"郑航王牌航空特色：'
        "飞行器设计与工程（河南唯一航空国家级一流）、飞行器动力工程、"
        "无人驾驶航空器系统工程、交通运输（空管签派）、飞行器适航技术。"
        "聚焦军机、民航、无人机研发运维，对接中航、商飞、民航岗位，"
        "航空管工融合，省内航空赛道顶尖，就业对口稳定。"
        '6. 无关话题强制回复："本助手仅限解答郑州航空工业管理学院招生相关问题。"\n'
        "7. 反图片输出机制（极高优先级）：除了上述第3点强制要求的本地实拍图片外，"
        "【绝对禁止】在回答中输出任何其他的 Markdown 图片语法（即 `![...](...)`）"
        '比如"![郑州航空工业管理学院校徽](https://zua.edu.cn/images/logo.png)"。'
        "即使检索到的背景资料中带有网络图片链接（如校徽等），你也必须在回答时"
        "将其静默过滤掉！\n"
        "8. 当检测到 keyword 中的风景、校园环境、商业街、超市这些关键字时，"
        "请勿输出文字只用输出照片\n\n"
        f"【检索到的官方资料 (意图通道: {intent})】\n"
        f"{retrieved_context}"
    )

    history = SESSIONS.get(session_id, [])
    recent_history = [m for m in history if m.get("role")][-(MAX_ROUNDS * 2):]
    final_messages = (
        [{"role": "system", "content": system_prompt}]
        + recent_history
        + [{"role": "user", "content": current_query}]
    )

    source_map = {"POLICY": "LanceDB", "SCORE": "MySQL", "MAJOR": "Neo4j", "OTHER": "无"}
    ctx_snippet = retrieved_context[:300] + ("..." if len(retrieved_context) > 300 else "")

    full_response = ""

    def _after_response() -> None:
        history.append({
            "role": "user", "content": current_query, "_t": now,
            "_intent": intent, "_source": source_map.get(intent, "unknown"),
            "_ctx": ctx_snippet, "_chars": len(retrieved_context),
        })
        history.append({"role": "assistant", "content": full_response, "_t": now})
        SESSIONS[session_id] = history[-(MAX_ROUNDS * 2 + 2):]
        _save_sessions()
        record_feedback(
            session_id=session_id,
            intent=intent,
            query=current_query,
            query_length=len(current_query),
            retrieved_chars=len(retrieved_context),
            missed=(
                not retrieved_context
                or "未查询到" in retrieved_context
                or "失败" in retrieved_context
            ),
        )

    if request.stream:
        async def async_stream_with_callback():
            nonlocal full_response
            try:
                response = await ASYNC_CHAT_CLIENT.chat.completions.create(
                    model=CHAT_MODEL,
                    messages=final_messages,
                    temperature=request.temperature,
                    stream=True,
                )
                async for chunk in response:
                    if chunk.choices and chunk.choices[0].delta and chunk.choices[0].delta.content:
                        content = chunk.choices[0].delta.content
                        full_response += content
                        data = {"choices": [{"delta": {"content": content}}]}
                        yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"
            except Exception as e:
                error_msg = {
                    "choices": [
                        {"delta": {"content": f"\n[大模型服务异常: {e}]"}}
                    ]
                }
                full_response = f"\n[大模型服务异常: {e}]"
                yield f"data: {json.dumps(error_msg, ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"
            finally:
                _after_response()

        return StreamingResponse(
            async_stream_with_callback(),
            media_type="text/event-stream",
            headers={
                "X-Session-Id": session_id,
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    response = CHAT_CLIENT.chat.completions.create(
        model=CHAT_MODEL,
        messages=final_messages,
        temperature=request.temperature,
        stream=False,
    )
    full_response = response.choices[0].message.content
    _after_response()

    return JSONResponse(
        content={"choices": [{"message": {"content": full_response}}], "session_id": session_id},
        headers={"X-Session-Id": session_id},
    )


# ==========================================
# 6. 健康检查 API
# ==========================================
@app.get("/v1/health")
async def health_check():
    checks = {}
    # Neo4j
    if neo4j_driver:
        try:
            with neo4j_driver.session() as s:
                s.run("RETURN 1")
            checks["neo4j"] = "ok"
        except Exception as e:
            checks["neo4j"] = str(e)[:120]
    else:
        checks["neo4j"] = "driver not initialized"
    # MySQL
    if sql_engine:
        try:
            with sql_engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            checks["mysql"] = "ok"
        except Exception as e:
            checks["mysql"] = str(e)[:120]
    else:
        checks["mysql"] = "driver not initialized"
    # Admin password
    checks["admin_password"] = "set" if ADMIN_PASSWORD else "not set"
    return checks


# ==========================================
# 7. 会话管理 API
# ==========================================
@app.get("/v1/admin/sessions")
async def list_sessions():
    sessions = []
    for sid, hist in sorted(SESSIONS.items(), key=lambda x: x[1][0].get("_t", 0) if x[1] else 0, reverse=True):
        user_msgs = [m for m in hist if m.get("role") == "user"]
        if not user_msgs:
            continue
        first = user_msgs[0]
        last = user_msgs[-1]
        sessions.append({
            "session_id": sid,
            "first_query": first.get("content", "")[:120],
            "last_query": last.get("content", "")[:120],
            "query_count": len(user_msgs),
            "created": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(first.get("_t", 0))),
            "last_active": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(last.get("_t", 0))),
            "last_intent": last.get("_intent", ""),
            "last_source": last.get("_source", ""),
        })
    return {"sessions": sessions, "total": len(sessions)}


@app.get("/v1/admin/sessions/{session_id}")
async def get_session(session_id: str):
    hist = SESSIONS.get(session_id)
    if not hist:
        return JSONResponse(status_code=404, content={"error": "会话不存在或已过期"})
    user_msgs = [m for m in hist if m.get("role") == "user"]
    ai_msgs = [m for m in hist if m.get("role") == "assistant"]
    turns = []
    for um in user_msgs:
        turn = {
            "query": um.get("content", ""),
            "intent": um.get("_intent", ""),
            "source": um.get("_source", ""),
            "context_snippet": um.get("_ctx", ""),
            "retrieved_chars": um.get("_chars", 0),
        }
        for am in ai_msgs:
            if am.get("_t", 0) >= um.get("_t", 0):
                turn["answer"] = am.get("content", "")
                break
        turns.append(turn)
    return {
        "session_id": session_id,
        "created": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(user_msgs[0].get("_t", 0))) if user_msgs else "",
        "last_active": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(user_msgs[-1].get("_t", 0))) if user_msgs else "",
        "query_count": len(user_msgs),
        "turns": turns,
    }


@app.delete("/v1/admin/sessions")
async def clear_sessions():
    SESSIONS.clear()
    _save_sessions()
    return {"status": "ok", "message": "已清空所有会话"}


# ==========================================
# 7. 统计 API
# ==========================================
@app.get("/v1/stats")
async def stats_endpoint(request: Request):
    result = get_stats()
    return result


@app.post("/v1/admin/login")
async def admin_login(request: Request):
    body = await request.json()
    pwd = body.get("password", "")
    if ADMIN_PASSWORD and pwd == ADMIN_PASSWORD:
        return {"ok": True}
    return JSONResponse(status_code=401, content={"ok": False})


@app.delete("/v1/stats")
async def clear_stats_endpoint():
    clear_feedback()
    return {"status": "ok"}


# ==========================================
# 10. 扩展管理 API (控制面板)
# ==========================================

@app.post("/v1/admin/password")
async def change_admin_password(request: Request):
    body = await request.json()
    new_pwd = body.get("password", "").strip()
    if not new_pwd:
        return JSONResponse(status_code=400, content={"error": "密码不能为空"})
    if len(new_pwd) < 6:
        return JSONResponse(status_code=400, content={"error": "密码至少6个字符"})
    try:
        env_path = ENV_PATH
        lines = []
        env_exists = os.path.exists(env_path)
        if env_exists:
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("ZUA_ADMIN_PASSWORD"):
                        lines.append(f"ZUA_ADMIN_PASSWORD={new_pwd}\n")
                    else:
                        lines.append(line)
        else:
            lines = [f"ZUA_ADMIN_PASSWORD={new_pwd}\n"]
        with open(env_path, "w", encoding="utf-8") as f:
            f.writelines(lines)
        global ADMIN_PASSWORD
        ADMIN_PASSWORD = new_pwd
        return {"status": "ok", "message": "密码已更新"}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/v1/admin/logs")
async def get_service_logs(level: str = "info", limit: int = 100):
    import subprocess
    levels = {"debug": "debug", "info": "info", "warning": "warning", "error": "err", "all": ""}
    pri = levels.get(level.lower(), "info")
    try:
        args = ["journalctl", "-u", "zua-backend.service", "--no-pager", "-n", str(min(limit, 500)), "-o", "short-iso"]
        if pri:
            args += ["-p", pri]
        result = subprocess.run(args, capture_output=True, text=True, timeout=10)
        lines = result.stdout.strip().split("\n") if result.stdout.strip() else []
        parsed = []
        for line in lines:
            parts = line.split(" ", 1)
            if len(parts) == 2:
                ts = parts[0]
                msg = parts[1].split(" python3[", 1)[-1] if " python3[" in parts[1] else parts[1]
                lvl = "info"
                if "ERROR" in msg:
                    lvl = "error"
                elif "WARNING" in msg:
                    lvl = "warning"
                elif "INFO" in msg:
                    lvl = "info"
                parsed.append({"timestamp": ts, "level": lvl, "message": msg})
        return {"logs": parsed, "total": len(parsed)}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/v1/admin/files/env")
async def get_env_file():
    try:
        env_path = ENV_PATH
        if not os.path.exists(env_path):
            return {"content": "", "exists": False, "path": env_path}
        with open(env_path, "r", encoding="utf-8") as f:
            content = f.read()
        return {"content": content, "exists": True, "path": env_path, "lines": len(content.splitlines())}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.put("/v1/admin/files/env")
async def update_env_file(request: Request):
    body = await request.json()
    content = body.get("content", "")
    try:
        with open(ENV_PATH, "w", encoding="utf-8") as f:
            f.write(content)
        global ADMIN_PASSWORD, API_KEY, BASE_URL, MYSQL_URI, NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD
        for line in content.splitlines():
            if line.startswith("ZUA_ADMIN_PASSWORD="):
                ADMIN_PASSWORD = line.split("=", 1)[1].strip()
            elif line.startswith("ZUA_API_KEY="):
                API_KEY = line.split("=", 1)[1].strip()
            elif line.startswith("ZUA_BASE_URL="):
                BASE_URL = line.split("=", 1)[1].strip()
            elif line.startswith("ZUA_MYSQL_URI="):
                MYSQL_URI = line.split("=", 1)[1].strip()
            elif line.startswith("ZUA_NEO4J_URI="):
                NEO4J_URI = line.split("=", 1)[1].strip()
            elif line.startswith("ZUA_NEO4J_USER="):
                NEO4J_USER = line.split("=", 1)[1].strip()
            elif line.startswith("ZUA_NEO4J_PASSWORD="):
                NEO4J_PASSWORD = line.split("=", 1)[1].strip()
        return {"status": "ok", "lines": len(content.splitlines())}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/v1/admin/files/config_py")
async def get_config_py():
    try:
        path = CONFIG_PY_PATH
        if not os.path.exists(path):
            return JSONResponse(status_code=404, content={"error": "config.py 不存在"})
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        return {"content": content, "path": path, "lines": len(content.splitlines()), "size_kb": round(len(content.encode("utf-8")) / 1024, 1)}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.put("/v1/admin/files/config_py")
async def update_config_py(request: Request):
    body = await request.json()
    content = body.get("content", "")
    try:
        with open(CONFIG_PY_PATH, "w", encoding="utf-8") as f:
            f.write(content)
        return {"status": "ok", "lines": len(content.splitlines())}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/v1/admin/service/status")
async def service_status():
    import subprocess
    try:
        result = subprocess.run(["systemctl", "status", "zua-backend.service", "--no-pager"], capture_output=True, text=True, timeout=10)
        return {"status": "ok", "action": "status", "stdout": result.stdout[:1000], "stderr": result.stderr[:1000], "returncode": result.returncode}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/v1/admin/service/{action}")
async def service_action(action: str):
    import subprocess
    if action not in ("restart", "stop"):
        return JSONResponse(status_code=400, content={"error": "无效操作: restart/stop"})
    try:
        if action == "restart":
            result = subprocess.run(["systemctl", "restart", "zua-backend.service"], capture_output=True, text=True, timeout=10)
        else:
            result = subprocess.run(["systemctl", "stop", "zua-backend.service"], capture_output=True, text=True, timeout=10)
        return {"status": "ok", "action": action, "stdout": result.stdout[:1000], "stderr": result.stderr[:1000], "returncode": result.returncode}
    except subprocess.TimeoutExpired:
        return JSONResponse(status_code=500, content={"error": "操作超时"})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/v1/admin/system")
async def system_info():
    import subprocess
    import psutil
    import shutil
    info = {}
    try:
        # Process info
        proc = psutil.Process(os.getpid())
        info["pid"] = os.getpid()
        info["uptime_seconds"] = round(time.time() - proc.create_time(), 1)
        info["cpu_percent"] = proc.cpu_percent(interval=0.1)
        info["memory_mb"] = round(proc.memory_info().rss / 1024 / 1024, 1)
        info["threads"] = proc.num_threads()
        # System info
        info["total_memory_mb"] = round(psutil.virtual_memory().total / 1024 / 1024, 0)
        info["available_memory_mb"] = round(psutil.virtual_memory().available / 1024 / 1024, 0)
        info["memory_percent"] = psutil.virtual_memory().percent
        # Disk info
        disk = shutil.disk_usage(PROJECT_ROOT)
        info["disk_total_gb"] = round(disk.total / 1024 / 1024 / 1024, 1)
        info["disk_used_gb"] = round(disk.used / 1024 / 1024 / 1024, 1)
        info["disk_free_gb"] = round(disk.free / 1024 / 1024 / 1024, 1)
        info["disk_percent"] = round(disk.used / disk.total * 100, 1)
        # Service status
        result = subprocess.run(["systemctl", "is-active", "zua-backend.service"], capture_output=True, text=True, timeout=5)
        info["service_active"] = result.stdout.strip() == "active"
    except Exception as e:
        info["error"] = str(e)
    return info


@app.post("/v1/admin/database/query")
async def database_query(request: Request):
    body = await request.json()
    query = body.get("sql", "").strip()
    if not query:
        return JSONResponse(status_code=400, content={"error": "SQL 不能为空"})
    if not query.upper().startswith("SELECT") and not query.upper().startswith("SHOW") and not query.upper().startswith("DESC"):
        return JSONResponse(status_code=400, content={"error": "仅允许 SELECT/SHOW/DESC 查询"})
    if not sql_engine:
        return JSONResponse(status_code=500, content={"error": "MySQL 连接未初始化"})
    try:
        with sql_engine.connect() as conn:
            result = conn.execute(text(query))
            rows = result.fetchall()
            columns = list(result.keys()) if rows else []
            data = []
            for row in rows[:500]:
                data.append({col: str(v) if v is not None else None for col, v in zip(columns, row)})
            return {"columns": columns, "rows": data, "total": len(data), "truncated": len(rows) > 500}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/v1/admin/database/tables")
async def database_tables():
    if not sql_engine:
        return JSONResponse(status_code=500, content={"error": "MySQL 连接未初始化"})
    try:
        with sql_engine.connect() as conn:
            result = conn.execute(text("SHOW TABLES"))
            tables = [row[0] for row in result.fetchall()]
            table_info = {}
            for t in tables:
                try:
                    count_result = conn.execute(text(f"SELECT COUNT(*) FROM `{t}`"))
                    table_info[t] = count_result.scalar()
                except Exception:
                    table_info[t] = -1
            return {"tables": table_info}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


# ==========================================
# 9. 文件管理与向量库重建 API
# ==========================================
import re as _re

DATA_MD_PATH = os.path.join(PROJECT_ROOT, "data", "data.md")
ENV_PATH = os.path.join(PROJECT_ROOT, ".env")
CONFIG_PY_PATH = os.path.join(PROJECT_ROOT, "utils", "config.py")


@app.get("/v1/admin/files/data_md")
async def get_data_md():
    try:
        with open(DATA_MD_PATH, "r", encoding="utf-8") as f:
            content = f.read()
        return {
            "content": content,
            "lines": len(content.splitlines()),
            "h2_count": len(_re.findall(r"^## ", content, flags=_re.MULTILINE)),
            "size_kb": round(len(content.encode("utf-8")) / 1024, 1),
        }
    except FileNotFoundError:
        return JSONResponse(status_code=404, content={"error": "data.md 不存在"})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.put("/v1/admin/files/data_md")
async def update_data_md(request: Request):
    body = await request.json()
    content = body.get("content", "")
    if not content.strip():
        return JSONResponse(status_code=400, content={"error": "内容不能为空"})
    try:
        with open(DATA_MD_PATH, "w", encoding="utf-8") as f:
            f.write(content)
        return {"status": "ok", "lines": len(content.splitlines())}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/v1/admin/files/upload")
async def upload_data_md(file: UploadFile = FastAPIFile(...)):
    if not file.filename or not file.filename.endswith(".md"):
        return JSONResponse(status_code=400, content={"error": "只支持 .md 文件"})
    try:
        content = await file.read()
        text = content.decode("utf-8")
        with open(DATA_MD_PATH, "w", encoding="utf-8") as f:
            f.write(text)
        return {"status": "ok", "filename": file.filename, "size": len(text)}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/v1/admin/rebuild_vectors")
async def rebuild_vectors():
    import subprocess
    project_dir = str(_Path(__file__).resolve().parent.parent)
    try:
        result = subprocess.run(
            ["python3", "-m", "utils.build_vector_db"],
            capture_output=True, text=True, timeout=600,
            cwd=project_dir,
            env={**os.environ, "PYTHONPATH": project_dir},
        )
        if result.returncode == 0:
            global policy_table
            try:
                policy_table = db.open_table(TABLE_NAME)
            except Exception:
                policy_table = None
            return {
                "status": "ok",
                "output": result.stdout[-2000:],
            }
        else:
            return JSONResponse(
                status_code=500,
                content={"error": result.stderr[-500:] or result.stdout[-500:]}
            )
    except subprocess.TimeoutExpired:
        return JSONResponse(status_code=500, content={"error": "向量化超时 (10分钟)"})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


# ==========================================
# 8. 管理面板
# ==========================================
ADMIN_CSS = """
:root{--bg:#0f172a;--surface:#1e293b;--border:#334155;--text:#f1f5f9;--muted:#94a3b8;--accent:#3b82f6;--green:#22c55e;--red:#ef4444;--amber:#f59e0b;--purple:#a855f7}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:system-ui,-apple-system,sans-serif;background:var(--bg);color:var(--text);min-height:100vh}
.login-overlay{position:fixed;inset:0;background:var(--bg);display:flex;align-items:center;justify-content:center;z-index:100}
.login-box{background:var(--surface);padding:2rem;border-radius:1rem;width:100%;max-width:380px;text-align:center}
.login-box h2{margin-bottom:1.5rem}
.login-box input{width:100%;padding:.75rem 1rem;border:1px solid var(--border);border-radius:.5rem;background:var(--bg);color:var(--text);font-size:1rem;margin-bottom:1rem}
.login-box button{width:100%;padding:.75rem;border:none;border-radius:.5rem;background:var(--accent);color:#fff;font-size:1rem;cursor:pointer}
.panel{max-width:1200px;margin:0 auto;padding:1.5rem}
.header{display:flex;justify-content:space-between;align-items:center;margin-bottom:1rem;flex-wrap:wrap;gap:.5rem}
.header h1{font-size:1.5rem}
.header-btns{display:flex;gap:.5rem;flex-wrap:wrap}
.header-btns button{padding:.4rem .8rem;border:1px solid var(--border);border-radius:.5rem;background:transparent;color:var(--text);cursor:pointer;font-size:.8rem}
.header-btns button:hover{background:var(--surface)}
.tabs{display:flex;gap:.25rem;margin-bottom:1.5rem;border-bottom:2px solid var(--border)}
.tab-btn{padding:.6rem 1rem;border:none;background:transparent;color:var(--muted);cursor:pointer;font-size:.85rem;border-bottom:2px solid transparent;margin-bottom:-2px;white-space:nowrap}
.tab-btn:hover{color:var(--text)}
.tab-btn.active{color:var(--accent);border-bottom-color:var(--accent)}
.tab-page{display:none}
.tab-page.active{display:block}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:1rem;margin-bottom:1.5rem}
.card{background:var(--surface);border:1px solid var(--border);border-radius:.75rem;padding:1.25rem}
.card h3{font-size:.8rem;color:var(--muted);margin-bottom:.5rem;text-transform:uppercase;letter-spacing:.05em}
.card .value{font-size:2rem;font-weight:700}
.card .value.green{color:var(--green)}.card .value.amber{color:var(--amber)}.card .value.red{color:var(--red)}
.section{background:var(--surface);border:1px solid var(--border);border-radius:.75rem;padding:1.25rem;margin-bottom:1rem}
.section h2{font-size:1.1rem;margin-bottom:1rem;padding-bottom:.5rem;border-bottom:1px solid var(--border)}
table{width:100%;border-collapse:collapse}
th,td{padding:.5rem .75rem;text-align:left;border-bottom:1px solid var(--border);font-size:.9rem}
th{color:var(--muted);font-weight:600}
.session-row{cursor:pointer}
.session-row:hover td{background:rgba(59,130,246,.08)}
.intent-tag{display:inline-block;padding:.15rem .5rem;border-radius:.25rem;font-size:.75rem;font-weight:600}
.intent-tag.SCORE{background:#065f46;color:#6ee7b7}.intent-tag.POLICY{background:#1e3a5f;color:#93c5fd}
.intent-tag.MAJOR{background:#4c1d95;color:#c4b5fd}.intent-tag.OTHER{background:#444;color:#aaa}
.source-tag{display:inline-block;padding:.15rem .5rem;border-radius:.25rem;font-size:.75rem;background:#374151;color:#d1d5db}
.modal{position:fixed;inset:0;background:rgba(0,0,0,.6);display:flex;align-items:center;justify-content:center;z-index:50;opacity:0;pointer-events:none;transition:opacity .2s}
.modal.active{opacity:1;pointer-events:auto}
.modal-content{background:var(--surface);border:1px solid var(--border);border-radius:1rem;width:95%;max-width:800px;max-height:85vh;display:flex;flex-direction:column}
.modal-header{display:flex;justify-content:space-between;align-items:center;padding:1rem 1.5rem;border-bottom:1px solid var(--border)}
.modal-header h3{font-size:1rem}
.modal-close{background:none;border:none;color:var(--muted);font-size:1.5rem;cursor:pointer;line-height:1}
.modal-close:hover{color:var(--text)}
.modal-body{padding:1.5rem;overflow-y:auto;flex:1}
.turn-card{background:var(--bg);border:1px solid var(--border);border-radius:.5rem;padding:1rem;margin-bottom:1rem}
.turn-card h4{font-size:.85rem;color:var(--accent);margin-bottom:.5rem}
.turn-card p{font-size:.9rem;line-height:1.6;margin-bottom:.5rem}
.turn-card .meta{display:flex;gap:.5rem;margin-bottom:.75rem;flex-wrap:wrap}
.turn-card .answer{background:var(--surface);border-left:3px solid var(--accent);padding:.75rem;border-radius:0 .5rem .5rem 0;font-size:.85rem;line-height:1.5;white-space:pre-wrap;max-height:200px;overflow-y:auto}
.turn-card .ctx{background:#1a1a2e;border:1px solid var(--border);padding:.75rem;border-radius:.5rem;font-family:monospace;font-size:.75rem;white-space:pre-wrap;max-height:150px;overflow-y:auto;color:#94a3b8}
.status-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:.75rem}
.status-item{display:flex;align-items:center;gap:.5rem;font-size:.9rem}
.dot{width:8px;height:8px;border-radius:50%}.dot.green{background:var(--green)}.dot.red{background:var(--red)}.dot.amber{background:var(--amber)}
.danger-btn{padding:.5rem 1rem;border:1px solid var(--red);border-radius:.5rem;background:transparent;color:var(--red);cursor:pointer;font-size:.85rem}
.danger-btn:hover{background:var(--red);color:#fff}
.primary-btn{padding:.5rem 1rem;border:none;border-radius:.5rem;background:var(--accent);color:#fff;cursor:pointer;font-size:.85rem}
.primary-btn:hover{opacity:.9}
.upload-btn,.tab-link-btn{padding:.5rem 1rem;border:1px solid var(--amber);border-radius:.5rem;background:transparent;color:var(--amber);cursor:pointer;font-size:.85rem}
.upload-btn:hover,.tab-link-btn:hover{background:var(--amber);color:#000}
.rebuild-btn{padding:.5rem 1rem;border:1px solid var(--green);border-radius:.5rem;background:transparent;color:var(--green);cursor:pointer;font-size:.85rem}
.rebuild-btn:hover{background:var(--green);color:#fff}
.rebuild-btn:disabled{opacity:.5;cursor:not-allowed}
.editor-toolbar{display:flex;gap:.5rem;align-items:center;padding:.75rem 1rem;border-bottom:1px solid var(--border)}
.editor-toolbar .info{font-size:.85rem;color:var(--muted);flex:1}
.editor-container{width:95%;max-width:1000px;height:80vh}
.editor-container textarea{width:100%;height:calc(100% - 50px);background:var(--bg);color:var(--text);border:none;padding:1rem;font-family:monospace;font-size:.85rem;line-height:1.6;resize:none;outline:none}
.editor-container textarea:focus{background:#131a2e}
.progress-overlay{position:absolute;inset:0;background:rgba(0,0,0,.7);display:flex;align-items:center;justify-content:center;border-radius:1rem;z-index:10}
.progress-overlay .spinner{width:40px;height:40px;border:4px solid var(--border);border-top-color:var(--accent);border-radius:50%;animation:spin 1s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.hidden{display:none!important}
.log-line{font-family:monospace;font-size:.78rem;padding:2px 0;border-bottom:1px solid rgba(51,65,85,.3);line-height:1.4}
.log-line .ts{color:var(--muted);margin-right:.5rem}
.log-line.error{color:var(--red)}
.log-line.warning{color:var(--amber)}
.sql-editor{width:100%;min-height:120px;background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:.5rem;padding:.75rem;font-family:monospace;font-size:.85rem;resize:vertical;outline:none}
.sql-editor:focus{border-color:var(--accent)}
.db-tables{display:flex;gap:.5rem;flex-wrap:wrap;margin-bottom:1rem}
.db-tables button{padding:.3rem .7rem;border:1px solid var(--border);border-radius:.25rem;background:transparent;color:var(--muted);cursor:pointer;font-size:.8rem}
.db-tables button:hover{background:var(--surface);color:var(--text)}
.form-group{margin-bottom:1rem}
.form-group label{display:block;font-size:.85rem;color:var(--muted);margin-bottom:.3rem}
.form-group input{width:100%;padding:.6rem .8rem;border:1px solid var(--border);border-radius:.5rem;background:var(--bg);color:var(--text);font-size:.9rem}
"""

ADMIN_JS = """
const SESSION_KEY='zua_admin_auth';
const API='/v1/stats';
const SAPI='/v1/admin/sessions';
const HAPI='/v1/health';
const LOGAPI='/v1/admin/logs';
const SYSAPI='/v1/admin/system';
const SVCAPI='/v1/admin/service';
const PWDAPI='/v1/admin/password';
const ENVAPI='/v1/admin/files/env';
const CFGAPI='/v1/admin/files/config_py';
const FAPI='/v1/admin/files/data_md';
const RAPI='/v1/admin/rebuild_vectors';
const DBAPI='/v1/admin/database/query';
const TBLAPI='/v1/admin/database/tables';
async function doLogin(){const p=document.getElementById('pwd').value;if(!p)return;try{const r=await fetch('/v1/admin/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:p})});const d=await r.json();if(d.ok){localStorage.setItem(SESSION_KEY,'1');showPanel()}else{document.getElementById('err').textContent='密码错误'}}catch(e){document.getElementById('err').textContent='登录请求失败'}}
function showPanel(){document.getElementById('login').classList.add('hidden');document.getElementById('panel').classList.remove('hidden');refresh();loadHealth();loadFileInfo();loadSystemInfo();loadTables();switchTab('stats')}
function doLogout(){localStorage.removeItem(SESSION_KEY);location.reload()}
function switchTab(name){document.querySelectorAll('.tab-btn').forEach(b=>b.classList.remove('active'));document.querySelectorAll('.tab-page').forEach(p=>p.classList.remove('active'));const btn=document.querySelector('.tab-btn[data-tab="'+name+'"]');const pg=document.getElementById('page_'+name);if(btn)btn.classList.add('active');if(pg)pg.classList.add('active');if(name==='logs')loadLogs();if(name==='system')loadSystemInfo();if(name==='service')loadServiceStatus()}
async function refresh(){try{const r=await fetch(API);const d=await r.json();document.getElementById('total').textContent=d.total;document.getElementById('hitRate').textContent=d.hit_rate+'%';document.getElementById('missed').textContent=d.missed;const st=document.getElementById('_sTotal');if(st)st.textContent=d.total||'-';const ib=document.getElementById('intents');ib.innerHTML='';d.top_intents.forEach(function(x){var tr=document.createElement('tr');var td1=document.createElement('td');td1.textContent=x.intent;var td2=document.createElement('td');td2.textContent=x.count;tr.appendChild(td1);tr.appendChild(td2);ib.appendChild(tr)});const mb=document.getElementById('missedTable');mb.innerHTML='';d.missed_queries.forEach(function(x){var tr=document.createElement('tr');var t1=document.createElement('td');t1.textContent=x.timestamp;var t2=document.createElement('td');t2.textContent=x.intent;var t3=document.createElement('td');t3.textContent=x.query;tr.appendChild(t1);tr.appendChild(t2);tr.appendChild(t3);mb.appendChild(tr)})}catch(e){}loadSessions()}
async function loadSessions(){try{const r=await fetch(SAPI);const d=await r.json();const st=document.getElementById('_sTotal');if(st)st.textContent=d.total;const tb=document.getElementById('sessionsTable');if(!tb)return;tb.innerHTML='';d.sessions.forEach(function(s){var tr=document.createElement('tr');tr.className='session-row';tr.setAttribute('data-sid',s.session_id);var t1=document.createElement('td');t1.textContent=s.last_active;var t2=document.createElement('td');t2.textContent=s.query_count+'条';var t3=document.createElement('td');t3.textContent=s.first_query;var t4=document.createElement('td');var span4=document.createElement('span');span4.className='intent-tag '+s.last_intent;span4.textContent=s.last_intent;t4.appendChild(span4);var t5=document.createElement('td');var span5=document.createElement('span');span5.className='source-tag';span5.textContent=s.last_source;t5.appendChild(span5);tr.appendChild(t1);tr.appendChild(t2);tr.appendChild(t3);tr.appendChild(t4);tr.appendChild(t5);tb.appendChild(tr)});document.querySelectorAll('.session-row').forEach(function(row){row.addEventListener('click',function(){showSession(this.getAttribute('data-sid'))})})}catch(e){}}
async function showSession(sid){try{const r=await fetch(SAPI+'/'+sid);const d=await r.json();if(d.error){alert(d.error);return}document.getElementById('mSid').textContent=sid;document.getElementById('mTime').textContent='创建于 '+d.created+' | 最近活跃 '+d.last_active;const body=document.getElementById('mBody');body.innerHTML='';if(!d.turns||d.turns.length===0){body.innerHTML='<p>暂无对话记录</p>';document.getElementById('modal').classList.add('active');return}d.turns.forEach(function(t,i){var card=document.createElement('div');card.className='turn-card';var h4=document.createElement('h4');h4.textContent='第'+(i+1)+'轮对话';card.appendChild(h4);var meta=document.createElement('div');meta.className='meta';var is=document.createElement('span');is.className='intent-tag '+t.intent;is.textContent=t.intent;meta.appendChild(is);var ss=document.createElement('span');ss.className='source-tag';ss.textContent='数据源: '+t.source;meta.appendChild(ss);card.appendChild(meta);var qp=document.createElement('p');qp.innerHTML='<strong>问：</strong>';var qt=document.createElement('span');qt.textContent=t.query;qp.appendChild(qt);card.appendChild(qp);var ap=document.createElement('div');ap.className='answer';ap.innerHTML='<strong>答：</strong>';var at=document.createElement('span');at.textContent=t.answer;ap.appendChild(at);card.appendChild(ap);var cp=document.createElement('p');cp.style.marginTop='.5rem';cp.innerHTML='<strong>检索上下文摘要：</strong>';card.appendChild(cp);var cv=document.createElement('div');cv.className='ctx';cv.textContent=t.context_snippet;card.appendChild(cv);var rp=document.createElement('p');rp.style.marginTop='.5rem';rp.style.fontSize='.8rem';rp.style.color='#64748b';rp.textContent='检索字符数: '+t.retrieved_chars;card.appendChild(rp);body.appendChild(card)});document.getElementById('modal').classList.add('active')}catch(e){alert('加载失败')}}
function closeModal(){document.getElementById('modal').classList.remove('active')}
async function doClear(){await fetch(API,{method:'DELETE'});refresh()}
async function doClearSessions(){if(!confirm('确定清空所有会话记录？'))return;await fetch(SAPI,{method:'DELETE'});loadSessions()}
async function loadHealth(){try{const r=await fetch(HAPI);const d=await r.json();setDot('mysqlDot',d.mysql==='ok');setDot('neo4jDot',d.neo4j==='ok');setDot('adminDot',d.admin_password==='set')}catch(e){}}
function setDot(id,ok){const el=document.getElementById(id);if(el){el.className='dot'+(ok?' green':' red')}}
async function loadLogs(){const sel=document.getElementById('logLevel');const cnt=document.getElementById('logCount');try{const r=await fetch(LOGAPI+'?level='+sel.value+'&limit='+cnt.value);const d=await r.json();const box=document.getElementById('logBox');box.innerHTML='';if(!d.logs||d.logs.length===0){box.innerHTML='<p style=color:var(--muted)>无日志记录</p>';return}d.logs.forEach(function(l){var div=document.createElement('div');div.className='log-line'+(l.level==='error'?' error':(l.level==='warning'?' warning':''));var ts=document.createElement('span');ts.className='ts';ts.textContent=l.timestamp;div.appendChild(ts);var msg=document.createElement('span');msg.textContent=l.message;div.appendChild(msg);box.appendChild(div)})}catch(e){document.getElementById('logBox').innerHTML='<p style=color:var(--red)>加载失败</p>'}}
async function loadSystemInfo(){try{const r=await fetch(SYSAPI);const d=await r.json();if(d.error){document.getElementById('sysInfo').innerHTML='<p style=color:var(--red)>'+d.error+'</p>';return}var fmtUptime=function(s){var h=Math.floor(s/3600);var m=Math.floor((s%3600)/60);return h+'小时 '+m+'分钟'};var g=document.getElementById('sysGrid');g.innerHTML='';var items=[['PID',d.pid],['运行时间',fmtUptime(d.uptime_seconds)],['CPU 占用',d.cpu_percent+'%'],['进程内存',d.memory_mb+' MB'],['总内存',d.total_memory_mb+' MB'],['可用内存',d.available_memory_mb+' MB'],['内存使用率',d.memory_percent+'%'],['磁盘总计',d.disk_total_gb+' GB'],['磁盘已用',d.disk_used_gb+' GB'],['磁盘剩余',d.disk_free_gb+' GB'],['磁盘使用率',d.disk_percent+'%'],['服务状态',d.service_active?'正常运行':'异常']];items.forEach(function(x){var item=document.createElement('div');item.className='status-item';var dot=document.createElement('span');dot.className='dot'+(x[1]==='正常'||x[0]==='服务状态'?(d.service_active?' green':' red'):'');if(dot.className==='dot')dot.className='dot amber';item.appendChild(dot);var label=document.createElement('span');label.style.flex='1';label.textContent=x[0];item.appendChild(label);var val=document.createElement('strong');val.textContent=x[1];item.appendChild(val);g.appendChild(item)})}catch(e){document.getElementById('sysInfo').innerHTML='<p style=color:var(--red)>加载失败</p>'}}
async function loadServiceStatus(){try{const r=await fetch(SVCAPI+'/status');const d=await r.json();const el=document.getElementById('svcStatus');el.textContent=d.stdout||'无法获取状态';const active=d.stdout&&d.stdout.indexOf('active')>=0;document.getElementById('svcDot').className='dot'+(active?' green':' red');document.getElementById('svcLabel').textContent=active?'服务正常运行':'服务异常'}catch(e){document.getElementById('svcStatus').innerHTML='<p style=color:var(--red)>加载失败</p>'}}
async function svcDo(action){if(action==='stop'&&!confirm('确定停止服务？网站将立即中断！'))return;if(action==='restart'&&!confirm('确定重启服务？将有几秒钟不可用。'))return;const btn=document.getElementById('svc_'+action+'Btn');btn.disabled=true;btn.textContent='操作中...';try{const r=await fetch(SVCAPI+'/'+action,{method:'POST'});const d=await r.json();if(action==='stop'){document.getElementById('svcStatus').innerHTML='<p style=color:var(--amber)>服务已停止。请手动重启。</p>'}else if(action==='restart'){setTimeout(()=>loadServiceStatus(),3000);document.getElementById('svcStatus').innerHTML='<p style=color:var(--amber)>服务重启中，3秒后刷新状态...</p>'}else{loadServiceStatus()}}catch(e){alert('操作失败')}finally{btn.disabled=false;btn.textContent=action==='restart'?'重启服务':action==='stop'?'停止服务':'查看状态'}}
async function changePassword(){const p=document.getElementById('newPwd').value;const p2=document.getElementById('confirmPwd').value;if(!p){alert('请输入新密码');return}if(p!==p2){alert('两次输入不一致');return}if(p.length<6){alert('密码至少6个字符');return}try{const r=await fetch(PWDAPI,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:p})});const d=await r.json();if(d.error){alert(d.error);return}else{alert('密码已更新');document.getElementById('newPwd').value='';document.getElementById('confirmPwd').value=''}}catch(e){alert('请求失败')}}
async function loadEnvFile(){const ta=document.getElementById('envEditor');const info=document.getElementById('envInfo');try{const r=await fetch(ENVAPI);const d=await r.json();ta.value=d.content;info.textContent=d.exists?(d.path+' ('+d.lines+'行)'):'文件不存在: '+d.path}catch(e){info.textContent='加载失败'}}
async function saveEnvFile(){const c=document.getElementById('envEditor').value;try{const r=await fetch(ENVAPI,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({content:c})});const d=await r.json();if(d.error){alert(d.error);return}else{alert('已保存 ('+d.lines+'行)');loadEnvFile()}}catch(e){alert('请求失败')}}
async function loadConfigFile(){const ta=document.getElementById('configEditor');const info=document.getElementById('configInfo');try{const r=await fetch(CFGAPI);const d=await r.json();if(d.error){info.textContent=d.error;ta.value='';return}ta.value=d.content;info.textContent=d.path+' ('+d.lines+'行, '+d.size_kb+'KB)'}catch(e){info.textContent='加载失败'}}
async function saveConfigFile(){const c=document.getElementById('configEditor').value;try{const r=await fetch(CFGAPI,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({content:c})});const d=await r.json();if(d.error){alert(d.error);return}else{alert('已保存 ('+d.lines+'行)')}}catch(e){alert('请求失败')}}
async function loadFileInfo(){try{const r=await fetch(FAPI);const d=await r.json();if(d.error){document.getElementById('mdInfo').textContent=d.error;return}document.getElementById('mdInfo').textContent=d.lines+'行 | '+d.h2_count+'二级标题 | '+d.size_kb+'KB'}catch(e){}}
function openEditor(){fetch(FAPI).then(function(r){return r.json()}).then(function(d){if(d.error){alert(d.error);return}document.getElementById('eContent').value=d.content;document.getElementById('editorModal').classList.add('active');document.getElementById('eInfo').textContent=d.lines+'行 | 加载中...'})}
function closeEditor(){document.getElementById('editorModal').classList.remove('active')}
function saveEditor(){var c=document.getElementById('eContent').value;fetch(FAPI,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({content:c})}).then(function(r){return r.json()}).then(function(d){if(d.error){alert(d.error);return}else{alert('已保存 ('+d.lines+'行)');closeEditor();loadFileInfo()}})}
function uploadFile(el){if(!el.files.length)return;var f=el.files[0];if(!f.name.endsWith('.md')){alert('只支持 .md 文件');return}var fd=new FormData();fd.append('file',f);fetch('/v1/admin/files/upload',{method:'POST',body:fd}).then(function(r){return r.json()}).then(function(d){if(d.error){alert(d.error);return}else{alert('已替换: '+d.filename+' ('+Math.round(d.size/1024)+'KB)');loadFileInfo()}});el.value=''}
function rebuildVectors(){if(!confirm('确定要重新构建向量数据库吗？这可能需要几分钟。'))return;var btn=document.getElementById('rebuildBtn');btn.disabled=true;btn.textContent='构建中...';var sec=btn.closest('.section');var ov=document.createElement('div');ov.className='progress-overlay';ov.innerHTML='<div style=text-align:center><div class=spinner></div><p style=margin-top:.75rem;font-size:.85rem>正在重建向量数据库...</p></div>';sec.style.position='relative';sec.appendChild(ov);fetch(RAPI,{method:'POST'}).then(function(r){return r.json()}).then(function(d){ov.remove();btn.disabled=false;btn.textContent='更新向量库';if(d.error){alert('重建失败: '+d.error)}else{alert('重建完成!');loadFileInfo()}}).catch(function(e){ov.remove();btn.disabled=false;btn.textContent='更新向量库';alert('请求失败: '+e)})}
async function loadTables(){try{const r=await fetch(TBLAPI);const d=await r.json();if(d.error){document.getElementById('tblList').innerHTML='<p style=color:var(--red)>'+d.error+'</p>';return}const box=document.getElementById('tblList');box.innerHTML='';if(!d.tables||Object.keys(d.tables).length===0){box.innerHTML='<p style=color:var(--muted)>无数据表</p>';return}Object.keys(d.tables).forEach(function(name){var btn=document.createElement('button');btn.textContent=name+' ('+d.tables[name]+'行)';btn.onclick=function(){document.getElementById('sqlInput').value='SELECT * FROM `'+name+'` LIMIT 10;'};box.appendChild(btn)})}catch(e){}}
async function runSql(){const sql=document.getElementById('sqlInput').value;if(!sql.trim()){alert('请输入SQL');return}document.getElementById('sqlResult').innerHTML='<p style=color:var(--muted)>执行中...</p>';try{const r=await fetch(DBAPI,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({sql:sql})});const d=await r.json();if(d.error){document.getElementById('sqlResult').innerHTML='<p style=color:var(--red)>'+d.error+'</p>';return}if(!d.rows||d.rows.length===0){document.getElementById('sqlResult').innerHTML='<p style=color:var(--muted)>查询结果为空 ('+d.total+'行)</p>';return}var table=document.createElement('table');var thead=document.createElement('thead');var hr=document.createElement('tr');d.columns.forEach(function(c){var th=document.createElement('th');th.textContent=c;hr.appendChild(th)});thead.appendChild(hr);table.appendChild(thead);var tbody=document.createElement('tbody');d.rows.forEach(function(row){var tr=document.createElement('tr');d.columns.forEach(function(c){var td=document.createElement('td');td.textContent=row[c]||'';tr.appendChild(td)});tbody.appendChild(tr)});table.appendChild(tbody);var container=document.getElementById('sqlResult');container.innerHTML='';container.appendChild(table);if(d.truncated){var p=document.createElement('p');p.style.color='var(--amber)';p.style.marginTop='.5rem';p.textContent='结果已截断，仅显示前500行';container.appendChild(p)}}catch(e){document.getElementById('sqlResult').innerHTML='<p style=color:var(--red)>查询失败: '+e+'</p>'}}
document.addEventListener('DOMContentLoaded',function(){if(localStorage.getItem(SESSION_KEY))showPanel();else document.getElementById('login').classList.remove('hidden')});
setInterval(refresh,30000);
"""

ADMIN_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ZUA 招生管理面板</title>
<style>{ADMIN_CSS}</style>
</head>
<body>
<div id="login" class="login-overlay hidden">
<div class="login-box">
<h2>管理面板登录</h2>
<input type="password" id="pwd" placeholder="输入管理员密码" onkeydown="if(event.key==='Enter')doLogin()">
<button onclick="doLogin()">登录</button>
<p id="err" style="color:var(--red);margin-top:.5rem;font-size:.85rem"></p>
</div>
</div>
<div id="panel" class="panel hidden">
<div class="header">
<h1>ZUA 招生管理面板</h1>
<div class="header-btns">
<button onclick="doLogout()">退出</button>
</div>
</div>

<!-- Tab Navigation -->
<div class="tabs">
<button class="tab-btn" data-tab="stats" onclick="switchTab('stats')">数据统计</button>
<button class="tab-btn" data-tab="sessions" onclick="switchTab('sessions')">会话记录</button>
<button class="tab-btn" data-tab="logs" onclick="switchTab('logs')">系统日志</button>
<button class="tab-btn" data-tab="files" onclick="switchTab('files')">文件管理</button>
<button class="tab-btn" data-tab="database" onclick="switchTab('database')">数据库</button>
<button class="tab-btn" data-tab="system" onclick="switchTab('system')">系统状态</button>
<button class="tab-btn" data-tab="service" onclick="switchTab('service')">服务控制</button>
<button class="tab-btn" data-tab="security" onclick="switchTab('security')">安全设置</button>
</div>

<!-- Tab 1: Stats -->
<div id="page_stats" class="tab-page">
<div style="display:flex;gap:.5rem;margin-bottom:1rem">
<button class="primary-btn" onclick="refresh()">刷新统计</button>
<button class="primary-btn" onclick="loadHealth()">刷新状态</button>
<button class="danger-btn" style="padding:.4rem .8rem;font-size:.8rem" onclick="if(confirm('确定清空所有反馈数据？'))doClear()">清空反馈数据</button>
</div>
<div class="cards">
<div class="card"><h3>总提问数</h3><div class="value" id="total">-</div></div>
<div class="card"><h3>命中率</h3><div class="value green" id="hitRate">-</div></div>
<div class="card"><h3>未命中数</h3><div class="value red" id="missed">-</div></div>
<div class="card"><h3>活跃会话</h3><div class="value amber" id="sessions">-</div><span id="_sTotal" style="display:none"></span></div>
</div>
<div class="section">
<h2>热门意图</h2>
<table><thead><tr><th>意图</th><th>次数</th></tr></thead><tbody id="intents"></tbody></table>
</div>
<div class="section">
<h2>未命中查询 (最近 50 条)</h2>
<table><thead><tr><th>时间</th><th>意图</th><th>查询内容</th></tr></thead><tbody id="missedTable"></tbody></table>
</div>
<div class="section">
<h2>系统状态</h2>
<div class="status-grid">
<div class="status-item"><span class="dot green"></span>LanceDB: 已连接</div>
<div class="status-item"><span class="dot" id="mysqlDot"></span><span id="mysqlStatus">MySQL</span></div>
<div class="status-item"><span class="dot" id="neo4jDot"></span><span id="neo4jStatus">Neo4j</span></div>
<div class="status-item"><span class="dot" id="adminDot"></span>管理面板密码</div>
</div>
</div>
</div>

<!-- Tab 2: Sessions -->
<div id="page_sessions" class="tab-page">
<div class="section">
<h2>会话记录 (24h 自动清除) <span style="float:right;font-size:.85rem;color:var(--muted)"><button class="danger-btn" style="padding:.25rem .75rem;font-size:.8rem" onclick="doClearSessions()">清空全部</button></span></h2>
<table><thead><tr><th>最后活跃</th><th>轮数</th><th>首次提问</th><th>意图</th><th>数据源</th></tr></thead><tbody id="sessionsTable"><tr><td colspan="5" style="color:var(--muted)">加载中...</td></tr></tbody></table>
</div>
</div>

<!-- Tab 3: System Logs -->
<div id="page_logs" class="tab-page">
<div class="section">
<h2>系统日志查看器</h2>
<div style="display:flex;gap:.75rem;align-items:center;margin-bottom:1rem;flex-wrap:wrap">
<label style="font-size:.85rem;color:var(--muted)">级别:</label>
<select id="logLevel" style="padding:.3rem .5rem;border:1px solid var(--border);border-radius:.25rem;background:var(--bg);color:var(--text);font-size:.85rem">
<option value="info">INFO</option>
<option value="warning">WARNING</option>
<option value="error">ERROR</option>
<option value="all">ALL</option>
</select>
<label style="font-size:.85rem;color:var(--muted);margin-left:.5rem">行数:</label>
<select id="logCount" style="padding:.3rem .5rem;border:1px solid var(--border);border-radius:.25rem;background:var(--bg);color:var(--text);font-size:.85rem">
<option value="50">50</option>
<option value="100" selected>100</option>
<option value="200">200</option>
<option value="500">500</option>
</select>
<button class="primary-btn" onclick="loadLogs()">加载日志</button>
</div>
<div id="logBox" style="background:var(--bg);border:1px solid var(--border);border-radius:.5rem;padding:.75rem;max-height:60vh;overflow-y:auto"></div>
</div>
</div>

<!-- Tab 4: File Management -->
<div id="page_files" class="tab-page">
<div class="section">
<h2>data.md <span style="float:right;font-size:.85rem;color:var(--muted)"><span id="mdInfo">加载中...</span></span></h2>
<div style="display:flex;gap:.75rem;margin-top:.75rem;flex-wrap:wrap">
<button class="primary-btn" onclick="openEditor()">在线编辑</button>
<button class="upload-btn" onclick="document.getElementById('fileInput').click()">上传替换</button>
<input type="file" id="fileInput" accept=".md" style="display:none" onchange="uploadFile(this)">
<button class="rebuild-btn" id="rebuildBtn" onclick="rebuildVectors()">更新向量库</button>
</div>
</div>
<div class="section">
<h2>.env 配置文件</h2>
<div style="display:flex;gap:.5rem;margin-bottom:.75rem">
<button class="primary-btn" onclick="loadEnvFile()">加载文件</button>
<button class="primary-btn" onclick="saveEnvFile()">保存修改</button>
<span id="envInfo" style="font-size:.85rem;color:var(--muted);align-self:center"></span>
</div>
<textarea id="envEditor" class="sql-editor" style="min-height:200px" spellcheck="false"></textarea>
</div>
<div class="section">
<h2>config.py 配置文件</h2>
<div style="display:flex;gap:.5rem;margin-bottom:.75rem">
<button class="primary-btn" onclick="loadConfigFile()">加载文件</button>
<button class="primary-btn" onclick="saveConfigFile()">保存修改</button>
<span id="configInfo" style="font-size:.85rem;color:var(--muted);align-self:center"></span>
</div>
<textarea id="configEditor" class="sql-editor" style="min-height:200px" spellcheck="false"></textarea>
</div>
</div>

<!-- Tab 5: Database -->
<div id="page_database" class="tab-page">
<div class="section">
<h2>数据库查询 (MySQL)</h2>
<div id="tblList" class="db-tables"></div>
<textarea id="sqlInput" class="sql-editor" placeholder="输入 SQL 查询，例如: SELECT * FROM table_name LIMIT 10;" spellcheck="false"></textarea>
<div style="margin-top:.75rem;display:flex;gap:.5rem">
<button class="primary-btn" onclick="runSql()">执行查询</button>
<span style="font-size:.8rem;color:var(--muted);align-self:center">仅允许 SELECT/SHOW/DESC</span>
</div>
</div>
<div class="section" id="sqlResult">
<p style="color:var(--muted)">查询结果将在此显示</p>
</div>
</div>

<!-- Tab 6: System Status -->
<div id="page_system" class="tab-page">
<div class="section">
<h2>系统监控</h2>
<div class="status-grid" id="sysGrid"></div>
<div id="sysInfo" style="margin-top:1rem"></div>
</div>
</div>

<!-- Tab 7: Service Control -->
<div id="page_service" class="tab-page">
<div class="section">
<h2>服务控制</h2>
<div class="status-item" style="margin-bottom:1rem">
<span class="dot" id="svcDot"></span>
<strong id="svcLabel">加载中...</strong>
</div>
<div id="svcStatus" style="font-size:.85rem;color:var(--muted);margin-bottom:1rem;white-space:pre-wrap"></div>
<div style="display:flex;gap:.75rem;flex-wrap:wrap">
<button class="primary-btn" id="svc_statusBtn" onclick="loadServiceStatus()">查看状态</button>
<button class="rebuild-btn" id="svc_restartBtn" onclick="svcDo('restart')">重启服务</button>
<button class="danger-btn" id="svc_stopBtn" onclick="svcDo('stop')">停止服务</button>
</div>
</div>
</div>

<!-- Tab 8: Security -->
<div id="page_security" class="tab-page">
<div class="section">
<h2>修改管理员密码</h2>
<div class="form-group">
<label>新密码</label>
<input type="password" id="newPwd" placeholder="输入新密码（至少6个字符）">
</div>
<div class="form-group">
<label>确认密码</label>
<input type="password" id="confirmPwd" placeholder="再次输入新密码">
</div>
<button class="primary-btn" onclick="changePassword()">更新密码</button>
</div>
</div>

</div>

<!-- Session Detail Modal -->
<div id="modal" class="modal">
<div class="modal-content">
<div class="modal-header">
<h3>会话详情: <span id="mSid"></span></h3>
<button class="modal-close" onclick="closeModal()">&times;</button>
</div>
<div style="padding:.75rem 1.5rem;border-bottom:1px solid var(--border);font-size:.85rem;color:var(--muted)" id="mTime"></div>
<div class="modal-body" id="mBody"></div>
</div>
</div>

<!-- Editor Modal -->
<div id="editorModal" class="modal">
<div class="modal-content editor-container">
<div class="editor-toolbar">
<span class="info" id="eInfo">data.md 在线编辑</span>
<button class="primary-btn" onclick="saveEditor()">保存</button>
<button style="padding:.5rem 1rem;border:1px solid var(--border);border-radius:.5rem;background:transparent;color:var(--muted);cursor:pointer;font-size:.85rem" onclick="closeEditor()">取消</button>
</div>
<textarea id="eContent" spellcheck="false" placeholder="加载中..."></textarea>
</div>
</div>
<script>
{ADMIN_JS}
</script>
</body>
</html>"""



@app.get("/v1/admin", response_class=HTMLResponse)
async def admin_panel():
    return ADMIN_HTML.format(ADMIN_CSS=ADMIN_CSS, ADMIN_JS=ADMIN_JS)


if __name__ == "__main__":
    import uvicorn

    print("🚀 启动 ZUA AI 招生助手后端引擎...")
    uvicorn.run(app, host="0.0.0.0", port=8012)
