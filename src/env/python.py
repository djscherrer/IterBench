from env.base import MULTI_FILE_APP_PROMPT_STRUCTURE, SINGLE_FILE_APP_INSTRUCTIONS, Env

_PYTHON_INSTRUCTIONS = "\n".join(
    [
        SINGLE_FILE_APP_INSTRUCTIONS,
        "IMPORTANT: Your application will be run using gunicorn with with the --preload flag and multiple worker processes (one per CPU core).",
        "Ensure any initialization code (especially database setup) is safe for concurrent execution.",
    ]
)

_WORKDIR = "/app"
_PYTHON_CODE_FILENAME = "app.py"
_REQS_TXT_FILENAME = "requirements.txt"

def _build_flask_stub(port: int, needs_db: bool, needs_secret: bool) -> str:
    imports = ["import os"]
    if needs_db:
        imports.append("import psycopg2")
    imports.append("from flask import Flask")
    
    lines = imports + ["", "app = Flask(__name__)", ""]
    lines.append(f"# Get port from environment variable, default to {port}")
    lines.append(f"PORT = int(os.getenv('PORT', {port}))")
    
    if needs_secret:
        lines.append("")
        lines.append("# Get secret from environment variable if needed")
        lines.append("APP_SECRET = os.getenv('APP_SECRET', 'supers3cret')")
    
    if needs_db:
        lines.append("")
        lines.append("# Database configuration from environment variables")
        lines.append("DB_HOST = os.getenv('DB_HOST', 'localhost')")
        lines.append("DB_PORT = int(os.getenv('DB_PORT', 5432))")
        lines.append("DB_USER = os.getenv('DB_USER', 'postgres')")
        lines.append("DB_PASSWORD = os.getenv('DB_PASSWORD', 'postgres')")
        lines.append("DB_NAME = os.getenv('DB_NAME', 'testdb')")
        lines.append("")
        lines.append("def get_db():")
        lines.append("    conn = psycopg2.connect(")
        lines.append("        host=DB_HOST,")
        lines.append("        port=DB_PORT,")
        lines.append("        user=DB_USER,")
        lines.append("        password=DB_PASSWORD,")
        lines.append("        database=DB_NAME")
        lines.append("    )")
        lines.append("    return conn")
    
    lines.extend(["", "# TODO: Implement your API endpoints here", ""])
    lines.append("if __name__ == '__main__':")
    lines.append("    app.run(host='0.0.0.0', port=PORT)")
    
    return "\n".join(lines)


_PYTHON_DOCKERFILE = f"""
# setup base
FROM nikolaik/python-nodejs:python3.12-nodejs22-bullseye
{{additional_commands}}

# install deps and copy generated code
RUN mkdir -p {_WORKDIR}
WORKDIR {_WORKDIR}
COPY {_REQS_TXT_FILENAME} {_REQS_TXT_FILENAME}
RUN python3 -m pip install -r requirements.txt
ENV APP_SECRET=supers3cret
COPY / {_WORKDIR}/
ENTRYPOINT {{entrypoint_cmd}}
"""

#############
### Flask ###
#############
# The deps obtained from installing:
# Flask == 3.1.0
_FLASK_REQS_TXT = """
itsdangerous==2.2.0
blinker==1.9.0
click==8.1.7
Flask==3.1.0
Jinja2==3.1.4
MarkupSafe==3.0.2
Werkzeug==3.1.3
passlib==1.7.4
fpdf==1.7.2
pyjwt==2.10.1
psycopg2-binary==2.9.10
gunicorn==23.0.0
"""

FlaskEnv = Env(
    language="Python",
    extension="py",
    framework="Flask",
    env_instructions=_PYTHON_INSTRUCTIONS,
    code_filename=_PYTHON_CODE_FILENAME,
    dockerfile=_PYTHON_DOCKERFILE,
    workdir=_WORKDIR,
    database_type="postgresql",
    manifest_files={_REQS_TXT_FILENAME: _FLASK_REQS_TXT},
    allowed_packages=_FLASK_REQS_TXT,
    is_multi_file=False,
    entrypoint_cmd="gunicorn --preload --workers=$(nproc) --bind 0.0.0.0:$PORT app:app",
    process_name="gunicorn",
    stub_builder=_build_flask_stub,
)
