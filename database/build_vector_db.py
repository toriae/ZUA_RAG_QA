#!/usr/bin/env python3
"""构建 LanceDB 向量库脚本
用法: 从项目根目录运行  python3 -m database.build_vector_db
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import os
import re
import lancedb
import pandas as pd
from openai import OpenAI

from utils.config import (
    API_KEY, BASE_URL, EMBEDDING_MODEL,
    LANCEDB_URI, TABLE_NAME,
    DATA_MD_PATH,
)

# ==========================================
# 1. 初始化 Embedding 客户端
# ==========================================
EMBEDDING_CLIENT = OpenAI(api_key=API_KEY, base_url=BASE_URL)


# ==========================================
# 2. 向量化函数
# ==========================================
def get_embedding(text):
    """调用大模型将文本转化为向量"""
    try:
        response = EMBEDDING_CLIENT.embeddings.create(
            input=text,
            model=EMBEDDING_MODEL
        )
        return response.data[0].embedding
    except Exception as e:
        print(f"❌ 向量化失败: {e}")
        return None


# ==========================================
# 3. 按 Markdown 二级标题分块
# ==========================================
def process_and_chunk_text(file_path):
    """读取 .md 文件并按二级标题 (## ) 切分为语义完整的文本块"""
    print("✂️ 正在读取并按二级标题切分 Markdown 文本...")
    with open(file_path, "r", encoding="utf-8") as f:
        full_text = f.read()

    pattern = r'^## (.+)$'
    splits = re.split(pattern, full_text, flags=re.MULTILINE)

    chunks = []
    if splits[0].strip():
        chunks.append({"text": splits[0].strip(), "category": "概述"})

    for i in range(1, len(splits), 2):
        title = splits[i].strip()
        body = splits[i + 1].strip() if i + 1 < len(splits) else ""
        chunk_text = f"## {title}\n\n{body}"
        if chunk_text.strip():
            chunks.append({"text": chunk_text, "category": title})

    print(f"📄 共切分为 {len(chunks)} 个文本块 (按二级标题)")
    return chunks


# ==========================================
# 4. 主执行流：切分 -> 向量化 -> 入库
# ==========================================
def main():
    if not os.path.exists(DATA_MD_PATH):
        print(f"❌ 数据文件不存在: {DATA_MD_PATH}")
        return

    chunks = process_and_chunk_text(DATA_MD_PATH)
    data_to_insert = []

    print("🧠 开始调用 Embedding 模型向量化文本 (这可能需要几分钟)...")
    for idx, chunk in enumerate(chunks):
        print(f"  正在处理第 {idx + 1}/{len(chunks)} 块 — {chunk['category']}...")
        vec = get_embedding(chunk["text"])
        if vec:
            data_to_insert.append({
                "vector": vec,
                "text": chunk["text"],
                "category": chunk["category"],
            })

    if not data_to_insert:
        print("❌ 没有成功生成任何向量数据。")
        return

    print(f"\n💾 正在将 {len(data_to_insert)} 条数据写入 LanceDB...")
    db = lancedb.connect(LANCEDB_URI)
    df = pd.DataFrame(data_to_insert)
    db.create_table(TABLE_NAME, data=df, mode="overwrite")

    print(f"🎉 向量数据库构建完成！数据已安全保存在 {LANCEDB_URI} 目录下。")


if __name__ == "__main__":
    main()
