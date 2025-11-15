# Telegram 媒体文件下载器

一个功能强大的Telegram频道媒体文件下载工具，支持多频道并发下载、断点续传、自动重试等特性。

## 主要特性

- 多频道并发下载
  - 支持同时监控多个频道
  - 异步并行下载，提高下载效率
  - 智能并发控制，避免过度占用资源，可配置最大并发下载数
  - 每个频道独立的下载进度管理和重试状态

- 智能文件管理
  - 支持视频、音频、文档等多种媒体类型下载
  - 自动过滤大小超限的文件，可配置最大文件大小
  - 文件名智能处理，避免特殊字符，确保文件系统兼容性
  - 原子写入确保文件完整性，避免下载中断导致文件损坏
  - 音频文件质量检查
    - 支持基于文件大小、比特率或时长的质量判断
    - 自动覆盖低质量文件，确保本地文件始终为最高质量版本
    - 可配置最小质量阈值，如最小文件大小、最小比特率和最小音频时长

- 灵活的重试策略
  - 采用指数退避算法，智能调整重试间隔，从1秒开始，最大不超过30分钟
  - 自动处理网络波动和连接错误，提高下载稳定性
  - 可配置最大重试次数，支持无限重试
  - 失败任务自动重试，确保下载最终完成

- 用户友好的交互
  - 交互式频道选择，方便用户管理下载源
  - 实时下载进度显示，支持tqdm进度条或日志输出模式
  - 详细的状态日志输出，便于问题排查和运行监控
  - 支持优雅退出，确保程序在中断时能保存进度并清理资源

## 环境要求

- Python 3.7+
- 必需的Python包：
  - telethon
  - tqdm
  - mutagen

## 配置说明

### 1. 环境变量

- `TGDL_DATA_DIR`: 数据存储目录，默认为 `./data`
- `TGDL_DISABLE_TQDM`: 控制下载进度显示方式
  - `false`（默认）：使用tqdm进度条显示下载进度
  - `true`：使用普通日志方式显示进度，每完成10%记录一次，适合在Docker容器等环境中使用
- `TZ`: 时区配置，默认为 `Asia/Shanghai`
  - 支持标准时区格式，如：`Asia/Shanghai`, `America/New_York`, `Europe/London` 等
- `TGDL_DISABLE_TQDM`: 是否禁用tqdm进度条，设置为`true`则禁用，默认为`false`
- `TGDL_MAX_FILE_SIZE_MB`: 单个文件的最大下载大小（MB），默认为`500`
- `TGDL_WAIT_INTERVAL_SECONDS`: 频道无新消息时，等待的秒数，默认为`300`
- `TGDL_INITIAL_RETRY_DELAY`: 首次重试的延迟时间（秒），默认为`1`
- `TGDL_MAX_RETRY_DELAY`: 最大重试延迟时间（秒），默认为`1800`
- `TGDL_MAX_RETRIES`: 最大重试次数，`0`表示无限重试，默认为`0`
- `TGDL_MAX_CONCURRENT_DOWNLOADS`: 单个频道最大并发下载数，默认为`3`
- `TGDL_BATCH_SIZE`: 每次从Telegram获取消息的批处理大小，默认为`15`
- `TGDL_PROGRESS_STEP`: 下载进度日志的步长（百分比），默认为`10`
- `TGDL_EXCLUDE_PATTERNS`: 排除包含特定关键字或匹配正则的文件名（逗号分隔）。支持两种形式：关键字（不区分大小写）与正则（以 `re:` 前缀）。
- `TGDL_RECONFIGURE`: 设置为 `1`/`true`/`yes` 时仅执行重配置，不启动下载

### 2. 配置文件

首次运行时会自动创建配置文件，需要填写：

- API ID: Telegram API ID
- API Hash: Telegram API Hash
- Phone Number: 你的Telegram手机号
- 媒体类型：需要下载的媒体类型（video/audio/document）
- 代理设置（可选）：
  - 代理类型：支持 socks5、http、mtproxy
  - 代理主机：代理服务器地址
  - 代理端口：代理服务器端口
  - 代理认证：可选的用户名和密码
  - 支持从环境变量（`HTTPS_PROXY`, `HTTP_PROXY`, `ALL_PROXY`）自动读取代理配置
- 音频质量检查（可选）：
  - 是否启用：yes/no
  - 检查方式：size（文件大小）、bitrate（比特率）、duration（时长）或 both（大小、比特率和时长）
  - 最小文件大小：启用size检查时的最小文件大小（MB），低于此大小的文件将被跳过
  - 最小比特率：启用bitrate检查时的最小比特率（kbps），低于此比特率的文件将被跳过
  - 最小音频时长：启用duration检查时的最小音频时长（秒），低于此时长的文件将被跳过
- 下载参数配置（可选）：
  - `max_file_size_mb`: 单个文件的最大下载大小（MB），默认为500MB
  - `wait_interval_seconds`: 频道无新消息时，等待的秒数，默认为300秒
  - `initial_retry_delay`: 首次重试的延迟时间（秒），默认为1秒
  - `max_retry_delay`: 最大重试延迟时间（秒），默认为1800秒（30分钟）
  - `max_retries`: 最大重试次数，0表示无限重试，默认为0
  - `max_concurrent_downloads`: 单个频道最大并发下载数，默认为3
  - `batch_size`: 每次从Telegram获取消息的批处理大小，默认为15
  - `progress_step`: 下载进度日志的步长（百分比），默认为10

### 3. 目录结构

```
data/
├── config/
│   ├── config.json         # 主配置文件
│   ├── state.json          # 运行时状态（每个频道的 last_id 持久化）
│   └── sessions/           # 会话文件
└── downloads/              # 下载文件存储
    ├── downloading/        # 临时下载目录（.part 原子写入）
    └── completed/          # 完成下载目录
```

## 使用方法

本项目支持多种运行方式，你可以根据自己的需求选择最适合的方式。

### 方式一：本地运行（推荐开发者）

1.  **安装依赖**：
    ```bash
    pip install -r requirements.txt
    ```
2.  **运行程序**：
    ```bash
    python main.py
    ```
    首次运行会提示输入 API ID、API HASH 和手机号等信息，并生成 `config.json` 文件。
3.  **选择频道**：
    程序会列出你加入的所有频道，输入要下载的频道序号（多个用逗号分隔）。
4.  **开始下载**：
    程序将开始下载指定频道中的媒体文件。

    日志中会看到增量抓取与持久化状态，例如：
    - `频道 <title> 拉取参数: min_id=<last_id>, limit=<N>`
    - `频道 <title> 候选消息 <count> 条，最高ID=<max_id>，当前持久化 last_id=<last_id>`
    - `频道 <title> 无新消息（min_id=<last_id>），结束本轮抓取`

5.  **仅重配置（不下载）**：
    - 通过参数触发：
      ```bash
      python main.py --reconfigure
      # 或简写
      python main.py -r
      ```
    - 通过环境变量触发：
      ```bash
      TGDL_RECONFIGURE=1 python main.py
      ```
    - 用途：只更新配置（如重新选择频道），完成后立即退出，不进行任何下载。

### 方式二：Docker 部署（推荐服务器部署）

本项目支持 Docker 部署，方便在不同环境中运行。

1.  **构建 Docker 镜像**：
    在项目根目录下执行：
    ```bash
    docker build -t tlgspider .
    ```
2.  **运行 Docker 容器**：
    你可以通过以下两种方式运行容器：

    a. **直接运行**：
       ```bash
       docker run -it --name tlgspider_container -v ./data:/app/data tlgspider
       ```
       - `-it`: 交互式运行，首次配置时需要输入信息。
       - `--name tlgspider_container`: 为容器指定一个名称。
       - `-v ./data:/app/data`: 将宿主机的 `./data` 目录挂载到容器内的 `/app/data`，用于持久化配置和下载文件。

    b. **使用 Docker Compose (推荐)**：
       项目提供了 `docker-compose.yml` 文件，可以更方便地管理服务。
       ```bash
       docker-compose up -d
       ```
       - `-d`: 后台运行容器。
       首次运行 `docker-compose up` 时，容器会进入交互模式，你需要输入 API ID、API HASH 和手机号等信息。配置完成后，容器将自动在后台运行。

       **配置环境变量**：
       你可以在 `docker-compose.yml` 文件中配置环境变量，例如代理设置、下载参数等，而无需修改 `config.json`。
       ```yaml
       environment:
         - TGDL_MAX_FILE_SIZE_MB=1024
         - TZ=Asia/Shanghai
         # ... 其他环境变量
       ```

       **查看日志**：
       ```bash
       docker-compose logs -f
       ```

       **停止和删除容器**：
       ```bash
       docker-compose down
       ```

## 状态持久化与增量抓取

- 持久化文件：`data/config/state.json`
  - 记录每个频道的最新处理进度 `last_id`，避免重启后重复处理旧消息。
  - 示例结构：
    ```json
    {
      "channels": {
        "123456789": { "last_id": 43521 },
        "987654321": { "last_id": 92011 }
      }
    }
    ```
- 增量抓取策略：
  - 每次抓取时按频道维度使用 `min_id=<last_id>`，只获取“比当前进度更新”的消息。
  - 运行中一旦发现更大的消息ID，立即更新并写回 `state.json`。
- 去重机制：
  - 运行期维护一个有限大小的去重队列（窗口500条），避免同一批次内重复处理。

### 重置或回滚进度
- 如果希望重新处理某个频道的历史消息：
  - 编辑 `data/config/state.json`，将对应频道的 `last_id` 调小或删除该频道条目；
  - 或直接删除整个 `state.json` 文件（将从最新开始重新建立状态）。

### 日志验证
- 正常抓取时会输出：
  - `频道 <title> 拉取参数: min_id=<last_id>, limit=<N>`
  - `频道 <title> 候选消息 <count> 条，最高ID=<max_id>，当前持久化 last_id=<last_id>`
  - 无新消息时：`频道 <title> 无新消息（min_id=<last_id>），结束本轮抓取`
- 这些日志可用于确认增量抓取是否生效，以及 `last_id` 是否正确持久化。

### 方式三：使用预构建 Docker 镜像（推荐快速部署）

如果你不想自己构建镜像，可以直接使用 Docker Hub 上预构建的镜像 `forisy/tlgspider:latest`。

1.  **拉取镜像**：
    ```bash
    docker pull forisy/tlgspider:latest
    ```
2.  **运行容器**：
    ```bash
    docker run --restart=always -d --name tlgspider \
      -v /your/local/path/config:/app/data/config \
      -v /your/local/path/downloads:/app/data/downloads \
      forisy/tlgspider:latest
    ```
    - `--restart=always`: 确保容器在 Docker 守护进程启动时自动启动，或在容器退出时重启。
    - `-v /your/local/path/config:/app/data/config`: 将宿主机的 `/your/local/path/config` 目录挂载到容器内的 `/app/data/config`，用于持久化配置。
    - `-v /your/local/path/downloads:/app/data/downloads`: 将宿主机的 `/your/local/path/downloads` 目录挂载到容器内的 `/app/data/downloads`，用于持久化下载文件。
    请将 `/your/local/path/config` 和 `/your/local/path/downloads` 替换为你实际的本地路径。

### 方式四：二进制文件运行（推荐桌面用户）

对于不熟悉 Python 环境或 Docker 的用户，可以直接下载预编译的二进制文件运行。

1.  **下载二进制文件**：
    访问项目的 [Release 页面](https://github.com/forisy/tlgspider/releases) 下载对应操作系统的最新版本。
2.  **运行**：
    下载后，直接运行可执行文件即可。首次运行同样会引导你完成配置。
    - 正常下载：
      ```powershell
      .\dist\tlgspider.exe
      ```
    - 仅重配置（不下载）：
      ```powershell
      .\dist\tlgspider.exe --reconfigure
      # 或简写
      .\dist\tlgspider.exe -r
      # 或使用环境变量
      $env:TGDL_RECONFIGURE='1'; .\dist\tlgspider.exe
      ```

## 高级特性

### 并发控制
- 异步消息获取和下载，提高效率
- 智能的并发管理，可配置每个频道的最大并发下载数
- 避免过度占用系统资源，确保程序稳定运行

### 重试机制
- 初始重试间隔：1秒
- 最大重试间隔：30分钟
- 支持无限重试（通过配置 `max_retries` 为0）
- 使用指数退避算法，每次重试后延迟时间翻倍，从1秒开始，最大不超过30分钟
- 连接成功后重置延迟时间，确保下次重试从初始延迟开始
- 智能的重试策略，自动处理网络连接错误和临时性问题
- 独立管理每个频道的重试状态，避免相互影响
- 优雅降级，避免因频繁重试导致IP被封锁或资源耗尽

### 任务恢复
- 自动检测未完成的下载任务，并在程序重启后继续下载
- 支持断点续传，从上次中断的地方继续下载，节省时间和带宽
- 异常退出后可恢复进度，确保下载数据的完整性
- 保证数据一致性，避免文件损坏或重复下载

### 错误处理
- 网络错误自动重试，提高下载成功率
- 文件损坏检测（通过原子写入确保完整性）
- 优雅处理中断信号（如Ctrl+C），确保程序安全退出并保存状态
- 详细的错误日志记录，便于用户排查问题

### 文件名排除（关键字与正则）

- 通过配置项 `download_settings.exclude_patterns` 或环境变量 `TGDL_EXCLUDE_PATTERNS` 控制。
- 支持两类规则：
  - 关键字：大小写不敏感的子串匹配，例如 `广告,promo`
  - 正则：以 `re:` 前缀编写正则表达式，例如 `re:免费|福利`, `re:(?i)trial`
- 示例（`config.json` 片段）：
  ```json
  {
    "download_settings": {
      "exclude_patterns": [
        "广告",
        "promo",
        "re:免费|福利",
        "re:(?i)trial"
      ]
    }
  }
  ```
- 行为说明：当文件名包含任一关键字或匹配任一正则规则时将跳过下载。

## 注意事项

1. 请确保有足够的存储空间
2. 需要稳定的网络连接
3. 请遵守Telegram的API使用限制
4. 建议使用配置文件管理下载选项

## 常见问题

1. 下载速度慢
   - 检查网络连接
   - 检查并发设置
   - 配置代理服务器：
     - 支持 SOCKS5、HTTP、MTProto 代理
     - 可配置代理认证信息
     - 推荐使用 SOCKS5 代理以获得更好的性能

2. 文件无法下载
   - 确认文件大小是否超限（默认500MB）
   - 检查存储空间
   - 验证媒体类型设置

3. 程序异常退出
   - 查看错误日志
   - 确认配置正确
   - 检查网络状态

## 开发计划

已完成：
- [x] 异步下载架构，支持多频道并发下载
- [x] 智能失败处理，包括网络错误和文件大小检查
- [x] 可配置的重试策略，支持指数退避和最大重试次数
- [x] 优雅的退出机制，确保数据保存和资源释放
- [x] 音频文件质量检查，支持大小、比特率和时长多维度判断
- [x] 支持代理配置，兼容多种代理类型和环境变量读取
- [x] 断点续传和任务恢复功能

计划中：
- [ ] 支持更多媒体类型
- [ ] 添加Web界面
- [ ] 优化存储策略
- [ ] 增加数据统计
- [ ] 支持自定义过滤器
- [ ] 添加下载速度限制
- [ ] 支持自定义文件命名规则
