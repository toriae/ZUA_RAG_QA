# ZUA 智能招生助手

郑州航空工业管理学院（ZUA）AI 招生问答系统。基于 RAG（检索增强生成）架构，融合 **向量检索**、**知识图谱**、**关系型数据库** 三种数据源，通过大模型意图路由自动分流查询，为考生和家长提供精准的招生咨询服务。

## 功能特性

- **智能意图识别**：自动判断用户问题是查分数、查专业、查政策还是闲聊
- **历年分数查询**：支持按省份、年份、专业查询录取分数（Text-to-SQL）
- **专业知识图谱**：查询学院、专业、学制、特色等关系数据（Text-to-Cypher）
- **校园政策问答**：宿舍、食堂、奖助学金、转专业等生活指南（向量检索，按 Markdown 标题分块）
- **实拍图片展示**：根据关键词自动关联校园实拍图片（食堂、宿舍、风景等）
- **流式响应**：SSE 流式输出，打字机效果，体验流畅
- **前端界面**：ChatGPT 风格对话界面，支持侧边栏快捷提问、分数弹窗查询、校园地图

## 系统架构

```
用户提问
  │
  ▼
┌─────────────────┐
│  意图路由        │  qwen-turbo 识别意图
│  (Intent Router) │
└───────┬─────────┘
        │
   ┌────┴────┬──────────┐
   ▼         ▼          ▼
┌──────┐ ┌──────┐ ┌──────────┐
│LanceDB│ │MySQL │ │  Neo4j   │
│向量库 │ │分数库│ │ 知识图谱  │
└──┬───┘ └──┬───┘ └────┬─────┘
   │        │          │
   └────┬───┴──────────┘
        ▼
┌─────────────────┐
│  qwen-plus      │  基于检索结果生成回答
│  生成回答       │
└─────────────────┘
```

## 目录结构

```
rag_zua/
├── .gitignore
├── pyproject.toml              # Python 项目配置
├── requirements.txt            # Python 依赖清单
├── README.md
│
├── data/                       # 数据文件
│   ├── data.txt                # 校园政策原始文本（保留源文件）
│   ├── data.md                 # 校园政策 Markdown 版 → 向量库（按标题分块）
│   ├── xueyuan.txt             # 学院专业原始文本（保留源文件）
│   ├── xueyuan.md              # 学院专业 Markdown 版 → 知识图谱（按标题分块）
│   └── csv/                    # 各省历年录取分数 CSV → MySQL
│       ├── henan_1.csv
│       ├── shandong.csv
│       └── ...
│
├── static/                     # 前端页面及静态资源
│   ├── index.html              # 主页面（ChatGPT 风格）
│   ├── brochure.md             # 招生章程（侧边栏可查看）
│   ├── videos.md               # 映像郑航（视频合集）
│   ├── img.png                 # 校徽 Logo
│   ├── canting/                # 食堂实拍 (1-3.jpg)
│   ├── ditu/                   # 校区地图 (1-2.jpg)
│   ├── fengjing/               # 校园风景 (1-8.jpg)
│   ├── qinshi/                 # 宿舍实拍 (1-5.jpg)
│   └── shangyejie/             # 商业街实拍 (1-5.jpg)
│
└── utils/                      # Python 后端及数据处理
    ├── __init__.py
    ├── config.py               # 统一配置（路径、API Key、数据库）
    ├── main.py                 # FastAPI 后端服务（主入口）
    ├── build_vector_db.py      # 构建 LanceDB 向量数据库
    ├── txt_2_neo4j.py          # 构建 Neo4j 知识图谱
    └── import_csv_to_mysql.py  # 导入 CSV 到 MySQL
```

## 环境要求

- **Python** >= 3.9
- **MySQL** >= 5.7（存储历年录取分数）
- **Neo4j** >= 4.4（存储专业知识图谱，可用免费的 [Neo4j Aura](https://neo4j.com/cloud/aura-free/)）
- **阿里云通义千问 API Key**（用于大模型调用和文本向量化）

## 安装步骤

### 1. 克隆项目

```bash
git clone https://github.com/your-username/zua-rag-assistant.git
cd zua-rag-assistant
```

### 2. 创建虚拟环境（推荐）

```bash
python -m venv venv

# Windows
venv\Scripts\activate

# macOS / Linux
source venv/bin/activate
```

### 3. 安装依赖

```bash
pip install -r requirements.txt
```

## 配置说明

所有配置集中在 `utils/config.py`，支持通过 **环境变量** 覆盖默认值。

### 大模型 API（必填）

需要一个阿里云通义千问的 API Key，用于对话生成和文本向量化。

| 环境变量 | 说明 | 默认值 |
|---------|------|--------|
| `ZUA_API_KEY` | 通义千问 API Key | config.py 中的默认值 |
| `ZUA_BASE_URL` | API 地址 | `https://dashscope.aliyuncs.com/compatible-mode/v1` |

> **获取方式**：前往 [阿里云百炼平台](https://bailian.console.aliyun.com/) 注册并创建 API Key。
> 需要开通的模型：`qwen-plus`（对话）、`qwen-turbo`（路由）、`text-embedding-v4`（向量化）。

### MySQL 数据库（必填）

存储历年录取分数数据，需要提前创建数据库和表。

| 环境变量 | 说明 | 默认值 |
|---------|------|--------|
| `ZUA_MYSQL_URI` | MySQL 连接字符串 | `mysql+pymysql://root:123456@localhost:3306/zua_admission_db?charset=utf8mb4` |

**初始化步骤**：

```sql
-- 1. 创建数据库
CREATE DATABASE zua_admission_db CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

-- 2. 建表（运行导入脚本时会自动建表，也可手动创建）
USE zua_admission_db;
CREATE TABLE historical_scores (
    id INT AUTO_INCREMENT PRIMARY KEY,
    year VARCHAR(10),
    province VARCHAR(20),
    admission_type VARCHAR(20),
    subject_category VARCHAR(20),
    major_name VARCHAR(100),
    enroll_count INT,
    min_score FLOAT,
    avg_score FLOAT,
    max_score FLOAT,
    notes TEXT,
    min_score_raw VARCHAR(50),
    avg_score_raw VARCHAR(50),
    max_score_raw VARCHAR(50),
    enroll_count_raw VARCHAR(50)
);
```

### Neo4j 图数据库（必填）

存储学院-专业-特色的知识图谱。可以使用免费的 Neo4j Aura 云服务。

| 环境变量 | 说明 | 默认值 |
|---------|------|--------|
| `ZUA_NEO4J_URI` | Neo4j 连接地址 | config.py 中的默认值 |
| `ZUA_NEO4J_USER` | 用户名 | config.py 中的默认值 |
| `ZUA_NEO4J_PASSWORD` | 密码 | config.py 中的默认值 |

> **获取方式**：前往 [Neo4j Aura](https://neo4j.com/cloud/aura-free/) 创建免费数据库实例，获取连接 URI 和凭据。

### 推荐：使用 .env 文件管理配置

如果不想每次设置环境变量，可以在项目根目录创建 `.env` 文件（已在 `.gitignore` 中忽略）：

```bash
# .env
ZUA_API_KEY=sk-your-api-key-here
ZUA_MYSQL_URI=mysql+pymysql://root:your_password@localhost:3306/zua_admission_db?charset=utf8mb4
ZUA_NEO4J_URI=neo4j+s://xxxx.databases.neo4j.io
ZUA_NEO4J_USER=your_user
ZUA_NEO4J_PASSWORD=your_password
```

然后在 `config.py` 顶部加载：

```python
from dotenv import load_dotenv
load_dotenv()
```

> 需额外安装：`pip install python-dotenv`

## 数据准备与初始化

按以下顺序依次运行数据处理脚本（只需运行一次，后续数据不变则无需重复）：

### 步骤 1：构建向量数据库

将 `data/data.md` 按一级标题分块并向量化，存入本地 LanceDB：

```bash
python utils/build_vector_db.py
```

- 按 Markdown 一级标题（`#`）切分，每个标题对应一个语义完整的文本块
- 调用 `text-embedding-v4` 模型生成向量
- 数据存储在 `utils/zua_lancedb/` 目录下（纯本地，无需额外服务）

### 步骤 2：构建知识图谱

从 `data/xueyuan.md` 中按学院分块，提取学院、专业、特色信息，写入 Neo4j：

```bash
python utils/txt_2_neo4j.py
```

- 按一级标题（`#`）切分，每个学院作为一个独立文本块送入大模型
- 调用 `qwen-plus` 大模型提取结构化 JSON
- 自动合并去重，使用 `MERGE` 保证幂等（可重复运行）
- 图谱结构：`(College)-[:CONTAINS]->(Major)-[:HAS_FEATURE]->(Feature)`

### 步骤 3：导入录取分数

将 `data/csv/` 下的所有 CSV 文件导入 MySQL：

```bash
python utils/import_csv_to_mysql.py
```

- 自动读取目录下所有 CSV 文件并合并
- 自动进行中文列名到英文字段名的映射
- 支持 utf-8 / gbk / gb2312 多种编码自动检测

## 启动服务

```bash
python utils/main.py
```

服务将在 `http://localhost:8012` 启动，控制台会显示：

```
🚀 启动 ZUA AI 招生助手终极后端引擎...
```

### 访问前端

用浏览器直接打开 `static/index.html` 即可使用。

> **注意**：前端通过 `http://127.0.0.1:8012/v1/chat/completions` 调用后端，请确保后端已启动。
> 如果需要修改端口或地址，编辑 `static/index.html` 中的 `API_URL` 变量和 `utils/main.py` 中的端口号。

### API 接口

后端暴露一个兼容 OpenAI 格式的接口：

```
POST http://localhost:8012/v1/chat/completions
Content-Type: application/json

{
  "model": "default",
  "messages": [{"role": "user", "content": "你们学校有哪些航空特色专业？"}],
  "temperature": 0.2,
  "stream": true
}
```

支持流式（SSE）和非流式两种模式。

## 常见问题

### Q: 启动时提示 "找不到 LanceDB 表"

说明还没有构建向量数据库，先运行 `python utils/build_vector_db.py`。需要确保 `data/data.md` 文件存在。

### Q: 修改了 data.md 或 xueyuan.md 后需要重新构建吗

是的。修改后需要重新运行对应的构建脚本：
- 修改了 `data/data.md` → 运行 `python utils/build_vector_db.py`
- 修改了 `data/xueyuan.md` → 运行 `python utils/txt_2_neo4j.py`

### Q: MySQL 连接失败

- 确认 MySQL 服务已启动
- 确认数据库 `zua_admission_db` 已创建
- 检查 `ZUA_MYSQL_URI` 环境变量中的用户名、密码、端口是否正确

### Q: Neo4j 连接失败

- 如果使用 Neo4j Aura，确认实例未过期（免费实例 30 天不活跃会暂停）
- 检查 URI、用户名、密码是否正确

### Q: 大模型 API 报错

- 确认 API Key 有效且余额充足
- 确认已开通 `qwen-plus`、`qwen-turbo`、`text-embedding-v4` 三个模型
- 前往 [阿里云百炼控制台](https://bailian.console.aliyun.com/) 检查模型开通状态

### Q: 前端页面打不开或显示异常

- 确认用浏览器直接打开了 `static/index.html`（不是通过 file:// 协议打开可能会有跨域问题）
- 推荐使用 VS Code 的 Live Server 插件，或直接在项目目录运行 `python -m http.server 8080` 后访问 `http://localhost:8080/static/index.html`

### Q: 如何更新录取分数数据

将新的 CSV 文件放入 `data/csv/` 目录，然后重新运行 `python utils/import_csv_to_mysql.py`。注意使用 `if_exists='append'` 模式，会追加数据。如需清空重导，先手动清空 MySQL 表。

## 技术栈

| 组件 | 技术 | 用途 |
|------|------|------|
| 后端框架 | FastAPI + Uvicorn | API 服务，SSE 流式响应 |
| 大模型 | 通义千问 (qwen-plus / qwen-turbo) | 意图识别、回答生成 |
| 向量化 | text-embedding-v4 | 文本向量化 |
| 向量数据库 | LanceDB | 本地向量检索（零配置） |
| 关系数据库 | MySQL | 历年录取分数存储 |
| 图数据库 | Neo4j | 学院-专业知识图谱 |
| 文本切分 | 按 Markdown 标题分块（正则 `^#`） | 替代固定字符切分，语义完整性更好 |
| 前端 | 原生 HTML/CSS/JS + Marked.js | ChatGPT 风格对话界面 |
| 地图 | 高德地图 JS API | 校园地图展示 |

## 致谢

- [阿里云百炼平台](https://bailian.console.aliyun.com/) — 提供大模型 API
- [LanceDB](https://lancedb.com/) — 轻量级本地向量数据库
- [Neo4j](https://neo4j.com/) — 图数据库
- [LangChain](https://www.langchain.com/) — 早期版本使用其文本切分工具（已替换为基于 Markdown 标题的自定义分块）
