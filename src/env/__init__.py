from env.base import Env
# from env.go import FiberEnv, GinEnv, NetHttpEnv
from env.go import NetHttpEnv
# from env.javascript import ExpressEnv, FastifyEnv, KoaEnv, NestJsEnv
from env.javascript import ExpressEnv
# from env.php import PhpLaravelLumenEnv
# from env.python import AioHttpEnv, DjangoEnv, FastAPIEnv, FlaskEnv
from env.python import FlaskEnv
# from env.ruby import RubyOnRailsEnv
from env.rust import RustActixEnv

all_envs: list[Env] = [
    # AioHttpEnv,
    # DjangoEnv,
    ExpressEnv,
    # FastAPIEnv,
    # FastifyEnv,
    # FiberEnv,
    FlaskEnv,
    # GinEnv,
    # KoaEnv,
    # NestJsEnv,
    NetHttpEnv,
    # PhpLaravelLumenEnv,
    # RubyOnRailsEnv,
    RustActixEnv,
]
