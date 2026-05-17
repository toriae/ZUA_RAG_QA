"""
ZUA 招生助手 - 统一配置文件
所有路径均使用相对路径，基于项目根目录自动计算。
敏感配置优先通过 .env 文件或环境变量提供。
"""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)

# ==========================================
# 输入数据路径
# ==========================================
INPUT_DIR = os.path.join(PROJECT_ROOT, "data")
DATA_TXT_PATH = os.path.join(INPUT_DIR, "data.txt")
DATA_MD_PATH = os.path.join(INPUT_DIR, "data.md")
XUEYUAN_TXT_PATH = os.path.join(INPUT_DIR, "xueyuan.txt")
XUEYUAN_MD_PATH = os.path.join(INPUT_DIR, "xueyuan.md")
CSV_DIR = os.path.join(INPUT_DIR, "csv")

# ==========================================
# LanceDB 配置
# ==========================================
LANCEDB_URI = os.path.join(PROJECT_ROOT, "utils", "zua_lancedb")
TABLE_NAME = "campus_policies"

# ==========================================
# 大模型 API 配置 (通义千问)
# ==========================================
API_KEY = os.environ.get("ZUA_API_KEY", "")
BASE_URL = os.environ.get(
    "ZUA_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"
)
CHAT_MODEL = "qwen-plus"
ROUTER_MODEL = "qwen-turbo"
EMBEDDING_MODEL = "text-embedding-v4"

# ==========================================
# MySQL 配置
# ==========================================
MYSQL_URI = os.environ.get("ZUA_MYSQL_URI", "")

# ==========================================
# Neo4j 配置
# ==========================================
NEO4J_URI = os.environ.get("ZUA_NEO4J_URI", "")
NEO4J_USER = os.environ.get("ZUA_NEO4J_USER", "")
NEO4J_PASSWORD = os.environ.get("ZUA_NEO4J_PASSWORD", "")

# ==========================================
# 管理面板配置
# ==========================================
ADMIN_PASSWORD = os.environ.get("ZUA_ADMIN_PASSWORD", "")

# ==========================================
# 反馈统计配置
# ==========================================
FEEDBACK_FILE = os.path.join(PROJECT_ROOT, "data", "feedback.json")

# ==========================================
# 文本切分参数
# ==========================================
SLEEP_BETWEEN_CHUNKS = 0.01
