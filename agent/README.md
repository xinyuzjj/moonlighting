# agent/ — 浏览器 Agent 的闭环层（P1）

补的是缺口文档里 L2 那三样：**每步校验、ref 失效重定位、预算**。没有 planner（P2）。

```
obs.py      /snap → 压缩到 ≤6KB（只留可操作节点）+ shot() 抓帧兜底
verify.py   断言 DSL：url / text / eval / count
act.py      11 个原子动作白名单：goto click type key scroll select wait read assert ask_user done
run.py      步骤执行 + 重试 + 断言 + 回滚 + forbid/ask_user/done_when + events.jsonl
mem.py      站点记忆：host+path → 成功动作序列
```

## 跑

```bash
cd F:/hermes-browser/agent
python run.py ../tasks/github-login-fields.json
```

输出 `runs/<ts>/events.jsonl`（每步动作、耗时、断言实际值）和 `result.json`。

## 实测（2026-10-18，本机 HermesDesk / CDP 18763）

| 项 | 数字 |
|---|---|
| 快照压缩 | MDN 42.4KB → 6.0KB（-86%）；B 站 29.9KB → 3.8KB（-87%） |
| 动作耗时 | goto 586ms、type 40~96ms、click 39ms |
| 空点击检出 | 故意点不存在的按钮 → 动作 FAIL、断言 FAIL，判为预期失败 |
| 失败恢复 | 同一步重试 3 次 → 第 2 次自动截图（28.9KB jpg，画面确认是登录页且账号已填） → 断言失败 → 回滚 milestone（URL + 28 个字段） |
| 站点记忆 | 成功任务写入 `mem/sites.jsonl`；下次跑打印「命中 github.com/login（N 步）」 |

## 设计取舍

- **正文不进上下文。** 压缩只留 link/button/textbox 等可操作节点；要内容用 `read` 按需取，
  否则 20 步就是 20 万 token。
- **完成判定只认断言。** 模型的自然语言"已完成"不参与判断。
- **没有 check 的步骤直接拒跑**（`run.py` 启动时检查），这是"点了没反应还继续走"的唯一根治点。
- **动作报错 ≠ 失败。** 先看断言；断言过了就算成功（点击超时但已生效是常见情况）。
- **重试有上限。** 每步 3 次、全局 `max_steps`；连败两次才截图，避免每步都存图。
- **回滚点是 milestone 而不是日志。** 每步成功后刷新（URL + 输入框值），失败时回跳重填。
- **动作空间是硬白名单。** 任务还能用 `actions_allow` / `forbid` 再收紧一层；越界直接拒跑。
- **人工闸门默认关。** `ask_user` 不带 `--yes` 一律阻塞——支付/发送/删除类动作走这里。

## 没做

- planner（`plan.py`）：P2。现在任务是显式步骤，人工当 planner——这正是缺口文档里的基线做法。
- 记忆回放：没有 planner 时回放等于跳过校验（实测会假通过），所以只记录+提示，回放留给 P2。
- 域名白名单：`ask_user` 已挡 commit 类动作，但"哪些域名算 write 可自动"没做。
- canvas / iframe：视觉兜底只到"截图存证"，没有多模态定位；跨域 iframe 没测。
- 弹窗自动关闭、成本度量（loop 里没有 LLM 调用，token 恒为 0，等 planner 才能测）。
- `bench.py` 10 页跑分：需要一个免登录目标站集合，现在 GitHub 会跳登录页。
