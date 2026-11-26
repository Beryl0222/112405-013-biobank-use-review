# 生物样本研究限制与二次使用审查

项目面向样本库的**具体分配办理**：持续读取研究方案、同意书及伦理往来，辨认真正要做的
分析和产物用途，而不是按主题词给整项研究贴一个同意标签。系统只做规则化资料合成与流程
留痕，**不替代真实伦理审查**。

## 口径与边界

- 用途区分四桶：研究背景、拟执行操作、已有结果、将来共享计划，背景假说不构成实际操作。
- 同意按捐赠者、签署人资格、签署时年龄、有效期、用途例外、再联系限制逐项核对；
  研究者称"主题相近"不能扩大授权。
- 伦理人员可以确认有歧义的限制，但不能代替无资格的人签同意；未知签署人的授权不生效。
- 方案可只批准部分操作；未获准分析不能包装成已批准操作的附属步骤；
  必要操作无法合法取得输入时，该研究部分暂缓。
- 分装不产生新同意；同一有限体积不能在两个研究重复承诺；已耗尽容器不能仅改状态再分出。
- 撤回分三级：未发分配收回、已发未分析停止并确认剩余、已完成结果不能假装未形成，
  其共享仍受原文单独限制。
- 每次批准与实验发生时的依据都以快照形式留存在追加式账本中，可重放、可核验、不可改写。

## 目录结构

```
biobank/
  model.py       领域枚举与值对象（用途四桶、模态、产物、资格结论码、依据快照等）
  dates.py       日期解析（中文相对日期、"成年后一年"、推定标注）
  extraction.py  中文原文规则抽取：每条结论回引原句，无法解析即挂歧义
  consent.py     同意时间线：签署资格、有效期、撤回、再联系、用途例外、共享限制
  inventory.py   容器库存：派生谱系、体积台账、双重承诺拦截、归还/损耗/核对
  ledger.py      追加式 JSONL 事件账本（链式哈希完整性检查，重放重建状态）
  service.py     应用服务：资料载入、按操作决定、按容器发放回收、撤回级联
  views.py       伦理员/库管/研究者/捐赠者四类角色视图
  cli.py         命令行入口
tests/           43 个单元测试（含原有资料完整性检查）
data/case.json   合成资料包（原始文本、对象登记、历史办理记录）
docs/            资料口径与业务规则说明
```

## 使用

需要 Python 3.11+，无第三方运行依赖。

```sh
# 运行全部测试（隔离模式，不读取环境中的站点包）
python3 -I -m unittest discover -s tests -v

# 命令行办理（普通模式）
python3 -m biobank.cli -l runtime/ledger.jsonl load data/case.json
python3 -m biobank.cli -l runtime/ledger.jsonl ethics
python3 -m biobank.cli -l runtime/ledger.jsonl decide O12 --date 2026-11-26
python3 -m biobank.cli -l runtime/ledger.jsonl withdraw BR-W1
python3 -m biobank.cli -l runtime/ledger.jsonl reverify T8A 40
python3 -m biobank.cli -l runtime/ledger.jsonl issue O21 T8B 30 --date 2026-11-26
python3 -m biobank.cli -l runtime/ledger.jsonl verify
```

在 `python -I` 隔离模式下通过模块方式调用时，需先把工作区加入 `sys.path`：

```sh
python3 -I -c "import sys; sys.path.insert(0,'.'); from biobank.cli import main; main(['-l','runtime/ledger.jsonl','verify'])"
```

## 资料合成不是业务结论

抽取层识别不了的原文会保留为 `unparsed` 并挂歧义；无日期材料按保守口径处理并显式标注
"推定"；历史办理即使依据不足也只标注缺陷、不改写事实。所有提示项（如日期未知）与阻断项
（如授权排除、撤回、双重承诺）在结论码中分开，视图与决定都带原文编号可回溯。
