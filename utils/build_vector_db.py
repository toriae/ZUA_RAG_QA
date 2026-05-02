import os
import lancedb
import pandas as pd
from openai import OpenAI
from langchain_text_splitters import RecursiveCharacterTextSplitter

from config import (
    API_KEY, BASE_URL, EMBEDDING_MODEL,
    LANCEDB_URI, TABLE_NAME,
    DATA_TXT_PATH,
    CHUNK_SIZE_LANCEDB, CHUNK_OVERLAP_LANCEDB,
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
# 3. 智能文本切片 (Semantic Chunking)
# ==========================================
def process_and_chunk_text(file_path):
    """读取 txt 文件并按段落进行智能切片"""
    print("✂️ 正在读取并切分文本...")
    with open(file_path, "r", encoding="utf-8") as f:
        full_text = f.read()

    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE_LANCEDB,
        chunk_overlap=CHUNK_OVERLAP_LANCEDB,
        length_function=len,
        separators=["\n\n", "\n", "。", "！", "？", "；", ""]
    )
    chunks = text_splitter.split_text(full_text)
    print(f"📄 共切分为 {len(chunks)} 个文本块 (Chunks)")
    return chunks


# ==========================================
# 4. 主执行流：切分 -> 向量化 -> 入库 LanceDB
# ==========================================
def main():
    if not os.path.exists(DATA_TXT_PATH):
        print(f"❌ 数据文件不存在: {DATA_TXT_PATH}")
        return

    chunks = process_and_chunk_text(DATA_TXT_PATH)

    data_to_insert = []

    print("🧠 开始调用 Embedding 模型向量化文本 (这可能需要几分钟)...")
    for idx, chunk in enumerate(chunks):
        print(f"  正在处理第 {idx + 1}/{len(chunks)} 块...")
        vec = get_embedding(chunk)
        if vec:
            data_to_insert.append({
                "vector": vec,
                "text": chunk,
                "category": "校园生活与政策"
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
