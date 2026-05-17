# 数据库初始化模块

本目录包含三个数据库的初始化脚本，用于将原始数据导入外部数据库服务。

## 架构说明

- **LanceDB**：嵌入式本地向量库，数据已预构建在 `../utils/zua_lancedb/` 中，**开箱即用，无需初始化**。
- **MySQL**：外部服务，用于历年录取分数查询（可选）。
- **Neo4j**：外部服务，用于学院专业知识图谱查询（可选）。

## 使用步骤

### 1. 安装依赖

```bash
pip install -r ../requirements-db.txt
```

### 2. 部署数据库服务

在运行脚本前，需要先安装并启动对应的数据库服务。

#### MySQL

```bash
# Ubuntu
sudo apt install mysql-server
sudo systemctl start mysql
mysql -e "CREATE DATABASE zua_chatbot;"
```

#### Neo4j

```bash
# 使用 Docker
docker run -d -p 7687:7687 -p 7474:7474 \
  -e NEO4J_AUTH=neo4j/your_password \
  neo4j:5
```

### 3. 配置连接信息

在 `../.env` 文件中填写：

```
ZUA_MYSQL_URI=mysql+pymysql://root:password@localhost:3306/zua_chatbot
ZUA_NEO4J_URI=bolt://localhost:7687
ZUA_NEO4J_USER=neo4j
ZUA_NEO4J_PASSWORD=your_password
```

### 4. 运行初始化脚本

从项目根目录执行：

```bash
# 导入历年分数到 MySQL
python3 -m database.sql.import_csv_to_mysql

# 构建知识图谱到 Neo4j
python3 -m database.neo4j.txt_2_neo4j
```

### 5. 重新构建向量库（可选）

向量库已预构建，仅在更新 `data/data.md` 后需要重新构建：

```bash
python3 -m database.build_vector_db
```
