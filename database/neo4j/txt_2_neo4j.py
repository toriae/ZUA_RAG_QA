"""将 data/xueyuan.md 中的学院专业信息导入 Neo4j 知识图谱。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import os
import re
import json
import time
from openai import OpenAI
from neo4j import GraphDatabase

from utils.config import (
    API_KEY, BASE_URL, CHAT_MODEL,
    NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD,
    XUEYUAN_MD_PATH,
    SLEEP_BETWEEN_CHUNKS,
)

LLM_CLIENT = OpenAI(api_key=API_KEY, base_url=BASE_URL)

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


def extract_graph_data(text_chunk, retry=3):
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
            return json.loads(response.choices[0].message.content.strip())
        except Exception as e:
            print(f"❌ 提取失败 (尝试 {attempt+1}): {e}")
            if attempt == retry - 1:
                return None
            time.sleep(2)
    return None


def merge_graph_data(all_data_list):
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
            existing = {m["name"] for m in idx["majors"]}
            for major in college.get("majors", []):
                mn = major.get("name")
                if not mn or mn in existing:
                    continue
                idx["majors"].append({
                    "name": mn,
                    "duration": major.get("duration", "未知"),
                    "features": major.get("features", [])
                })
                existing.add(mn)
    return merged


def write_to_neo4j(tx, graph_data):
    colleges = graph_data.get("colleges", [])
    for college in colleges:
        cn = college.get("name")
        if not cn:
            continue
        tx.run("MERGE (c:College {name: $name})", name=cn)
        print(f"🏗️ 创建/匹配学院节点: {cn}")
        for major in college.get("majors", []):
            mn = major.get("name")
            dur = major.get("duration", "未知")
            if not mn:
                continue
            tx.run("MERGE (m:Major {name: $name}) SET m.duration = $duration", name=mn, duration=dur)
            tx.run("MATCH (c:College {name: $c}) MATCH (m:Major {name: $m}) MERGE (c)-[:CONTAINS]->(m)", c=cn, m=mn)
            for feat in major.get("features", []):
                if not feat.strip():
                    continue
                tx.run("MERGE (f:Feature {name: $name})", name=feat)
                tx.run("MATCH (m:Major {name: $m}) MATCH (f:Feature {name: $f}) MERGE (m)-[:HAS_FEATURE]->(f)", m=mn, f=feat)
            print(f"  └─ 关联专业: {mn}")


def main():
    if not os.path.exists(XUEYUAN_MD_PATH):
        print(f"❌ 数据文件不存在: {XUEYUAN_MD_PATH}")
        return

    with open(XUEYUAN_MD_PATH, "r", encoding="utf-8") as f:
        full_text = f.read()

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
