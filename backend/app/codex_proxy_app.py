"""The standalone OpenAI-Responses proxy as its own FastAPI app.

为什么不复用 ``app.main``
------------------------

与 Claude 代理同因（见 :mod:`app.claude_proxy_app`）：本代理是后端在自己
lifespan 里拉起的**子进程**。若它 import ``app.main``，每个子进程都会进入同一
段 lifespan 并再拉起一个孙子进程——一个无界的进程树，每一代都占着一个 socket
和一个 token。这个模块存在的意义就是给出一个只含代理路由、**不可能重入
launcher** 的 app。

顺带带来的好处是子进程很轻：``app.main`` 会开数据库、同步内置技能、灌策略
并启动三个后台循环，而一个无状态的翻译器除了「一次一条的模型配置查询」之外
什么都不需要。保持 app 精简既让启动更快，也让子进程的影响面更小。
"""

from __future__ import annotations

from fastapi import FastAPI

from app.api.codex_proxy import router as codex_proxy_router


def create_proxy_app() -> FastAPI:
    """构建只含代理的 app。

    用工厂而不是模块级单例，是为了测试能按需构建一个而不占端口——与
    ``app.main`` 暴露 app 的方式一致。
    """

    app = FastAPI(
        title="Codex Responses proxy",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.include_router(codex_proxy_router)
    return app


app = create_proxy_app()