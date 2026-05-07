import os
import re
import json
import time
from openai import OpenAI
from neo4j import GraphDatabase

from config import (
    API_KEY, BASE_URL, CHAT_MODEL,
    NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD,
    XUEYUAN_MD_PATH,
    SLEEP_BETWEEN_CHUNKS,
)

# ==========================================
# 1. 初始化客户端
# ==========================================
LLM_CLIENT = OpenAI(api_key=API_KEY, base_url=BASE_URL)


# ==========================================
# 2. 系统 Prompt (引导 LLM 输出稳定 JSON)
# ==========================================
SYSTEM_PROMPT = """
你是一个严谨的高校知识图谱构建专家。
请阅读提供的《郑州航空工业管理学院》的文本片段，提取出其中的【学院】、【专业】以及【专业特色】。
你必须严格输出纯 JSON 格式的数据，绝不能包含任何 markdown 符号（如 ```json）或解释性文字。

JSON 结构必须严格如下：
{
  "colleges": [
    {
      "name": "学院名称（如：航空发动机学院）",
      "majors": [
        {
          "name": "专业名称",
          "duration": "学制（如：4年）",
          "features": ["特色1", "特色2", "就业去向1"]
        }
      ]
    }
  ]
}
如果提取不到相关信息，请输出 {"colleges": []}。
"""


# ==========================================
# 3. 数据抽取函数 (支持重试)
# ==========================================
def extract_graph_data(text_chunk, retry=3):
    """调用大模型，将纯文本转化为结构化 JSON，失败时重试"""
    for attempt in range(retry):
        try:
            print(f"🧠 正在请求大模型 (尝试 {attempt+1}/{retry})...")
            response = LLM_CLIENT.chat.completions.create(
                model=CHAT_MODEL,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": f"请提取以下文本:\n\n{text_chunk}"}
                ],
                temperature=0.1,
                timeout=30
            )
            result_text = response.choices[0].message.content.strip()
            return json.loads(result_text)
        except Exception as e:
            print(f"❌ 提取失败 (尝试 {attempt+1}): {e}")
            if attempt == retry - 1:
                return None
            time.sleep(2)
    return None


# ==========================================
# 4. 将 JSON 数据合并去重
# ==========================================
def merge_graph_data(all_data_list):
    """合并多个 JSON 结果，去重学院和专业"""
    merged = {"colleges": []}
    college_map = {}

    for data in all_data_list:
        if not data or "colleges" not in data:
            continue
        for college in data["colleges"]:
            name = college.get("name")
            if not name:
                continue
            if name not in college_map:
                college_map[name] = {"name": name, "majors": []}
                merged["colleges"].append(college_map[name])
            idx = college_map[name]
            existing_major_names = {m["name"] for m in idx["majors"]}
            for major in college.get("majors", []):
                major_name = major.get("name")
                if not major_name or major_name in existing_major_names:
                    continue
                idx["majors"].append({
                    "name": major_name,
                    "duration": major.get("duration", "未知"),
                    "features": major.get("features", [])
                })
                existing_major_names.add(major_name)
    return merged


# ==========================================
# 5. 写入 Neo4j (使用 MERGE 保证幂等)
# ==========================================
def write_to_neo4j(tx, graph_data):
    """将合并后的 JSON 数据转化为 Cypher 语句并执行入库"""
    colleges = graph_data.get("colleges", [])
    for college in colleges:
        college_name = college.get("name")
        if not college_name:
            continue

        tx.run("MERGE (c:College {name: $name})", name=college_name)
        print(f"🏗️ 创建/匹配学院节点: {college_name}")

        for major in college.get("majors", []):
            major_name = major.get("name")
            duration = major.get("duration", "未知")
            if not major_name:
                continue

            tx.run("""
                MERGE (m:Major {name: $name})
                SET m.duration = $duration
            """, name=major_name, duration=duration)

            tx.run("""
                MATCH (c:College {name: $c_name})
                MATCH (m:Major {name: $m_name})
                MERGE (c)-[:CONTAINS]->(m)
            """, c_name=college_name, m_name=major_name)

            for feature in major.get("features", []):
                if not feature.strip():
                    continue
                tx.run("MERGE (f:Feature {name: $name})", name=feature)
                tx.run("""
                    MATCH (m:Major {name: $m_name})
                    MATCH (f:Feature {name: $f_name})
                    MERGE (m)-[:HAS_FEATURE]->(f)
                """, m_name=major_name, f_name=feature)

            print(f"  └─ 关联专业: {major_name} (及特色标签)")


# ==========================================
# 6. 主执行流：分块读取、提取、合并、入库
# ==========================================
def main():
    if not os.path.exists(XUEYUAN_MD_PATH):
        print(f"❌ 数据文件不存在: {XUEYUAN_MD_PATH}")
        return

    with open(XUEYUAN_MD_PATH, "r", encoding="utf-8") as f:
        full_text = f.read()

    # 按一级标题 (# ) 切分，每个学院一个 chunk
    pattern = r'^# (.+)$'
    splits = re.split(pattern, full_text, flags=re.MULTILINE)

    chunks = []
    for i in range(1, len(splits), 2):
        title = splits[i].strip()
        body = splits[i + 1].strip() if i + 1 < len(splits) else ""
        chunk_text = f"# {title}\n\n{body}"
        if chunk_text.strip():
            chunks.append(chunk_text)

    print(f"📄 按一级标题切分为 {len(chunks)} 个学院块")

    all_extracted = []
    for idx, chunk in enumerate(chunks):
        print(f"\n--- 处理第 {idx+1}/{len(chunks)} 块 ---")
        extracted = extract_graph_data(chunk)
        if extracted:
            all_extracted.append(extracted)
            colleges_count = len(extracted.get("colleges", []))
            print(f"  本块提取到 {colleges_count} 个学院")
        else:
            print(f"  本块提取失败，跳过")
        time.sleep(SLEEP_BETWEEN_CHUNKS)

    if not all_extracted:
        print("❌ 没有任何数据提取成功，退出")
        return

    merged_data = merge_graph_data(all_extracted)
    print(f"\n✅ 合并后共 {len(merged_data['colleges'])} 个学院")

    print("\n⏳ 开始写入 Neo4j...")
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    try:
        with driver.session() as session:
            session.execute_write(write_to_neo4j, merged_data)
        print("🎉 图谱数据全部成功入库！")
    except Exception as e:
        print(f"❌ Neo4j 写入失败: {e}")
    finally:
        driver.close()


if __name__ == "__main__":
    main()
