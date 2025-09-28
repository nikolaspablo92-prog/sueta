from flask import Flask, render_template
import psycopg2
from dotenv import load_dotenv
import os
from datetime import datetime

# Загружаем переменные окружения
load_dotenv()

app = Flask(__name__)

def get_db_connection():
    return psycopg2.connect(
        host=os.getenv("DB_HOST"),
        port=int(os.getenv("DB_PORT")),
        database=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASS")
    )

@app.route('/')
def dashboard():
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute('''
            SELECT s.date, u.username, s.status_text
            FROM statuses s
            JOIN users u ON s.user_id = u.user_id
            WHERE s.date >= CURRENT_DATE - INTERVAL '7 days'
            ORDER BY s.date DESC, u.username
        ''')
        statuses = cur.fetchall()
        cur.close()
        conn.close()
        
        return render_template(
            'index.html',
            statuses=statuses,
            now=datetime.now().strftime("%Y-%m-%d %H:%M")
        )
    except Exception as e:
        return f"<h1>Ошибка подключения к БД</h1><p>{str(e)}</p>", 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080, debug=False)
