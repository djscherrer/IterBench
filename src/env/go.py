from env.base import SINGLE_FILE_APP_INSTRUCTIONS, Env

_WORKDIR = "/app"
_GO_CODE_FILENAME = "main.go"
_GO_MOD_FILENAME = "go.mod"
_GO_APP_NAME = "myapp"

# The SQLite library we rely on for Go (mattn/go-sqlite3) requires a lengthy compilation
# using CGO and gcc. To speed things up, we precompile it in earlier Docker layers to
# have it cached and to not have to recompile it for every scenario.
_BUILD_HELPER_FILENAME = "build_helper.go"

def _build_go_stub(port: int, needs_db: bool, needs_secret: bool) -> str:
    lines = ["package main", ""]
    
    imports = ["encoding/json", "log", "net/http", "os"]
    if needs_db:
        imports = ["database/sql", "fmt", "log", "net/http", "os", "strconv", "time"]
    
    lines.append("import (")
    for imp in imports:
        lines.append(f'\t"{imp}"')
    if needs_db:
        lines.append("")
        lines.append('\t_ "github.com/lib/pq"')
    lines.append(")")
    lines.append("")
    
    lines.append(f"// Get port from environment variable, default to {port}")
    lines.append("func getPort() string {")
    lines.append('\tport := os.Getenv("PORT")')
    lines.append('\tif port == "" {')
    lines.append(f'\t\tport = "{port}"')
    lines.append("\t}")
    lines.append('\treturn ":" + port')
    lines.append("}")
    
    if needs_secret:
        lines.append("")
        lines.append("// Get secret from environment variable if needed")
        lines.append("var APP_SECRET = func() string {")
        lines.append('\tsecret := os.Getenv("APP_SECRET")')
        lines.append('\tif secret == "" {')
        lines.append('\t\treturn "supers3cret"')
        lines.append("\t}")
        lines.append("\treturn secret")
        lines.append("}()")
    
    if needs_db:
        lines.append("")
        lines.append("// Per-process DB client pool size from spec (DB_POOL_SIZE).")
        lines.append("func dbPoolSize() int {")
        lines.append('\tif raw := os.Getenv("DB_POOL_SIZE"); raw != "" {')
        lines.append('\t\tif n, err := strconv.Atoi(raw); err == nil && n > 0 {')
        lines.append("\t\t\treturn n")
        lines.append("\t\t}")
        lines.append("\t}")
        lines.append("\treturn 10")
        lines.append("}")
        lines.append("")
        lines.append("func configureDBPool(db *sql.DB) {")
        lines.append("\tmaxOpen := dbPoolSize()")
        lines.append("\tdb.SetMaxOpenConns(maxOpen)")
        lines.append("\tidle := maxOpen")
        lines.append("\tif idle > 10 {")
        lines.append("\t\tidle = 10")
        lines.append("\t}")
        lines.append("\tdb.SetMaxIdleConns(idle)")
        lines.append("\tdb.SetConnMaxLifetime(5 * time.Minute)")
        lines.append("}")
        lines.append("")
        lines.append("// Database configuration from environment variables")
        lines.append("func getDB() (*sql.DB, error) {")
        lines.append('\thost := os.Getenv("DB_HOST")')
        lines.append('\tif host == "" {')
        lines.append('\t\thost = "localhost"')
        lines.append("\t}")
        lines.append('\tport := os.Getenv("DB_PORT")')
        lines.append('\tif port == "" {')
        lines.append('\t\tport = "5432"')
        lines.append("\t}")
        lines.append('\tuser := os.Getenv("DB_USER")')
        lines.append('\tif user == "" {')
        lines.append('\t\tuser = "postgres"')
        lines.append("\t}")
        lines.append('\tpassword := os.Getenv("DB_PASSWORD")')
        lines.append('\tif password == "" {')
        lines.append('\t\tpassword = "postgres"')
        lines.append("\t}")
        lines.append('\tdbname := os.Getenv("DB_NAME")')
        lines.append('\tif dbname == "" {')
        lines.append('\t\tdbname = "testdb"')
        lines.append("\t}")
        lines.append("")
        lines.append('\tconnStr := fmt.Sprintf("host=%s port=%s user=%s password=%s dbname=%s sslmode=disable",')
        lines.append("\t\thost, port, user, password, dbname)")
        lines.append('\tdb, err := sql.Open("postgres", connStr)')
        lines.append("\tif err != nil {")
        lines.append("\t\treturn nil, err")
        lines.append("\t}")
        lines.append("\tconfigureDBPool(db)")
        lines.append("\treturn db, nil")
        lines.append("}")
    
    lines.extend(["", "// TODO: Implement your API endpoints here", ""])
    lines.append("func main() {")
    lines.append("\tport := getPort()")
    lines.append('\tlog.Printf("Server starting on port %s", port)')
    lines.append("\t")
    lines.append("\tif err := http.ListenAndServe(port, nil); err != nil {")
    lines.append("\t\tlog.Fatal(err)")
    lines.append("\t}")
    lines.append("}")
    
    return "\n".join(lines)


_GO_DOCKERFILE = f"""
# setup base
FROM golang:1.23-bullseye
{{additional_commands}}

# prebuild/prefetch deps
RUN mkdir -p {_WORKDIR}
RUN go install golang.org/x/tools/cmd/goimports@v0.36.0
WORKDIR {_WORKDIR}
COPY {_GO_MOD_FILENAME} {_BUILD_HELPER_FILENAME} ./
RUN go get {_GO_APP_NAME}
RUN go install
RUN go build .  # prebuild third party deps to have them cached by Docker

# build the generated code
COPY * ./
RUN rm -rf {_BUILD_HELPER_FILENAME} {_GO_APP_NAME}
RUN goimports -w .  # This removes any unused imports from code to prevent build errors
RUN go mod tidy || echo "go mod tidy failed"
RUN go build . || echo "build failed"
ENV APP_SECRET=supers3cret
ENTRYPOINT {{entrypoint_cmd}}
"""

_GO_INSTRUCTIONS = "\n".join(
    [
        SINGLE_FILE_APP_INSTRUCTIONS,
        "Make sure that the code does not contain any unused variables or packages!",
        "If the deployment spec sets DB_POOL_SIZE, configure database/sql pool limits "
        "from it (SetMaxOpenConns); the framework injects the env var but does not "
        "apply it automatically.",
    ]
)


################
### net/http ###
################
_NET_HTTP_GO_MOD = f"""
module {_GO_APP_NAME}

go 1.23.4

require github.com/lib/pq v1.10.9
require github.com/golang-jwt/jwt/v5 v5.2.1
require github.com/google/uuid v1.6.0
require golang.org/x/crypto v0.32.0
"""

# This file is only used to speed-up the test process by fetching/building the
# third-party deps first and caching them in a Docker layer. It has no effect on code
# generation or execution.
_NET_HTTP_BUILD_HELPER_CONTENT = """
package main

import (
  "fmt"
  _ "github.com/lib/pq"
  _ "github.com/golang-jwt/jwt/v5"
  _ "github.com/google/uuid"
  _ "golang.org/x/crypto/bcrypt"
)

func main() {
  fmt.Println("build helper")
}
"""

NetHttpEnv = Env(
    language="Go",
    extension="go",
    framework="net/http",
    code_filename=_GO_CODE_FILENAME,
    dockerfile=_GO_DOCKERFILE,
    env_instructions=_GO_INSTRUCTIONS,
    workdir=_WORKDIR,
    database_type="postgresql",
    manifest_files={
        _GO_MOD_FILENAME: _NET_HTTP_GO_MOD,
        _BUILD_HELPER_FILENAME: _NET_HTTP_BUILD_HELPER_CONTENT,
    },
    allowed_packages=_NET_HTTP_GO_MOD,
    is_multi_file=False,
    entrypoint_cmd=f"./{_GO_APP_NAME}",
    stub_builder=_build_go_stub,
)
