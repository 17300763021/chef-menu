# Luna Server MySQL 安装记录（MySQL-only）

日期：2026-09-08  
状态：已完成限定范围的功能验收；未进行容量压测或 Navicat 图形界面验收。

## 范围

本记录仅覆盖经确认的 **MySQL-only** 方案：在新服务器上安装 Docker Engine、Docker Compose 与一个本机回环地址访问的 MySQL 8.4 实例。

- 未安装 Supabase、PostgreSQL、Studio、Kong、Realtime 或 Storage。
- 初始安装验收未修改既有 Supabase、TiDB、应用代码、GitHub Actions、GitHub Secrets 或任何模拟账户数据。
- 初始安装验收完成后，另经确认将 TiDB 的 34 张行情表迁入 MySQL；这不是本安装报告原先“空库”验收的一部分。迁移后的表、数据集、检查点和发布记录仍全部是研究用途，不能产生模拟订单。

## 已部署内容

- Docker Engine `29.8.0`、Docker Compose `v5.5.1`。
- MySQL `8.4.11`，镜像固定为 `docker.io/library/mysql:8.4.11@sha256:b3b90af2a6552ae30c266fdb7d5dd55f3afb72404bb78d37fe8a23eb857fd3fb`，并在目标机核验为 `linux/amd64`；未使用 `latest` 或浮动标签，后续仍须单独评估安全更新。
- Compose 文件：`deploy/mysql/compose.yaml`；服务器部署目录：`/srv/quant-platform/mysql`。
- 数据卷：`quant-market-mysql-data`。备份目录：`/srv/quant-platform/backups`。
- MySQL 容器内部监听 `3306`；宿主机将其发布为 `0.0.0.0:13306`，并已核验公网 TLS 路由可达。该临时公网管理入口允许任意来源尝试连接，必须使用 TLS 和受限应用账号；应在完成云端验收后改回 SSH 隧道或来源白名单。
- 已创建应用数据库 `chef_menu_market` 和受限应用账户 `market_app`。root 账户只允许本机访问。
- MySQL 使用 UTF-8 (`utf8mb4`)、UTC、InnoDB、强制 TLS、20 个最大连接、128 MiB InnoDB 缓冲池等低资源限制。

Docker Hub 直连在该服务器网络中不可用，因此 Docker 使用腾讯云 VPC 的官方镜像加速地址；镜像完整性以已核验的 Docker 官方 digest 为准，镜像加速不构成长期可用性或供应链担保。未配置不安全镜像仓库，也没有暴露 Docker TCP Socket。临时 Debian 官方 APT 源仅用于完成受签名校验的 Docker 软件包安装，随后已删除；保留的是 Docker 官方软件源。

## 验收证据

以下检查均已通过：

1. Compose 渲染与启动健康检查通过；容器处于 `healthy`、无 OOM、无异常重启。
2. 宿主机回环路径 `127.0.0.1:3306` 的 TLS 客户端连接成功；公共 `13306` TLS 路由已核验可达。
3. 应用账户通过 TLS 连接，且仅拥有 `chef_menu_market.*` 范围的权限；root 没有 `%` 远程账户。
4. 写入、重复主键拒绝、事务回滚、中文 UTF-8 字节一致性、两次容器重启后的数据持久化均通过。
5. 初始安装验收已生成本地受限 SQL 备份，SHA-256 为 `49706856c52da3ecaba15a9849194be8db719e2f856d6655742e1b4c1c809986`；在独立临时数据库中恢复并逐字段核对通过。恢复后的临时数据库及验收表已删除。该“空库”结果只描述导入前状态，不描述当前已迁移的 34 张行情表。

后续 34 表迁移的可复核证据如下：源快照运行标识为 `tidb_market_20260908T033610Z`，共 34 张表、4,231,128 行、533 个字段和 149 个索引条目；行数清单 SHA-256 为 `f2b39607e2ec402431cc49f208da45449e38c3c5dd5b558a5fd570a58f00c5ac`，结构清单 SHA-256 为 `721b2021952dd73ce8e73f250cc46eb97bf3b43abe1e84d135e9fe07723fd465`，迁移 SQL 包 SHA-256 为 `48d03a4eaa6ec095462c3fdd3d2c635d8ad2111e938bc3d89e5b790f5e11d272`。迁移后研究边界汇总仍为 `authoritative=0`、`simulation_orders_allowed=0`；原 TiDB 数据未删除或改写。

## Navicat 连接方式

在 Navicat 中可使用直接 TLS 连接；更安全的长期方式仍是 SSH 隧道：

| 项目 | 值 |
| --- | --- |
| 直接连接主机 | 私有部署记录中的服务器公网地址 |
| 直接连接端口 | `13306` |
| SSH 隧道（推荐） | 启用后使用服务器地址、端口 `22` 和受控 SSH 账号 |
| SSH 隧道内 MySQL 主机 | `127.0.0.1` |
| SSH 隧道内 MySQL 端口 | `3306` |
| 数据库 | `chef_menu_market` |
| 用户 | `market_app` |
| TLS/SSL | 启用并要求加密连接 |

密码不记录在本文档中。MySQL 使用容器自动生成的自签名证书：已验证 TLS 加密通道，但尚未向 Navicat 配发受信任 CA，因此不能把当前状态表述为“客户端身份完全校验”。如需完整证书链校验，应在后续单独配置并分发 CA。

后续经批准的 GitHub Actions 任务应使用已核验的公共 `13306` TLS 路由和 GitHub Secrets 中的 MySQL 连接配置；本报告不包含主机、DSN 或密码。该云端迁移验收尚未启动，必须另行确认后再写入任何市场数据。

## 容量与运维限制

- 目标服务器约 2 GiB 内存、无 swap。空闲环境下 MySQL 容器约占 438 MiB；这只适用于低并发的 MySQL-only 用途，尚未进行负载、故障恢复或容量压测。
- 备份当前仅保存在该服务器本地，并非异地灾备。
- 现有腾讯 Debian 安全镜像的 Release 元数据已过期；此次没有改写其现有源配置。下次常规系统升级前，应先单独修复该镜像问题。
- 因资源限制，原文档中的完整 Supabase 自托管方案仍未实施，且 MySQL 不能替代 Supabase 的认证、对象存储、Realtime、REST 网关或 RLS 能力。
- 这台实例没有新增真实交易、券商连接、账户激活或自动下单能力；它仅可作为模拟研究的数据存储与迁移验证候选。
- 云端 MySQL 迁移验收、GitHub Actions 实际连接和任何新的检查点写入均为待单独批准事项；本次报告不把本地安装验收表述为云端数据迁移完成。

## 后续动作（均需另行确认）

1. 立即轮换曾在聊天中出现过的服务器 root 密码，并改用 SSH 密钥登录。
2. 建立受信任的 MySQL CA、Navicat 客户端验证配置和最小权限的只读/迁移角色。
3. 配置异地、加密、可演练的备份与恢复流程。
4. 在不影响既有云端模拟平台的前提下，另行评估数据迁移、应用连接和容量扩展。
