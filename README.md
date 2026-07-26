# H100 Spot Capacity Controller

**为 GPU 训练/推理稳定维持指定数量的 Spot 实例——用 Spot 的价格，不承担"悄悄超支"或"跨 Region 拆集群"的风险。**

如果你在 EC2 Spot 上跑 GPU 负载，你已经知道这个权衡：Spot 比 On-Demand 便宜 50-70%，但容量随时可能被回收、随时可能恢复。这个 controller 把"始终维持 N 台 GPU 机器"这件事自动化，让你不用手动盯着 Spot Fleet——同时对任何花钱或触碰集群的动作都保持刻意的保守。

## 这个适合你吗？

适合，如果你：
- 需要**稳定数量的 GPU Spot 机器**（比如"始终维持 4 台 p5.48xlarge"）用于训练或推理
- 想要 Spot 中断后的**自动恢复**，并且希望 fallback 顺序合理（同 AZ → 其他 AZ → Local Zone → 换 Region），而不想自己写这套逻辑
- 想要**有护栏的自动化，不是黑盒**——任何影响容量/花钱的动作都卡在一个可审查、会过期的显式审批后面
- 可能已经在跑 **EKS**，想让 controller 帮忙把节点喂饱，但不允许它碰 Kubernetes API 或干预集群状态

大概率不适合你，如果你需要的是 Kubernetes 原生弹性（Karpenter/Cluster Autoscaler）且不想要额外审批流程，或者一台常驻 On-Demand 机器对你来说就足够简单。

## 工作原理

```
                         ┌─────────────────────────────┐
                         │   EventBridge（定时调度）      │
                         └───────────────┬──────────────┘
                                          │ 每 1 / 5 / 15 分钟
                                          ▼
                         ┌─────────────────────────────┐
   已审核的 target ────► │   Reconciler Lambda          │ ───► CloudWatch 指标
   （存于 DynamoDB）      │   - 校验 target                │      + SNS 告警
                         │   - 发现已持有的容量            │
                         │   - 校验 GPU metadata          │
                         │   - 评分/选择 Region/AZ        │
                         └───────────────┬──────────────┘
                                          │ 创建 / 更新
                                          ▼
                         ┌─────────────────────────────┐
                         │   EC2 Fleet（持久化请求）      │
                         │   维持 N 台 Spot 机器           │
                         └─────────────────────────────┘
```

**容量短缺时的分配顺序**：优先 AZ → 持续短缺后扩到其他已批准的 AZ → 最后才 fallback 到已批准的 Local Zone。只要已持有容量，Region 就不会自动变动——换 Region 永远是一次刻意、经审批的动作，绝不会因为某天 Spot 供给差就自动触发。

**跨 Region 迁移**（如果真的需要）只遵守一条铁律：先停掉并完全验证源 Region 容量归零，才会在目标 Region 发起请求。controller 永远不会让你的负载同时跨两个 Region 分裂运行。

## 它绝对不会做的事

- 第一次部署就创建容量——它默认**disabled**，这是故意的
- 请求 On-Demand 容量（On-Demand target 永远是 0，只用来推导 Spot 价格上限）
- 接受一个没有通过 AWS metadata 确认 GPU 数量为正的实例类型（不会误建 CPU-only 的 Fleet）
- 让目标容量同时分裂在两个 Region
- 调用 Kubernetes API、创建 EKS 集群，或者根据 pod/node 就绪状态反向调整 Fleet 容量
- 未经显式、经过审查的人工审批就做任何写操作——部署、启用、取消 Fleet、终止实例、跨 Region 迁移，一概不例外

## 架构

| 组件 | 职责 |
|---|---|
| CDK stack | 部署 Lambda controller、DynamoDB 状态表、EventBridge 调度、SNS、CloudWatch 指标/告警和 dashboard |
| Reconciler Lambda | 每 1-15 分钟跑一次。校验 target、发现自己已持有的容量、收集 Spot Placement Score/价格/quota 证据，在已批准的合约范围内创建或更新 Fleet |
| EC2 Fleet | 在两次 reconcile 之间持续维持目标数量的 Spot 机器 |
| DynamoDB | 唯一真相源：target 配置版本、当前 Region/AZ 状态、迁移计划、审批记录、执行锁 |
| CLI（`h100-spot-controller`） | 所有手动操作入口：只读校验、target 持久化、failover 审批、清理命令 |

**两种部署模式：**
- `standalone` —— controller 就是全部，单纯维持 N 台 Spot 机器
- `existing-eks` —— 你已经有 EKS 集群，controller 帮它把 EC2 容量喂饱。它**从不调用 Kubernetes API**，节点的 bootstrap/labels/taint 都是你的 launch template 自己负责的事。如果节点注册失败，那是 bootstrap 或 EKS 访问权限的问题——controller **不会**因此去多拉容量。你可以选择把就绪快照（`registered_node_count`、`ready_node_count`）写进 DynamoDB，controller 会把它发布成独立的 CloudWatch 指标，纯粹用于观测——它永远不会反过来影响 Fleet 的期望容量。

## 快速开始（只读，随处可安全运行）

```sh
python -m venv .venv
.venv/bin/pip install -r requirements.lock

# 下面四条命令都是只读的，不会创建任何 AWS 容量
.venv/bin/h100-spot-controller validate-target config/target.example.yaml
.venv/bin/h100-spot-controller dry-run config/target.example.yaml --profile default
.venv/bin/h100-spot-controller discover config/target.example.yaml --profile default
.venv/bin/h100-spot-controller capacity-review config/target.example.yaml --profile default
```

`config/target.example.yaml` 默认是 disabled 且带占位符 AWS ID——从它开始改，别提交填好的版本（已经在 `.gitignore` 里了）。

准备部署或启用容量了？那是另一条需要显式授权的路径——动手前先过一遍 [部署前检查清单](docs/deployment-preflight-checklist.md) 和 [运维手册](docs/operator-runbook.md)。

## 配置一个 target

一个 target（YAML）描述你想维持的容量。核心字段：

| 字段 | 含义 |
|---|---|
| `desired_instance_count` / `maximum_instance_count` | 要维持的机器数（不是 GPU 数）。默认 `1`/`1` |
| `instance_types` | 你的负载能接受的 EC2 GPU 类型。使用前会先过 AWS metadata 校验——绝不会静默替换成别的类型 |
| `candidate_regions` | 有序、已批准的候选 Region 及其 Launch Template、AMI、安全组、子网 |
| `region_selection` | `manual`（你手动指定 Region）/ `recommend`（只做建议，不自动执行任何动作）/ `auto_initial`（controller 只在还没持有任何容量时自动选一次） |
| `enabled` | 必须保持 `false`，直到 target 经过审查、且容量启用单独获得批准 |

完整字段说明见 [`config/target.example.yaml`](config/target.example.yaml)。

## 已在真实 AWS 上验证

在 Seoul 用一个受限的 `g6e.xlarge` Spot target 端到端验证过通用路径：GPU metadata 正确识别出了 L40S（同一路径也能识别 P5 系列 H100/H200），一个 CPU-only 的类型在发起 Fleet 请求前被拒绝，一次运维发起的终止操作触发了自动补位，且始终没有超过配置的上限。121 个测试覆盖了 controller、安全边界、部署模板和 Region/AZ 选型逻辑。

```sh
JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION=1 .venv/bin/python -m unittest discover -s tests -q
openspec validate --specs --strict
```

需求规范维护在 [`openspec/specs`](openspec/specs)；已完成的设计变更归档在 [`openspec/changes/archive`](openspec/changes/archive)。

## 延伸阅读

- [运维手册](docs/operator-runbook.md) —— 部署、启用、failover、清理的完整流程
- [部署前检查清单](docs/deployment-preflight-checklist.md) —— 任何 AWS 写操作前的审批闸门
- [验证证据模板](docs/validation-evidence-template.md) —— Seoul 测试运行的记录方式
