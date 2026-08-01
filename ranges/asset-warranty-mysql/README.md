# Asset Warranty MySQL Lab

独立的真实 MySQL 8.4 资产保修核验靶场。它与平台持久化 MySQL、Backend、Bridge、Runner 完全隔离。

## 启动

```powershell
Copy-Item .env.example .env
# 修改 .env 中的两个密码
docker compose up -d --build
```

服务：

- Web：`asset-warranty-web`，宿主机 `28036`，内部监听 `5000`
- DB：`asset-warranty-db`，只加入 `asset-warranty-net`，不发布宿主机 3306
- Volume：`asset-warranty-mysql-data`
- Network：`asset-warranty-net`

`WARRANTY_FLAG` 仅作为部署时生成并写入 `challenge_settings` 的内部值，不会出现在接口响应或 Challenge metadata 中。

## 靶场验收

在 Compose 网络内运行：

```powershell
docker compose --profile test run --rm asset-warranty-tests
```

测试容器会通过 `asset-warranty-web` 服务名访问 Web，并通过 root 账户创建/删除随机临时表，以验证 `information_schema` 的动态可见性。Runner VM 的目标地址为：

```text
http://192.168.236.1:28036/api/warranty/check
```

接口故意只让 `department` 进入 SQL 字符串拼接，`asset_no` 使用参数化绑定；所有表达式由真实 MySQL 8.4 解析执行。
