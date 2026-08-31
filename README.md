# 游戏平台爬虫（API 直接抓取模式）

不使用浏览器，直接调用厂商后台 API，速度快、稳定可靠。

## 功能

- **来源管理**：添加/删除厂商后台账号
- **登录**：自动获取验证码图片，人工识别后登录，获取 access_token（有效期7天）
- **抓取数据**：选择数据类型、日期范围，自动翻页抓取，保存到本地数据库
- **数据查看**：查看抓取任务和详细数据

## 支持的数据类型

- 玩家账号信息
- 游戏充值详单
- 游戏角色信息
- 角色更新记录
- 小组数据信息
- 员工数据信息

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 启动服务

双击 `启动爬虫.bat`，或命令行执行：

```bash
python -m uvicorn app:app --host 0.0.0.0 --port 8000
```

### 3. 打开界面

浏览器访问：http://localhost:8000

## 使用流程

### 第一步：添加来源

在"来源管理"标签页，填写来源名称、厂商后台账号、密码，点击"添加来源"。

### 第二步：登录

1. 切换到"登录"标签页
2. 选择刚才添加的来源
3. 点击"获取验证码并登录"
4. 页面会显示验证码图片，人工识别后输入验证码
5. 点击"确认登录"
6. 登录成功后，access_token 会自动保存（有效期7天）

### 第三步：抓取数据

1. 切换到"抓取数据"标签页
2. 选择来源和数据类型
3. 填写日期范围（格式：开始~结束，如 2026-08-01~2026-08-29）
4. 可选：填写搜索账号（玩家登录名）
5. 设置最大翻页数
6. 点击"开始抓取"
7. 抓取完成后会显示抓取结果

### 第四步：查看数据

1. 切换到"数据查看"标签页
2. 选择来源
3. 点击抓取任务的"查看"按钮
4. 查看详细数据表格

## 项目结构

```
game-platform-pachong/
├── app.py              # FastAPI 后端，提供操作界面和 API
├── crawler.py          # 爬虫核心（纯 API 抓取：登录、验证码、数据抓取）
├── storage.py          # 数据库存储（SQLite）
├── static/
│   └── index.html      # 前端操作界面
├── data/
│   └── crawler.db      # SQLite 数据库（自动创建）
├── captcha/            # 验证码图片（自动创建）
├── requirements.txt    # 依赖清单
├── 启动爬虫.bat         # 一键启动脚本
└── README.md           # 本文件
```

## API 接口说明

### 来源管理

- `GET /api/sources` — 来源列表
- `POST /api/sources` — 新增来源 `{name, username, password}`
- `DELETE /api/sources/{id}` — 删除来源
- `GET /api/sources/{id}` — 来源详情

### 登录

- `POST /api/sources/{id}/captcha` — 获取验证码（返回图片路径）
- `GET /api/captcha/image/{image_name}` — 获取验证码图片
- `POST /api/sources/{id}/login` — 登录 `{verifyCode}`

### 抓取数据

- `GET /api/api-list` — 可用数据接口列表
- `POST /api/sources/{id}/fetch` — 抓取数据 `{api_path, intervalDate, userLogin, max_pages, task_name}`

### 数据查看

- `GET /api/sources/{id}/data` — 抓取任务列表
- `GET /api/sources/{id}/data/{task_id}` — 任务详情（含数据行）
- `DELETE /api/data/{task_id}` — 删除任务

## 厂商后台 API 说明

### 登录接口

```
POST https://tg-admin-api.jinyou68.com/api/auth/login_user_name
Content-Type: application/json

{
  "userName": "账号",
  "password": "密码",
  "verKey": "验证码密钥",
  "verifyCode": "验证码"
}
```

返回 `data.accessToken`，后续请求头带 `access-token: {accessToken}`。

### 数据接口

```
POST https://tg-admin-api.jinyou68.com/api/cps_info/xxx_list
Content-Type: multipart/form-data
Header: access-token: {accessToken}

参数：pageNo, pageSize, intervalDate, userLogin
```

## 注意事项

- access_token 有效期 7 天，过期后需要重新登录
- 验证码需要人工识别（4位数字）
- 数据库文件在 `data/crawler.db`，可直接用 SQLite 工具查看
- 如需添加新的数据类型，在 `crawler.py` 的 `API_LIST` 字典中添加即可
