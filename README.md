# ElderAgent：面向老年用户的手机 GUI 智能体

老人用一句话说出需求（"给女儿打个电话""明早六点半叫我起床"），Agent 读取安卓屏幕的无障碍控件树，由大语言模型逐步规划并执行点击、输入、滑动。拨号、发送、支付、删除这类不可撤销的操作，执行前一律停下，交给老人本人确认。

本项目是厦门工学院大学生创新创业训练计划项目"面向老年群体的手机 Agent 辅助应用"的研究原型。

## 主要设计

| 模块 | 文件 | 说明 |
|---|---|---|
| 屏幕感知 | `agent/uitree.py` | 把无障碍 UI 树精简为紧凑文本：去除不可见节点和状态栏、可交互容器合并子节点文字、去除混淆的资源 ID、相同标签的父子节点合并。真机上约为原始 XML 的 1–2% |
| 规划执行 | `agent/agent.py` | 观察 → 规划 → 安全检查 → 执行 → 再观察的闭环，每步对照上一步的预期结果，不符时重新规划；另含"一次规划到底"的对照基线 |
| 安全守卫 | `agent/safety.py` | 敏感词规则 + 大模型意图核验 + 应用级风险画像。输出放行 / 请老人确认 / 拦截三档；密码和验证码一律不由 Agent 输入 |
| 本地脱敏 | `agent/privacy.py` | 发往云端前遮蔽任务未提及的手机号、验证码、取件码、证件号、卡号 |
| 设备控制 | `agent/device.py` | 仅依赖 adb + uiautomator2，不同品牌通用；已知号码、闹钟、倒计时等高频操作优先走系统 Intent，界面操作作为后备 |

## 运行

```bash
python3 -m venv .venv && .venv/bin/pip install uiautomator2
export DEEPSEEK_API_KEY=...          # 也可用 LLM_BASE_URL / LLM_MODEL 指定其他 OpenAI 兼容接口
cd agent
../.venv/bin/python run.py check                      # 环境自检
../.venv/bin/python run.py task call_contact_1        # 运行单个任务
TASKS=tasks_test.json ../.venv/bin/python run.py exp repr   # 运行实验组
../.venv/bin/python run.py stats                      # 汇总结果
../.venv/bin/python bench_safety.py                   # 安全守卫离线评测
```

中文输入需要在设备上安装 [ADBKeyBoard](https://github.com/senzhk/ADBKeyBoard)。

## 任务集与评测

- `tasks.json`：开发集，20 个任务，在华为真机（HarmonyOS 4.2）上开发调试时使用
- `tasks_dev_emu.json`：开发集的模拟器版本
- `tasks_test.json`：留出测试集，30 个任务，在任何调试之前编写，用于报告正式结果；运行于 Android 14 模拟器，每个任务开始前恢复同一快照

判定方式：
- `intercept`：在正确对象的最终不可逆操作前被安全守卫拦下
- `final`：任务结束时停在正确的应用和页面
- `state`：直接检查设备真实状态，例如闹钟数据库、通讯录、定时器、亮度

## 注意

在真机上运行时，Agent 会真实操作手机。建议使用测试机或模拟器，并只使用测试联系人。
