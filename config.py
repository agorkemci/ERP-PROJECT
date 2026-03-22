SERVER       = "localhost"
DB_NAME      = "TopTanERP_v4"
WINDOWS_AUTH = True
DB_USER      = "sa"
DB_PASSWORD  = ""

if WINDOWS_AUTH:
    CONNECTION_STRING = (
        f"mssql+pyodbc://@{SERVER}/{DB_NAME}"
        f"?driver=ODBC+Driver+17+for+SQL+Server&Trusted_Connection=yes"
    )
else:
    CONNECTION_STRING = (
        f"mssql+pyodbc://{DB_USER}:{DB_PASSWORD}@{SERVER}/{DB_NAME}"
        f"?driver=ODBC+Driver+17+for+SQL+Server"
    )

SECRET_KEY = "toptanerp-v4-acid-2026"
