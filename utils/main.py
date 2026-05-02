import os
import random
import json
import lancedb
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from openai import OpenAI
from neo4j import GraphDatabase
from sqlalchemy import create_engine, text

from config import (
    API_KEY, BASE_URL,
    CHAT_MODEL, ROUTER_MODEL, EMBEDDING_MODEL,
    LANCEDB_URI, TABLE_NAME,
    MYSQL_URI,
    NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD,
)

# ==========================================
# 1. 客户端初始化
# ==========================================
CHAT_CLIENT = OpenAI(api_key=API_KEY, base_url=BASE_URL)

# LanceDB
db = lancedb.connect(LANCEDB_URI)
try:
    policy_table = db.open_table(TABLE_NAME)
except Exception:
    print("⚠️ 警告：找不到 LanceDB 表，请确认是否已运行向量化脚本。")
    policy_table = None

# MySQL
try:
    sql_engine = create_engine(MYSQL_URI)
except Exception as e:
    print(f"⚠️ MySQL 连接初始化失败: {e}")
    sql_engine = None

# Neo4j
try:
    neo4j_driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
except Exception as e:
    print(f"⚠️ Neo4j 连接初始化失败: {e}")
    neo4j_driver = None

# FastAPI
app = FastAPI(title="ZUA AI 招生助手终极后端")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ChatRequest(BaseModel):
    model: str = "default"
    messages: list
    temperature: float = 0.2
    stream: bool = True


# ==========================================
# 2. 核心：智能意图路由 (Intent Router)
# ==========================================
def detect_intent(query: str) -> str:
    prompt = f"""
    判断以下用户问题的意图，只能输出以下四个英文单词之一，绝不输出其他字符：
    - SCORE : 询问历年录取分数、位次、最低分、能上什么专业（估分）。
    - POLICY : 询问硕士点、宿舍、食堂、军训、转专业、奖助贷政策、参军退伍、大学英语、日语、俄语、外语等校园生活指南，包括介绍学校。
    - MAJOR : 询问专业的从属学院、学制、特色、就业去向、选科要求。
    - OTHER : 闲聊寒暄，或与学校招生完全无关的问题。
    用户问题：{query}
    """
    try:
        response = CHAT_CLIENT.chat.completions.create(
            model=ROUTER_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0
        )
        intent = response.choices[0].message.content.strip().upper()
        return intent if intent in ["SCORE", "MAJOR", "POLICY", "OTHER"] else "POLICY"
    except:
        return "POLICY"


# ==========================================
# 3. 三路查询模块 (Retrievers)
# ==========================================

def retrieve_from_lancedb(query: str) -> str:
    if not policy_table: return "暂无政策数据库连接。"
    try:
        query_vec = CHAT_CLIENT.embeddings.create(input=query, model=EMBEDDING_MODEL).data[0].embedding
        results = policy_table.search(query_vec).limit(3).to_pandas()
        return "\n\n".join(results["text"].tolist())
    except Exception as e:
        return f"向量库查询失败: {e}"


def retrieve_from_mysql(query: str) -> str:
    if not sql_engine: return "暂无分数数据库连接。"

    schema_prompt = f"""
    你是一个精通 MySQL 的数据分析师。根据用户问题编写查询 SQL。
    表名: historical_scores
    字段: year(年份), province(省市), admission_type(录取类型), subject_category(科类如物理类), major_name(专业名称), enroll_count(录取人数), min_score(最低分), avg_score(平均分), max_score(最高分)

    用户问题："{query}"

    【核心铁律】
    1. 只能返回纯 SQL 语句，绝对不要包含 markdown 标记 (如 ```sql)。
    2. 必须使用 SELECT *，绝对禁止只 SELECT 单个字段！
    3. 专业名称匹配必须使用 LIKE '%关键字%'（如 LIKE '%工商管理%'）。
    4. 省份匹配必须使用模糊匹配（如 province LIKE '%北京%'）。
    5. 必须加上 LIMIT 100 防止结果过多。
    """
    try:
        response = CHAT_CLIENT.chat.completions.create(
            model=CHAT_MODEL,
            messages=[{"role": "user", "content": schema_prompt}],
            temperature=0.0
        )
        sql_query = response.choices[0].message.content.strip()
        sql_query = sql_query.replace("```sql", "").replace("```", "").strip()
        print(f"\n🛠️ 生成的 SQL: {sql_query}")

        with sql_engine.connect() as conn:
            result = conn.execute(text(sql_query))
            rows = result.fetchall()
            print(f"📦 MySQL 实际返回的行数: {len(rows)}")

            if len(rows) == 0:
                print("⚠️ 警告：SQL 执行成功，但在 MySQL 中真的返回了 0 条数据！")
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
    if not neo4j_driver: return "暂无图谱数据库连接。"

    schema_prompt = f"""
    你是一个精通 Neo4j Cypher 语言的知识图谱专家。根据用户问题编写查询语句。
    图谱 Schema:
    - 节点: College(name), Major(name, duration), Feature(name)
    - 关系: (College)-[:CONTAINS]->(Major), (Major)-[:HAS_FEATURE]->(Feature)

    用户问题："{query}"

    要求：
    1. 只能返回可以执行的纯 Cypher 语句，绝对不要包含 markdown 标记 (如 ```cypher)。
    2. 对 name 属性使用正则或 CONTAINS 进行模糊匹配。
    3. 必须返回具体的节点属性，如 RETURN c.name, m.name, f.name LIMIT 50。
    """
    try:
        response = CHAT_CLIENT.chat.completions.create(
            model=CHAT_MODEL,
            messages=[{"role": "user", "content": schema_prompt}],
            temperature=0.0
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
# 4. API 接口：最终生成与流式返回
# ==========================================
@app.post("/v1/chat/completions")
async def chat_endpoint(request: ChatRequest):
    messages = request.messages
    recent_history = messages[-7:-1] if len(messages) > 1 else []
    current_query = messages[-1]["content"]

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

    # 图片随机抽取逻辑
    image_config = {
        "canting": {"keywords": ["餐厅", "食堂", "吃饭", "美食", "饭菜"], "max": 3},
        "ditu": {"keywords": ["地图", "位置", "校区", "多大"], "max": 2},
        "fengjing": {"keywords": ["风景", "校园环境", "漂亮", "图书馆", "操场"], "max": 8},
        "qinshi": {"keywords": ["寝室", "宿舍", "几人间", "床", "空调"], "max": 5},
        "shangyejie": {"keywords": ["商业街", "超市", "购物", "买东西", "生活费"], "max": 5}
    }

    selected_folder = None
    max_imgs = 0

    for folder, config in image_config.items():
        if any(kw in current_query for kw in config["keywords"]):
            selected_folder = folder
            max_imgs = config["max"]
            break

    image_instruction = ""
    if selected_folder:
        sample_size = min(2, max_imgs)
        nums = random.sample(range(1, max_imgs + 1), sample_size)
        img_lines = [f"![{selected_folder}实拍](./{selected_folder}/{n}.jpg)" for n in nums]
        img_str = "  ".join(img_lines)
        image_instruction = f"3. 【强制要求】请务必在回答的最末尾，原样附加以下实拍图片代码，绝对不要修改路径：\n\n{img_str}\n"
    else:
        image_instruction = f"3. 【纯文本指令】本次回答【绝对禁止】输出任何形式的图片 Markdown 语法（如 `![...](...)` 或 `<img>`）。即使检索资料中含有类似 `zua_aviation_major`、`logo` 等图片标识符，也必须强制丢弃，只能输出文字！"

    system_prompt = f"""
        你是郑州航空工业管理学院（ZUA）官方 Web 端智能招生助手。
        你的回答代表学校官方立场，必须客观、严谨、不卑不亢、惜字如金。

        【核心铁律】
        1. 必须 100% 基于以下检索到的官方资料回答问题(最高优先级)。严禁你利用自己的预训练知识自行编造、猜测分数或政策。这一条最重要不要忘记，不要编造。
        2. 结构化排版：遇到多条分数对比，必须使用 Markdown 表格输出；遇到多条特色介绍，必须使用 Markdown 无序列表。
        3. 如果下方提供了图片 Markdown 代码，请务必在回答的最后原样输出它，不要修改图片路径。
    {image_instruction}
        4. 如果背景资料中提示"未查询到相关记录"，回复："抱歉，本助手暂未查到该信息..."。
        5. 当问道航空特色专业时回答"郑航王牌航空特色：飞行器设计与工程（河南唯一航空国家级一流）、飞行器动力工程、无人驾驶航空器系统工程、交通运输（空管签派）、飞行器适航技术。聚焦军机、民航、无人机研发运维，对接中航、商飞、民航岗位，航空管工融合，省内航空赛道顶尖，就业对口稳定。"。
        6. 无关话题强制回复："本助手仅限解答郑州航空工业管理学院招生相关问题。"
        7.反图片输出机制（极高优先级）：除了上述第3点强制要求的本地实拍图片外，【绝对禁止】在回答中输出任何其他的 Markdown 图片语法（即 `![...](...)`）比如"![郑州航空工业管理学院校徽](https://zua.edu.cn/images/logo.png)"。即使检索到的背景资料中带有网络图片链接（如校徽等），你也必须在回答时将其静默过滤掉！
        8.当检测到keyword中的风景、校园环境、商业街、超市这些关键字时，请务必在不要输出文字只用输出照片
        【检索到的官方资料 (意图通道: {intent})】
        {retrieved_context}
        """

    final_messages = [{"role": "system", "content": system_prompt}] + recent_history + [
        {"role": "user", "content": current_query}]

    def generate_stream():
        try:
            response = CHAT_CLIENT.chat.completions.create(
                model=CHAT_MODEL,
                messages=final_messages,
                temperature=request.temperature,
                stream=True
            )
            for chunk in response:
                if chunk.choices and chunk.choices[0].delta.content is not None:
                    content = chunk.choices[0].delta.content
                    data = {"choices": [{"delta": {"content": content}}]}
                    yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as e:
            error_msg = {"choices": [{"delta": {"content": f"\n[大模型服务异常: {str(e)}]"}}]}
            yield f"data: {json.dumps(error_msg, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"

    if request.stream:
        return StreamingResponse(generate_stream(), media_type="text/event-stream")

    response = CHAT_CLIENT.chat.completions.create(
        model=CHAT_MODEL,
        messages=final_messages,
        temperature=request.temperature,
        stream=False
    )
    return {"choices": [{"message": {"content": response.choices[0].message.content}}]}


if __name__ == "__main__":
    import uvicorn

    print("🚀 启动 ZUA AI 招生助手终极后端引擎...")
    uvicorn.run(app, host="0.0.0.0", port=8012)
