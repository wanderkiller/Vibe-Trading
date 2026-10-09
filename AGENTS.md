# 项目协作规则

本仓库是上游 [HKUDS/Vibe-Trading](https://github.com/HKUDS/Vibe-Trading) 的 fork，加一层本地补丁；用途是量化研究端：长历史数据、因子挖掘、参数扫描、walk-forward，产出 Strategy IR 交给 AlphaKeel（Rust 执行端）复核、paper、实盘。本仓库**不连接交易所、不下单、不写 AlphaKeel 的配置**。默认中文。分工与交接格式以 AlphaKeel `docs/32`（交接）、`docs/37`（离线研究服务）、`docs/43`（Strategy IR）为准，本文不重复。

## 补丁层

1. 本仓库 = 上游 `patches/BASE` 记录的提交 + `patches/MANIFEST.md` 列出的补丁组。改动优先放进叶子包 `agent/alphakeel_research/` 或新文件；改上游文件是最后手段。
2. 触碰上游文件必须同一改动里更新 `patches/MANIFEST.md` 与 `patches/anchors.json`（anchors 为准，`agent/tests/test_alphakeel_patch_layer.py` 校验），并写明该改动是只留本地还是可提上游。
3. 升级上游只走 `patches/UPGRADE.md` 的流程，在独立 worktree 做；rebase 能过不等于补丁仍然正确，每个 hook 的锚点和引擎/指标测试都要在新基线上重跑。
4. 不把本地部署文件（`Dockerfile.local`、`docker-compose.override.yml`、`DEPLOYMENT.local.md`）当上游文件改。

## 研究正确性（静默错误，测试查不出）

5. **按时间点取数。** 任何回测、因子、IC、对账只能用在决策时刻已可见的数据：K 线按 `close_time_ms`、bbo 按 `ts_recv_ms`、资金费结算按 `available_at_ms`（缺省用 `t + delay`），品种元数据取 `as_of` 之前最后一个快照。新增数据路径先写明它的可见性时间戳是哪个字段；写不出就不加。
6. **资金费、费率、金额用 `Decimal`**，不经 float 往返；费率的计息周期与是否年化写进字段名（如 `funding_hourly_net`、`funding_apr`），不靠注释。
7. **样本边界是契约。** 训练窗、样本外窗、AlphaKeel 持有的 hold-out 窗彼此不重叠；研究凭证的 hold-out 不得绕过（不用别的数据源补那段）。交接的 IR 在 `provenance` 里写清窗口、试验数、数据审计。
8. **与 AlphaKeel 对账以规范为准。** Python 与 Rust 求值器的语义以 AlphaKeel `docs/contracts/strategy-ir/README.md` 为准，两侧必须一致；唯一允许的例外是 `ir_crosscheck.py` 里单列、并引用 AlphaKeel 文档出处的已知差异（如引擎数量精度，docs/29 §16）。发现新差异先改规范，再两边同改，不在本地绕过或加进已知差异了事。
9. 回测通过只说明账务与执行一致，不说明策略有效；交付说明里的结论要分开写"引擎一致"和"策略评级"。

## 工程规则

10. 不加平行物：没有第二个真实需求，不新增第二套数据源封装、第二个求值器、第二份契约模型、第二条 CLI 路径。要加必须说明现有那份为什么不能扩展。
11. `agent/alphakeel_research/contract/` 是从 AlphaKeel 同步来的契约副本（openapi、strategy-ir schema、向量），由 AlphaKeel `tools/export-research-contract.sh` 导出（`PIN` 记录哈希），`models.py` 由 `gen_models.sh` 从它生成；两者都不手改，契约变更在 AlphaKeel 改源、重新导出并生成。
12. 密钥：研究服务凭证只经 `ALPHAKEEL_RESEARCH_TOKEN_FILE` 文件引用；日志、测试、git、聊天里不出现 token、API key、交易所密钥。
13. 验证：改到的模块跑 `ruff check` 和对应的 `pytest` 目标（`agent/tests/`，解释器 `/home/ubuntu/vt-phase2/.venv/bin/python`）；全量测试一批只跑一次。前端改动跑 `frontend` 的 vitest。不为求安心重跑。
14. 部署：生产是本机 Docker 容器（`DEPLOYMENT.local.md`），只在用户明确许可后重建；重建前备份镜像、env 与 volume（`~/vibe-trading-backups/`）。AlphaKeel 与本仓库的契约改动要一起部署，不单独上一边。
15. 交付说明列出：改动、验证命令与结果、未验证边界、状态（本地/已提交/已推送/已部署，带 hash）、回滚方式。
16. 用户授权范围内自主推进，不为日常可逆开发反复求确认；推主分支、重建容器、删数据、动凭证每次都问。
