# 内容工作台 · 公众号 / 头条号 内容 Agent

一个自托管的**内容生产流水线**：从热榜选题、AI 写稿、排版、配图、做封面，一直到**存进公众号 / 头条号的草稿箱**。

不是"AI 写一篇给你复制粘贴"，而是把「选题 → 写作 → 自检 → 排版 → 配图 → 封面 → 投递草稿」整条链路串起来，
每一环都有硬指标回读，出问题能看见是哪一环。**发布永远需要人工点确认**，程序不会自己发东西出去。

```
热榜/自定选题 ──▶ 写稿(LLM) ──▶ 自检(字数/结构/违禁词)
                                    │
                                    ▼
              存草稿(公众号接口 / 头条浏览器) ◀── 封面 ◀── 配图/AI生图 ◀── 排版(内联样式)
                                    │
                                    ▼
                          人工确认后再发布
```

---

## 能力矩阵

| 能力 | 公众号 | 头条号 | 大众点评 |
|---|---|---|---|
| 存草稿 | ✅ 官方接口 | ✅ 浏览器自动化 | ❌ 未接入 |
| 定时任务 | ✅ | ✅ | — |
| 正文配图 | ✅ 自动抓图 / AI 生图 | ✅ 同左 | — |
| 封面（标题压图） | ✅ | ✅ | — |
| 正式发布 | ⚠️ 需人工二次确认 | ❌ 只做草稿 | — |

> 大众点评没有对外发文接口，目前只做了账号、人设、排版主题的存储位，等有需要再单独做后台自动化。

## 功能

**写作**

- 热搜选题（微博热榜）→ 一键挑题、批量生成
- 每号一套**人设预设**（美妆/穿搭/美食/科技/汽车/历史/财经/生活/旅行）与写作规范
- 生成过程可视化：写作、配图、排版、投递分阶段有进度和日志
- 自检：字数区间、加粗/小标题结构、违禁词与极限词

**排版**

- 13 套主题，全部**内联样式**（公众号正文不支持 `<style>` 和 class）
- 主题可按账号绑定，AI 还能按文章内容再优化一遍段落结构
- 另有「看图日常」主题，专给以图为主的稿子用

**配图与封面**

- 按选题自动联网抓图 + 相关性排序（可选按模型打分）
- 也可 AI 生图；封面自动压中文标题（含换行、字号自适应）
- 生图提示词按**摄影逻辑**拼：具体焦距和光圈、光从哪来、能数出来的质感、不完美构图，
  自带一组反 AI 味的负向词；7 种写实风格可选（手机随手拍 / 纪实 / 胶片 / 硬光街拍 / 居家暖光 / 棚拍）
- 每张配图可单独「AI 重生成」并**就地替换**，支持写补充要求

**虚拟人物号（本项目比较特别的一块）**

- 建人物 → 传 1~3 张参考照 → 绑到账号
- 之后这个号的配图都用**同一个人**，机位/动作/表情自动轮换，不会老是正面大头照
- **内容模式**可切「长文」或「人物日常」：后者出几张人物图 + 几句口语（要有具体事件和吐槽），适合纯晒人的号

**投递**

- 公众号：建草稿走接口，正文图和封面两套接口分开走，回读校验图片数量
- 头条号：扫码登录一次，之后无头浏览器填写发文页并存草稿；**不碰发布**

---

## 3 分钟跑起来（Docker）

```bash
git clone https://github.com/<your-name>/<repo>.git
cd <repo>

cp .env.example .env
# 编辑 .env，至少把 WXWB_ADMIN_PASSWORD 设成你自己的

docker compose up -d
docker compose logs -f      # 看启动日志
```

打开 <http://localhost:8899>，用 `.env` 里的账号密码登录。

数据（数据库、上传的图、封面、日志、加密密钥）都在 `./data` 下，**备份就备份这一个目录**。

### 国内构建加速

镜像里要下 Chromium 和 apt 包，国内直连很慢（实测官方 CDN 0KB/s）。三个 arg 都支持换源：

```bash
docker build \
  --build-arg APT_MIRROR=mirrors.tuna.tsinghua.edu.cn \
  --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
  --build-arg PLAYWRIGHT_DOWNLOAD_HOST=https://cdn.npmmirror.com/binaries/playwright \
  -t wxwb:latest .
```

浏览器那个源默认就走 npmmirror（国内外都通，实测 19MB/s），一般不用改。

### 不要浏览器（只做公众号）

头条号草稿是浏览器自动化做的，镜像里因此带了 Chromium，体积约 2.3G。
不需要头条就用精简版，400M 左右：

```bash
docker build -f Dockerfile.slim -t wxwb:slim .
docker run -d --name wxwb -p 8899:8899 -v ./data:/data \
  -e WXWB_ADMIN_PASSWORD='换个强密码' wxwb:slim
```

> 实测过：容器内健康检查 200、登录正常、中文字体齐、Chromium 能启动（159.0.8154.6）。

### 挂在子路径 / 反向代理后面

Caddy 例子（把 `https://your-domain.com/wxwb/*` 转给容器）：

```caddyfile
your-domain.com {
    handle_path /wxwb/* {
        reverse_proxy 127.0.0.1:8899
    }
}
```

对应在 `.env` 里设 `WXWB_PREFIX=/wxwb`、`WXWB_PUBLIC_URL=https://your-domain.com/wxwb`。
后者只有用「人物参考图」生图时才必须——生图服务得能下载到你的参考图。

---

## 不用 Docker

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 头条号功能需要浏览器内核
python -m playwright install --with-deps chromium

# 缺中文字体的话封面会变方块（Debian/Ubuntu）
sudo apt install fonts-noto-cjk

WXWB_ADMIN_PASSWORD='换个强密码' python run.py --host 0.0.0.0 --port 8899
```

Windows 上用 `tools/install_browser.py` 装浏览器更省心（走国内镜像）：

```bash
python tools/install_browser.py --check     # 看装没装
python tools/install_browser.py             # 装
```

---

## 首次配置

1. **模型**：设置页填一个 OpenAI 兼容的对话模型。默认给 DeepSeek（`https://api.deepseek.com` + `deepseek-chat`），国内直连、一个 key 就能用。
2. **账号**：
   - 公众号：填 AppID / AppSecret（服务器出口 IP 要在这个号的**接口 IP 白名单**里，否则会 40164）
   - 头条号：账号页点「扫码登录」，用今日头条 App 扫一次，登录态存在本机
3. **AI 生图**（可选）：设置页选后端（智谱 CogView / 通义万相 / 本地 SD / 任意 OpenAI 兼容中转），填 key。想用虚拟人物，还要填「参考图模型」。

配完就能在工作台选个选题、点生成。

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `WXWB_DATA` | `./data` | 数据目录（数据库、图片、封面、日志、密钥） |
| `WXWB_HOST` / `WXWB_PORT` | `127.0.0.1` / `8899` | 监听地址；Docker 里是 `0.0.0.0` |
| `WXWB_PREFIX` | 空 | 反代子路径，比如 `/wxwb` |
| `WXWB_PUBLIC_URL` | 空 | 本站公网地址，人物参考图生图要用 |
| `WXWB_ADMIN_USER` / `WXWB_ADMIN_PASSWORD` | `admin` / 随机 | 首次启动创建的管理员；密码不填会随机并打印一次 |
| `WXWB_FONT` | 空 | 指定封面用的中文字体路径 |

其它配置（模型、人设、主题、生图、定时任务）都在网页里配，存在数据库。

## 数据与备份

```
data/
├── workbench.db      # 账号、稿件、选题、定时任务（SQLite）
├── secret.key        # 加密 AppSecret / API Key 用的密钥
├── uploads/          # 上传和生成的图片
├── covers/  articles/
└── logs/
```

- AppSecret 和各家 API Key 在库里是 **Fernet 加密**的，密钥是 `secret.key`
- 所以 `data/` 要一起备份，**丢了 `secret.key` 就得重新填一遍所有密钥**
- `data/` 已在 `.gitignore` 里，不要提交

## 安全须知（认真看）

这是一个**单人/小团队自用的后台**，不是多租户 SaaS，默认假设放在内网或反代后面：

- **必须走 HTTPS**。登录密码是明文 POST，明文 HTTP 下等于裸奔
- 不要直接 `0.0.0.0:8899` 暴露到公网；用 Caddy/Nginx 反代 + TLS
- 头条号的登录态（cookies / 浏览器 profile）就存在 `data/` 里，等于账号密码，别乱传
- 文章发布设计成**二次确认**，就是防手滑。别把 `allow_publish` 当成"随便发"的开关

## 常见问题

**公众号报 `40164 invalid ip`**
服务器出口 IP 不在白名单。到公众号后台「设置与开发 → 基本配置 → IP 白名单」加上。

**头条草稿存不上，接口返回 `7050 保存失败`**
不是内容问题，是头条服务端在看请求头的 `sec-ch-ua`——无头浏览器会带 `HeadlessChrome`，
一律拒。项目里已经用 CDP 把客户端提示覆盖成正常 Chrome 了；如果又出现，说明头条加了新检测。
详细排查过程见 [docs/notes.md](docs/notes.md)。

**头条草稿存出来正文是空的**
编辑器里按 ESC 会让页面进入「放弃编辑」，之后的自动保存就不带正文了。
代码里已经不再按 ESC，并会核对编辑器实际字数，填不进去就中止，不会留空稿。

**封面中文变成方块**
缺中文字体。装 `fonts-noto-cjk`，或用 `WXWB_FONT` 指定一个中文字体文件。

**AI 生图报「积分不足」/ `402`**
生图服务账户余额问题，跟代码无关。项目会把 401/402/429 翻成人话直接告诉你。

更多坑（公众号图片接口、排版内联样式、人物一致性、字段名兼容等）都在 [docs/notes.md](docs/notes.md)。

## 代码结构

```
app/
├── main.py        FastAPI 路由、登录会话、页面渲染
├── pipeline.py    流水线编排：写稿→自检→排版→配图→封面→投递
├── writer.py      LLM：写稿、起标题、压缩、去 AI 味、排版优化
├── render.py      Markdown → 内联样式 HTML（13 套主题）
├── checks.py      自检：字数、结构、违禁词
├── images.py      联网抓图 + 校验
├── imgsource.py   搜图候选源
├── aigen.py       AI 生图（多后端 + 人物参考）
├── photo.py       写实提示词工程：焦距/光线/质感/负向词、风格预设
├── characters.py  虚拟人物：参考图、外形描述
├── cover.py       封面：标题压图
├── wechat.py      公众号接口客户端
├── toutiao.py     头条号浏览器自动化
├── topics.py      热榜选题
├── scheduler.py   定时任务
├── personas.py    人设预设
├── db.py          SQLite + 轻量迁移
└── templates/ static/
tools/             浏览器安装、头条校准 CLI
deploy/            可选：VPS 一键部署脚本（systemd + Caddy）
```

技术栈：FastAPI + Jinja2 + SQLite + 原生 JS，没有前端构建步骤。Playwright 只在头条号那条链路上用。

## 路线图

- [ ] 大众点评发布通道
- [ ] 更多虚拟人物一致性方案（PhotoMaker / InstantID）
- [ ] 配图批量重生成、A/B 封面
- [ ] 多用户与权限
- [ ] 数据导出（Markdown / Word）

## 贡献

欢迎 issue / PR。改代码前建议先看 [docs/notes.md](docs/notes.md)，里面记了不少"看着能跑其实是坑"的地方。

## 许可

[MIT](LICENSE)
