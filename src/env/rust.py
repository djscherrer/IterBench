from env.base import SINGLE_FILE_APP_INSTRUCTIONS, Env

_WORKDIR = "/app"
_SRC_FILENAME = "main.rs"
_CARGO_TOML = "Cargo.toml"

SINGLE_FILE_APP_INSTRUCTIONS = (
    ""
    "Networking requirements: the server must listen on 0.0.0.0 and use the port provided in the PORT environment variable, defaulting to {port} if PORT is not set.\n"
    "Dependency requirements: You must ONLY use the dependencies listed in the Cargo.toml provided above. Do not introduce any new dependencies.\n"
    "If the deployment spec sets DB_POOL_SIZE, configure the deadpool max_size from it; "
    "the framework injects the env var but does not apply it automatically.\n"
)


def _build_rust_stub(port: int, needs_db: bool, needs_secret: bool) -> str:
    imports = ["use actix_web::{web, App, HttpServer};"]
    if needs_db:
        imports.append(
            "use deadpool_postgres::{Config as PoolConfig, Manager, ManagerConfig, Pool, RecyclingMethod, Runtime};"
        )
        imports.append("use tokio_postgres::NoTls;")
    imports.append("use std::env;")

    lines = imports + [""]
    lines.append(f"// Get port from environment variable, default to {port}")
    lines.append("fn get_port() -> u16 {")
    lines.append('    env::var("PORT")')
    lines.append(f'        .unwrap_or_else(|_| "{port}".to_string())')
    lines.append("        .parse()")
    lines.append(f"        .unwrap_or({port})")
    lines.append("}")

    if needs_secret:
        lines.append("")
        lines.append("// Get secret from environment variable if needed")
        lines.append("fn get_app_secret() -> String {")
        lines.append(
            '    env::var("APP_SECRET").unwrap_or_else(|_| "supers3cret".to_string())'
        )
        lines.append("}")

    if needs_db:
        lines.append("")
        lines.append("// Per-process DB client pool size from spec (DB_POOL_SIZE).")
        lines.append("fn db_pool_size() -> usize {")
        lines.append('    if let Ok(raw) = env::var("DB_POOL_SIZE") {')
        lines.append("        if let Ok(n) = raw.parse::<usize>() {")
        lines.append("            if n > 0 {")
        lines.append("                return n;")
        lines.append("            }")
        lines.append("        }")
        lines.append("    }")
        lines.append("    10")
        lines.append("}")
        lines.append("")
        lines.append("// Database configuration from environment variables")
        lines.append("fn create_pool() -> Pool {")
        lines.append("    let mut cfg = PoolConfig::new();")
        lines.append(
            '    cfg.host = Some(env::var("DB_HOST").unwrap_or_else(|_| "localhost".to_string()));'
        )
        lines.append(
            '    cfg.port = Some(env::var("DB_PORT").unwrap_or_else(|_| "5432".to_string()).parse().unwrap_or(5432));'
        )
        lines.append(
            '    cfg.user = Some(env::var("DB_USER").unwrap_or_else(|_| "postgres".to_string()));'
        )
        lines.append(
            '    cfg.password = Some(env::var("DB_PASSWORD").unwrap_or_else(|_| "postgres".to_string()));'
        )
        lines.append(
            '    cfg.dbname = Some(env::var("DB_NAME").unwrap_or_else(|_| "testdb".to_string()));'
        )
        lines.append(
            "    cfg.manager = Some(ManagerConfig { recycling_method: RecyclingMethod::Fast });"
        )
        lines.append("    cfg.pool = Some(deadpool::managed::PoolConfig::new(db_pool_size()));")
        lines.append("    cfg.create_pool(Some(Runtime::Tokio1), NoTls).unwrap()")
        lines.append("}")

    lines.extend(["", "// TODO: Implement your API endpoints here", ""])
    lines.append("#[actix_web::main]")
    lines.append("async fn main() -> std::io::Result<()> {")
    lines.append("    let port = get_port();")
    lines.append('    println!("Server starting on port {}", port);')

    if needs_db:
        lines.append("    let pool = create_pool();")

    lines.append("")
    lines.append("    // Optional concurrency knob provided by the harness/spec.")
    lines.append("    // Interpreted as *Actix worker threads* (not OS processes).")
    lines.append('    let web_concurrency: Option<usize> = std::env::var("WEB_CONCURRENCY")')
    lines.append("        .ok()")
    lines.append("        .and_then(|v| v.parse::<usize>().ok())")
    lines.append("        .filter(|&n| n > 0);")

    lines.append("    ")
    lines.append("    let mut server = HttpServer::new(move || {")

    if needs_db:
        lines.append("        App::new()")
        lines.append("            .app_data(web::Data::new(pool.clone()))")
        lines.append("            // Add your routes here")
    else:
        lines.append("        App::new()")
        lines.append("            // Add your routes here")

    lines.append("    });")
    lines.append("    if let Some(n) = web_concurrency {")
    lines.append("        server = server.workers(n);")
    lines.append("    }")
    lines.append('    server.bind(("0.0.0.0", port))?.run().await')
    lines.append("}")

    return "\n".join(lines)


_EMPTY_BRACKETS = "{{ App::new() }}"
_RUST_DOCKERFILE = f"""
FROM rust:1.95.0-bullseye
{{additional_commands}}

RUN apt-get update
RUN apt-get install imagemagick musl-dev build-essential git -y
RUN mkdir -p {_WORKDIR}
WORKDIR {_WORKDIR}

COPY . .

ENV RUST_BACKTRACE=1
ENV APP_SECRET=supers3cret

RUN cargo build

ENTRYPOINT {{entrypoint_cmd}}
"""

_CARGO_ACTIX = """
[package]
name = "server"
version = "0.1.0"
edition = "2021"

[[bin]]
name = "server"
path = "main.rs"

[dependencies]
actix-web = { version = "4.9.0" }
actix-multipart = { version = "0.7.2" }
clap = { version = "4", features = ["derive"] }
tempdir = "0.3"
serde = { version = "1.0", features = ["derive"] }
serde_json = "1.0"
tokio = { version = "1", features = ["full","macros", "rt-multi-thread"] }
current_platform = "*"
chrono = { version = "0.4", features = ["serde"] }
nix = { version = "0.29.0", features = ["signal"]}
tokio-postgres = { version = "0.7.12", features = ["with-chrono-0_4", "with-uuid-1"] }
deadpool-postgres = "0.14.0"
deadpool = { version = "0.12", features = ["rt_tokio_1"] }
env_logger = "0.11.6"
uuid = { version = "1.11.0", features = ["v4", "fast-rng", "macro-diagnostics"] }
futures-util = "0.3"
"""

RustActixEnv = Env(
    language="Rust",
    extension="rs",
    framework="Actix",
    env_instructions=SINGLE_FILE_APP_INSTRUCTIONS,
    code_filename=_SRC_FILENAME,
    dockerfile=_RUST_DOCKERFILE,
    workdir=_WORKDIR,
    database_type="postgresql",
    manifest_files={_CARGO_TOML: _CARGO_ACTIX},
    allowed_packages=_CARGO_ACTIX,
    is_multi_file=False,
    entrypoint_cmd="cargo run",
    stub_builder=_build_rust_stub,
    wait_to_start_time=90.0,
)
