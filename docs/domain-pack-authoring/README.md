# 用户引导式领域包制作规范

规范版本 1.0.0，2026-11-17。适用于用户、领域专家和开发者共同设计、补充及维护仿真领域包。

制作过程围绕用户的实际任务展开。系统持续引导用户讲清案例、取舍、失败、行业规程与验收经验，将资料整理为可定位、有条件、有标签、有采用记录的知识，再连接到模型、工具、工作流、检查与指标。

## 使用入口

| 资料 | 用途 |
|---|---|
| [01 制作目标与协作边界](01-scope-and-collaboration.md) | 用户、AI、领域审阅者、开发者分别做什么 |
| [02 引导流程与会话规范](02-guided-workshop.md) | 首轮怎样开始、怎样追问、怎样恢复及结束 |
| [03 行业案例、规范与经验注入](03-materials-and-experience.md) | 从资料和口述中提取可复用经验 |
| [04 标签、标注与知识表达](04-annotation-and-taxonomy.md) | 打什么标签、标到哪里、由谁确认和维护 |
| [05 调研与知识缺口补齐](05-domain-research.md) | 借鉴 scholar-apprentice 开展目标相关调研 |
| [06 入库与存储接口](06-repository-contracts.md) | 定义逻辑记录、审阅、写入、检索与迁移能力 |
| [07 从知识到可开发领域包](07-pack-assembly.md) | 将用户采用的知识映射为流程、检查和参数 |
| [08 开发实施与验收](08-development-and-acceptance.md) | 交互/服务边界、开发工作包和验收场景 |
| [09 完整引导演示](09-walkthroughs.md) | 小分子对接与有限元动力学的制作过程示例 |
| [10 来源、衔接与交接](10-provenance-and-handoff.md) | 既有规范与当前实现的关系、参考来源和交付状态 |

配套工件包括 [问答目录](question-catalog.json)、[交换记录 Schema](schemas/authoring-bundle.schema.json)、[空白制作档案](templates/authoring-session.json)、[资料采集表](templates/material-intake.md)、[经验访谈表](templates/experience-interview.md)、[案例复原表](templates/case-reconstruction.md)、[规范条款表](templates/standard-clause.md)、[审阅清单](templates/review-checklist.md) 和 [内容对照演示](examples/workshop-demo.json)。

采用前后的修订与用途对照可查看 [范围采用演示](examples/scoped-adoption-demo.json)。

## 开始制作一套包

系统先恢复已有决定和资料，再提出当前最影响设计的少量问题。一个可直接使用的首轮引导为“这套包先帮助谁完成什么任务？你手头哪份案例、规范或亲历经验最能说明这个任务？”用户可先讲经验、提交文件、给资料清单，或指出目前缺少什么。

每轮交付已明确的设计、原始材料/经验记录、标签建议、用户采用决定和下一项具体缺口。用户允许批量确认并可声明以后适用的整理规则。资料登记、摘要校验、索引更新等已授权机械步骤连续完成。

## 数据与版本

本规范定义与存储实现无关的实体、关系、读写语义和交换格式。物理数据库、索引技术、部署模式和产品选型保持待定。现有工程中的具体存储属于兼容适配背景，后续制作工具按本规范接口接入。

本轮完成规范、表单、交换 Schema、引导目录和构造演示。实际行业资料由用户在制作会话中注入，后续工具开发与领域验证按对应验收执行。实际检查结果见 [文档检查回执](evidence/validation.json)。
