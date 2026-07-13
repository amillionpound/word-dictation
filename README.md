# Vocab Buddy - 单词听写系统

给孩子自助听写单词的 Web 应用，支持盲听/读听两种模式，AI 自动生成音标/释义/例句。

## 技术栈

- **后端**: Python Flask (腾讯云 SCF Web Function)
- **前端**: 单页 HTML + Vanilla JS
- **存储**: 腾讯云 COS
- **AI**: DeepSeek API (拼写校验 / 内容生成 / 词库结构化)
- **TTS**: 浏览器内置 Web Speech API (en-GB)

## 功能

- 三级权限: 超管 → 家长 → 孩子
- 家庭隔离，密码 SHA-256 哈希存储
- 4位录入码 (8小时有效)
- 年级 → 单元 → 课文三级词库选择
- 盲听模式 (限重听) / 读听模式 (无限重听)
- 成绩百分制自动计算，永久记录
- AI 用量统计

## 部署

### 环境变量

| 变量名 | 说明 |
|--------|------|
| `ADMIN_PWD` | 超管密码 (明文或 SHA-256 哈希，64位hex自动识别) |
| `DEEPSEEK_API_KEY` | DeepSeek API Key |
| `COS_SECRET_ID` | 腾讯云子账号 ID |
| `COS_SECRET_KEY` | 腾讯云子账号 Key |
| `COS_BUCKET` | COS 存储桶名 |
| `COS_REGION` | COS 地域 (如 ap-guangzhou) |

### CAM 权限

子账号需要 COS 的 `PutObject` / `PostObject` / `GetObject` / `HeadObject` 权限，resource 包含目标桶。

## 文件

- `app.py` - SCF Web Function 后端
- `index.html` - 前端单页应用
