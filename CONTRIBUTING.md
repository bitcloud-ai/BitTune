# Contributing to BitTune

感谢你考虑为 BitTune 贡献代码！

## 行为准则

本项目遵循 [Contributor Covenant](CODE_OF_CONDUCT.md) 行为准则。参与即表示同意遵守。

## 如何贡献

### 1. 提 Issue

- **Bug 报告**：使用 Bug Report 模板，描述清楚复现步骤、预期行为、实际行为、环境信息
- **功能建议**：使用 Feature Request 模板，描述使用场景和期望效果

### 2. 提交 Pull Request

1. Fork 本仓库
2. 创建你的特性分支：`git checkout -b feat/your-feature` 或 `git checkout -b fix/your-fix`
3. 确保代码通过标准检查：

   ```bash
   uv sync --all-extras
   uv run ruff check .
   uv run ruff format --check .
   uv run mypy src/autopilot runner
   uv run pytest tests/unit tests/contract
   ```

4. 提交代码：`git commit -m 'feat: add some feature'`
5. 推送到你的 Fork：`git push origin feat/your-feature`
6. 在 GitHub 上打开 Pull Request

### 3. 开发指南

- 确保代码类型标注完整（mypy strict 模式）
- 新功能需要添加对应测试
- 保持文档同步更新
- 遵循 [Conventional Commits](https://www.conventionalcommits.org/) 提交规范
- 代码风格遵循 ruff 配置

### 4. 代码审查

所有 PR 需要至少 1 位核心维护者 Review 后才能合并。CI 检查必须全部通过。

### 5. 项目结构

```
src/autopilot/     # 主代码
tests/             # 测试（unit + contract）
runner/            # Docker runner
docs/              # 文档
scripts/           # 工具脚本
```

### 6. 环境要求

- Python 3.12
- uv 0.12.x
- Linux 是 GPU 测试的唯一正式环境
- Windows 可运行领域逻辑、Golden、Contract 和不依赖 Linux 的集成测试