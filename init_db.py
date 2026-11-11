import sqlite3

def init_db(db_name="financial_research.db"):
    conn = sqlite3.connect(db_name)
    cursor = conn.cursor()

    # 1. Companies table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS companies (
        company_id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        ticker TEXT NOT NULL UNIQUE
    )
    """)

    # 2. Documents table (references 10-Q files on disk)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS documents (
        report_id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id INTEGER,
        title TEXT NOT NULL,
        form_type TEXT NOT NULL,
        date TEXT NOT NULL,
        url TEXT,
        file_path TEXT NOT NULL,
        FOREIGN KEY (company_id) REFERENCES companies (company_id)
    )
    """)

    # 3. Figures table (stores normalized, extracted numeric facts)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS figures (
        figure_id INTEGER PRIMARY KEY AUTOINCREMENT,
        report_id INTEGER,
        statement TEXT NOT NULL,
        metric TEXT NOT NULL,
        period TEXT NOT NULL,
        value REAL NOT NULL,
        unit TEXT NOT NULL,
        page INTEGER,
        FOREIGN KEY (report_id) REFERENCES documents (report_id)
    )
    """)

    conn.commit()
    conn.close()
    print("Database initialized successfully with schema tables: companies, documents, figures.")

if __name__ == "__main__":
    init_db()