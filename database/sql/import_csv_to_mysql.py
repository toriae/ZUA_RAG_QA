"""数据库初始化脚本 — 将 data/csv 下的 CSV 文件导入 MySQL。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import os
import glob
import pandas as pd
from sqlalchemy import create_engine

from utils.config import MYSQL_URI, CSV_DIR

TABLE_NAME = "historical_scores"

COLUMN_MAPPING = {
    '年份': 'year',
    '省市': 'province',
    '类型': 'admission_type',
    '科类': 'subject_category',
    '专业': 'major_name',
    '录取人数': 'enroll_count',
    '最低分': 'min_score',
    '平均分': 'avg_score',
    '最高分': 'max_score',
    '备注': 'notes'
}

NUMERIC_COLS = ['min_score', 'avg_score', 'max_score', 'enroll_count']


def read_csv_safe(file_path: str) -> pd.DataFrame:
    for encoding in ('utf-8', 'gbk', 'gb2312'):
        try:
            return pd.read_csv(file_path, encoding=encoding)
        except (UnicodeDecodeError, ValueError):
            continue
    raise ValueError(f"无法解码文件: {file_path}")


def main():
    if not os.path.isdir(CSV_DIR):
        print(f"❌ CSV 目录不存在: {CSV_DIR}")
        return

    csv_files = glob.glob(os.path.join(CSV_DIR, '*.csv'))
    if not csv_files:
        print(f"❌ 目录下没有找到 CSV 文件: {CSV_DIR}")
        return

    print(f"📂 找到 {len(csv_files)} 个 CSV 文件")

    df_list = []
    for file_path in csv_files:
        try:
            df = read_csv_safe(file_path)
            df_list.append(df)
            print(f"  ✅ {os.path.basename(file_path)} — {len(df)} 行")
        except Exception as e:
            print(f"  ❌ {os.path.basename(file_path)} — 读取失败: {e}")

    if not df_list:
        print("❌ 没有成功读取任何 CSV 文件")
        return

    merged_df = pd.concat(df_list, ignore_index=True)
    print(f"\n📄 合并后共 {len(merged_df)} 行数据")

    existing_cols = {k: v for k, v in COLUMN_MAPPING.items() if k in merged_df.columns}
    merged_df.rename(columns=existing_cols, inplace=True)

    for col in NUMERIC_COLS:
        if col in merged_df.columns:
            merged_df[f'{col}_raw'] = merged_df[col].astype(str).replace('nan', None)
            merged_df[col] = pd.to_numeric(merged_df[col], errors='coerce')

    merged_df = merged_df.where(pd.notnull(merged_df), None)

    print("\n⏳ 正在写入数据库...")
    engine = create_engine(MYSQL_URI)
    try:
        merged_df.to_sql(name=TABLE_NAME, con=engine, if_exists='append', index=False, chunksize=1000)
        print(f"🎉 成功导入 {len(merged_df)} 条记录到 {TABLE_NAME} 表")
    except Exception as e:
        print(f"❌ 写入数据库失败: {e}")


if __name__ == "__main__":
    main()
