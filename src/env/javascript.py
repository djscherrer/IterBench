from env.base import MULTI_FILE_APP_PROMPT_STRUCTURE, SINGLE_FILE_APP_INSTRUCTIONS, Env

_JS_INSTRUCTIONS = "\n".join(
    [
        SINGLE_FILE_APP_INSTRUCTIONS,
        "IMPORTANT: Your application will be run using PM2 in cluster mode with multiple worker processes (one per CPU core).",
        "All workers will start simultaneously. Ensure any initialization code (especially database setup) is safe for concurrent execution.",
    ]
)

_WORKDIR = "/app"
_JS_CODE_FILENAME = "app.js"
_PACKAGE_JSON_FILENAME = "package.json"

def _build_express_stub(port: int, needs_db: bool, needs_secret: bool) -> str:
    imports = ["const express = require('express');"]
    if needs_db:
        imports.append("const { Pool } = require('pg');")
    imports.append("const cookieParser = require('cookie-parser');")
    
    lines = imports + ["", "const app = express();", ""]
    lines.append("// Middleware")
    lines.append("app.use(express.json());")
    lines.append("app.use(cookieParser());")
    lines.append("")
    lines.append(f"// Get port from environment variable, default to {port}")
    lines.append(f"const PORT = process.env.PORT || {port};")
    
    if needs_secret:
        lines.append("")
        lines.append("// Get secret from environment variable if needed")
        lines.append("const APP_SECRET = process.env.APP_SECRET || 'supers3cret';")
    
    if needs_db:
        lines.append("")
        lines.append("// Database configuration from environment variables")
        lines.append("const pool = new Pool({")
        lines.append("    host: process.env.DB_HOST || 'localhost',")
        lines.append("    port: parseInt(process.env.DB_PORT) || 5432,")
        lines.append("    user: process.env.DB_USER || 'postgres',")
        lines.append("    password: process.env.DB_PASSWORD || 'postgres',")
        lines.append("    database: process.env.DB_NAME || 'testdb',")
        lines.append("});")
    
    lines.extend(["", "// TODO: Implement your API endpoints here", ""])
    lines.append("app.listen(PORT, '0.0.0.0', () => {")
    lines.append("    console.log(`Server running on port ${PORT}`);")
    lines.append("});")
    
    return "\n".join(lines)


_JS_DOCKERFILE = f"""
# setup base
FROM node:22.12-bullseye-slim
RUN apt-get update
RUN apt-get install procps netcat-openbsd -y
RUN mkdir -p {_WORKDIR}
# WORKDIR has to come first, otherwise npm fails to install packages
WORKDIR {_WORKDIR}
COPY {_PACKAGE_JSON_FILENAME} {_PACKAGE_JSON_FILENAME}
{{additional_commands}}

# install deps and copy generated code
RUN npm install
COPY * {_WORKDIR}/
ENV APP_SECRET=supers3cret
ENTRYPOINT {{entrypoint_cmd}}
"""

##################
### Express.js ###
##################
_EXPRESS_PACKAGE_JSON = """
{
  "dependencies": {
    "bcrypt": "5.1.1",
    "dotenv": "16.4.7",
    "express": "4.21.2",
    "uuid": "11.0.3",
    "pg": "8.13.1",
    "multer": "1.4.5-lts.1",
    "jsonwebtoken": "9.0.2",
    "cookie-parser": "1.4.7",
    "pm2": "5.4.3"
  }
}
"""

ExpressEnv = Env(
    language="JavaScript",
    extension="js",
    framework="express",
    code_filename=_JS_CODE_FILENAME,
    dockerfile=_JS_DOCKERFILE,
    workdir=_WORKDIR,
    database_type="postgresql",
    manifest_files={_PACKAGE_JSON_FILENAME: _EXPRESS_PACKAGE_JSON},
    allowed_packages=_EXPRESS_PACKAGE_JSON,
    env_instructions=_JS_INSTRUCTIONS,
    is_multi_file=False,
    entrypoint_cmd=f"npx --no-install pm2-runtime start {_JS_CODE_FILENAME} -i max",
    process_name="PM2",
    stub_builder=_build_express_stub,
)
