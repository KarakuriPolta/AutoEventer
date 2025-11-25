import sqlite3
from datetime import datetime, timedelta
import logging
import os

USAGE_DB_PATH = 'usage.db'

def init_db():
    """DBを初期化する。破損時は再作成する。"""
    try:
        # DB破損チェック用の一時接続
        conn = sqlite3.connect(USAGE_DB_PATH)
        c = conn.cursor()
        
        # 整合性チェック
        c.execute("PRAGMA integrity_check")
        result = c.fetchone()
        if result[0] != 'ok':
            logging.warning("DB破損を検知。再作成します。")
            conn.close()
            os.remove(USAGE_DB_PATH)
            conn = sqlite3.connect(USAGE_DB_PATH)
            c = conn.cursor()
        
        # テーブルとインデックスの作成
        c.execute('''CREATE TABLE IF NOT EXISTS usage (
                        user_id TEXT,
                        timestamp DATETIME
                    )''')
        
        # timestamp列にインデックスを追加（クエリ効率化のため）
        c.execute('''CREATE INDEX IF NOT EXISTS idx_timestamp 
                     ON usage(timestamp)''')
        
        # user_idとtimestampの組み合わせにもインデックスを追加
        c.execute('''CREATE INDEX IF NOT EXISTS idx_user_timestamp 
                     ON usage(user_id, timestamp)''')
        
        conn.commit()
        conn.close()
        logging.info("DB初期化成功")
        
    except sqlite3.Error as e:
        logging.error(f"DB初期化エラー: {e}")
        # エラー時はDBファイルを削除して再作成
        if os.path.exists(USAGE_DB_PATH):
            os.remove(USAGE_DB_PATH)
        conn = sqlite3.connect(USAGE_DB_PATH)
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS usage (
                        user_id TEXT,
                        timestamp DATETIME
                    )''')
        c.execute('''CREATE INDEX IF NOT EXISTS idx_timestamp 
                     ON usage(timestamp)''')
        c.execute('''CREATE INDEX IF NOT EXISTS idx_user_timestamp 
                     ON usage(user_id, timestamp)''')
        conn.commit()
        conn.close()
        logging.info("DBを再作成しました")

def check_limits(user_id):
    """実行回数制限をチェックし、問題なければ記録を追加する。"""
    try:
        with sqlite3.connect(USAGE_DB_PATH) as conn:
            c = conn.cursor()
            now = datetime.now()
            one_day_ago = now - timedelta(days=1)

            # ユーザーごとのチェック
            c.execute("SELECT COUNT(*) FROM usage WHERE user_id = ? AND timestamp > ?", 
                      (user_id, one_day_ago))
            user_count = c.fetchone()[0]
            if user_count >= 20:
                return False, "1日の実行回数の上限（20回）に達しました。"

            # 全ユーザー全体のチェック
            c.execute("SELECT COUNT(*) FROM usage WHERE timestamp > ?", 
                      (one_day_ago,))
            total_count = c.fetchone()[0]
            if total_count >= 1000:
                return False, "全ユーザーの実行回数の上限（1000回）に達しました。"

            # 実行記録をデータベースに追加
            c.execute("INSERT INTO usage (user_id, timestamp) VALUES (?, ?)", 
                      (user_id, now))
            conn.commit()
            return True, None
            
    except sqlite3.Error as e:
        logging.error(f"DB操作エラー(check_limits): {e}")
        return False, "データベースエラーが発生しました。Bot管理者に連絡してください。"
    except Exception as e:
        logging.error(f"予期しないエラー(check_limits): {e}")
        return False, "予期しないエラーが発生しました。Bot管理者に連絡してください。"