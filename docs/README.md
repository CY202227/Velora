# 开发文档入口

当前开发者和后续代理必须先阅读：

想先理解执行过程，可读 [Agent Loop 讲解](agent-loop-walkthrough.md)，再阅读下面的开发约束。

1. [产品定义](velora-product.md)：当前产品范围。
2. [不可破坏的运行约束](runtime-invariants.md)：实现和评审的硬边界。
3. [Runtime 开发手册](runtime-development.md)：代码入口、配置、接口和恢复流程。
4. [验证与交付状态](runtime-validation.md)：已验证内容与尚未完成事项。
5. [分阶段计划](agent-runtime-plan.md)：演进过程，旧进展以最新核验为准。

文档纳入版本管理；聊天记录不是唯一设计依据。功能、配置、迁移或验收结果变化时同步更新。密钥、真实会话、检查点数据库和灰度原始数据不提交。

试用入口：[终端预览版](terminal.md)。

后续工作：[开发待办](todo.md)。

执行环境演进：[本地、服务端与设备执行架构设计](execution-architecture.md)（分阶段实施，实际范围以验证记录为准）。
