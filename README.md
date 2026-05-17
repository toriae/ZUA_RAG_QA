# ZUA 招生咨询助手

郑州航空工业学院招生智能问答系统 —— 基于 RAG（检索增强生成）+ 多路数据查询的 AI 招生咨询平台。

## 功能特性

- **智能意图识别**：自动识别用户意图，分为政策咨询、分数查询、专业了解、闲聊四类
- **多路数据检索**：
  - **LanceDB 向量库**：校园政策、宿舍、军训、转专业等文本语义检索（本地内置）
  - **MySQL 关系库**：全国各省历年录取分数查询（可选）
  - **Neo4j 图谱库**：学院、专业、特色知识图谱查询（可选）
- **流式对话输出**：基于 SSE（Server-Sent Events）的逐 token 实时输出
- **会话持久化**：对话记录自动保存到磁盘，重启不丢失
- **管理面板**：数据统计、会话查看、系统日志、文件管理、系统状态等

## 快速开始

### 1. 环境要求

- Python 3.10+
- pip 包管理器

### 2. 安装依赖

```bash
# 安装核心依赖（即可运行聊天功能）
pip install -r requirements.txt
```

### 3. 配置文件

```bash
cp .env.example .env
```

编辑 `.env` 文件，**至少填写 `ZUA_API_KEY`**（从阿里云 DashScope 获取）：

```
ZUA_API_KEY=sk-你的真实APIKey
ZUA_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
```

> **说明**：MySQL 和 Neo4j 为可选配置。不填写对应 URI 时系统自动跳过相关查询通道，不影响基本聊天功能。

### 4. 启动服务

```bash
# Linux / macOS
chmod +x start.sh
./start.sh

# Windows 或直接运行
python3 -m uvicorn utils.main:app --host 0.0.0.0 --port 8012
```

### 5. 访问

- **前端页面**：http://localhost:8012
- **管理面板**：http://localhost:8012/v1/admin

## 项目结构

```
zua-chatbot/
├── .env.example           # 配置文件模板
├── .gitignore
├── requirements.txt       # 核心依赖（必需）
├── requirements-db.txt    # 数据库依赖（可选）
├── start.sh               # 一键启动脚本
├── README.md              # 本文件
│
├── data/                  # 原始数据
│   ├── data.md            # 校园政策文本（向量库数据源）
│   ├── data.txt           # 校园政策原始文本
│   ├── xueyuan.md         # 学院专业信息（图谱数据源）
│   ├── xueyuan.txt        # 学院信息原始文本
│   └── csv/               # 各省历年录取分数 CSV
│
├── database/              # 数据库初始化脚本（独立模块）
│   ├── __init__.py
│   ├── build_vector_db.py # 构建 LanceDB 向量库
│   ├── sql/
│   │   ├── __init__.py
│   │   └── import_csv_to_mysql.py  # CSV → MySQL
│   └── neo4j/
│       ├── __init__.py
│       └── txt_2_neo4j.py          # 文本 → Neo4j 图谱
│
├── utils/                 # 后端核心代码
│   ├── __init__.py
│   ├── main.py            # FastAPI 主应用（路由、API、管理面板）
│   ├── config.py          # 统一配置管理
│   ├── feedback.py        # 反馈统计模块
│   └── zua_lancedb/       # LanceDB 向量数据（已预构建）
│
├── static/                # 前端静态文件
│   ├── index.html         # 主页面
│   ├── app.js             # 前端逻辑
│   ├── style.css          # 样式文件
│   └── img.png            # AI 助手头像
│
└── docs/                  # 文档与截图
    └── images/            # 界面截图
```

## 数据库说明

项目采用**数据库分离架构**：核心应用（聊天对话）仅依赖本地 LanceDB 向量库，已预构建并随项目分发，开箱即用。MySQL 和 Neo4j 作为可选的外部数据库，需要单独安装和配置。

### 核心依赖（内置）

| 数据库 | 用途 | 安装方式 |
|--------|------|----------|
| **LanceDB** | 政策文本向量检索 | `pip install -r requirements.txt`（已包含） |

LanceDB 是**嵌入式本地数据库**，无需额外安装服务，数据已预构建在 `utils/zua_lancedb/` 目录下。

### 可选数据库（需自行部署）

| 数据库 | 用途 | 安装方式 |
|--------|------|----------|
| **MySQL** | 历年录取分数查询 | `pip install -r requirements-db.txt` + 部署 MySQL 服务 |
| **Neo4j** | 学院专业知识图谱 | `pip install -r requirements-db.txt` + 部署 Neo4j 服务 |

### 数据库初始化

如果需要启用 MySQL 或 Neo4j 功能：

```bash
# 1. 安装数据库依赖
pip install -r requirements-db.txt

# 2. 部署并启动对应的数据库服务（MySQL / Neo4j）

# 3. 在 .env 中填写数据库连接信息

# 4. 运行初始化脚本
python3 -m database.sql.import_csv_to_mysql   # 导入分数数据到 MySQL
python3 -m database.neo4j.txt_2_neo4j         # 构建知识图谱到 Neo4j
```

如果不需要重新构建向量库（已预构建），可跳过。如需重建：

```bash
python3 -m database.build_vector_db
```

## 管理面板功能

访问 `/v1/admin` 输入管理员密码后可使用：

| 模块 | 功能 |
|------|------|
| 数据统计 | 总提问数、命中率、热门意图、未命中查询 |
| 会话记录 | 查看所有用户对话历史，支持详情查看 |
| 系统日志 | 按级别筛选查看服务运行日志 |
| 文件管理 | 在线编辑 data.md、.env、config.py |
| 数据库 | MySQL 可视化查询，支持表名快捷 |
| 系统状态 | CPU、内存、磁盘等实时监控 |
| 服务控制 | 查看/重启/停止服务状态 |
| 安全设置 | 修改管理面板密码 |

## API 接口

| 路径 | 方法 | 说明 |
|------|------|------|
| `/` | GET | 前端主页面 |
| `/v1/chat/completions` | POST | 聊天对话接口（支持 SSE 流式） |
| `/v1/admin` | GET | 管理面板 |
| `/v1/admin/sessions` | GET | 获取会话列表 |
| `/v1/admin/sessions/{id}` | GET | 获取单条会话详情 |
| `/v1/stats` | GET | 统计信息 |
| `/v1/health` | GET | 服务健康检查 |
| `/v1/admin/logs` | GET | 系统日志 |
| `/v1/admin/system` | GET | 系统状态 |
| `/v1/admin/service/status` | GET | 服务运行状态 |

## 技术栈

| 层级 | 技术 |
|------|------|
| 后端 | FastAPI + Uvicorn |
| 前端 | 原生 HTML/CSS/JS |
| 向量检索 | LanceDB（嵌入式本地库）+ text-embedding-v4 |
| 关系数据库 | MySQL（可选，SQLAlchemy + PyMySQL） |
| 图数据库 | Neo4j（可选） |
| 大模型 | 通义千问 qwen-plus（OpenAI 兼容 API） |
| 会话存储 | JSON 文件持久化 |

## 常见问题

### Q: 启动后提示 "ZUA_API_KEY 未配置"

A: 请确保 `.env` 文件中 `ZUA_API_KEY` 填写了有效的 API Key。

### Q: 分数查询功能不工作

A: 需要单独安装 MySQL 服务，填写 `.env` 中的 `ZUA_MYSQL_URI`，并运行 `python3 -m database.sql.import_csv_to_mysql` 导入数据。

### Q: 专业图谱查询不工作

A: 需要单独安装 Neo4j 服务，填写 `.env` 中的 Neo4j 配置，并运行 `python3 -m database.neo4j.txt_2_neo4j` 构建知识图谱。

### Q: 政策咨询功能不工作

A: 检查 `utils/zua_lancedb` 目录是否存在，如缺失请运行 `python3 -m database.build_vector_db` 重建向量库。

## 许可

本项目为课程作业，仅用于学习和演示。
