"""
ZUA 招生助手 - 统一配置文件
所有路径均使用相对路径，基于项目根目录自动计算。
"""
import os

# ==========================================
# 项目根目录 (E:\rag_zua)
# ==========================================
# utils/ 的上级目录即为项目根目录
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ==========================================
# 输入数据路径
# ==========================================
INPUT_DIR = os.path.join(PROJECT_ROOT, "data")
DATA_TXT_PATH = os.path.join(INPUT_DIR, "data.txt")
DATA_MD_PATH = os.path.join(INPUT_DIR, "data.md")
XUEYUAN_TXT_PATH = os.path.join(INPUT_DIR, "xueyuan.txt")
XUEYUAN_MD_PATH = os.path.join(INPUT_DIR, "xueyuan.md")

# ==========================================
# LanceDB 配置
# ==========================================
LANCEDB_URI = os.path.join(PROJECT_ROOT, "utils", "zua_lancedb")
TABLE_NAME = "campus_policies"

# ==========================================
# 大模型 API 配置 (通义千问)
# ==========================================
API_KEY = os.environ.get("ZUA_API_KEY", "your-api-key-here")
BASE_URL = os.environ.get("ZUA_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
CHAT_MODEL = "qwen-plus"
ROUTER_MODEL = "qwen-turbo"
EMBEDDING_MODEL = "text-embedding-v4"

# ==========================================
# MySQL 配置
# ==========================================
MYSQL_URI = os.environ.get(
    "ZUA_MYSQL_URI",
    "mysql+pymysql://root:your_password@localhost:3306/zua_admission_db?charset=utf8mb4"
)

# ==========================================
# Neo4j 配置
# ==========================================
NEO4J_URI = os.environ.get("ZUA_NEO4J_URI", "neo4j+s://your-instance.databases.neo4j.io")
NEO4J_USER = os.environ.get("ZUA_NEO4J_USER", "your_user")
NEO4J_PASSWORD = os.environ.get("ZUA_NEO4J_PASSWORD", "your_password")

# ==========================================
# 文本切分参数
# ==========================================
SLEEP_BETWEEN_CHUNKS = 0.01
