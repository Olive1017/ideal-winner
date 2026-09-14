# SDCC 订单工具

这是一个 Windows 桌面工具，用于把壳牌 LMS 导出的订单数据整理成 SDCC 可导入的格式，并支持自动准备、队列管理、重试和上传处理。

## 功能概览

- 从壳牌 LMS 导出订单原始数据
- 自动转换成 SDCC 导入格式
- 生成待上传文件队列
- 支持手动触发处理
- 支持强制重新拉取最新数据
- 失败后可重试，并保留日志和截图
- 支持托盘最小化和后台运行
- 支持定时自动准备数据

## 工作流程

1. 在应用中选择数据目录
2. 按配置定时导出壳牌订单
3. 转换为 SDCC 模板格式
4. 生成待上传文件并放入队列
5. 在“运行”页手动开始处理
6. 失败时查看日志和截图定位问题

## 项目结构

- main.py：程序入口
- core/：核心业务逻辑
  - converter.py：Excel 转换
  - shell_exporter.py：壳牌订单导出
  - uploader.py：SDCC 上传逻辑
  - models.py：数据结构与状态定义
- services/：基础服务
  - config.py：配置与目录管理
  - credentials.py：凭据存取
  - logger.py：日志记录
  - scheduler.py：定时与重试
  - autostart.py：开机自启
- ui/：PySide6 桌面界面与托盘逻辑

## 数据目录

程序需要用户指定一个数据根目录，通常包含：

- 壳牌订单/：原始壳牌订单导出
- SDCC订单/：转换后的待上传文件
- 归档/：上传成功或失败后的归档文件

## 说明

- 适用于 Windows
- 密码通过系统凭据管理器保存，不写入配置文件
- 程序会保留执行日志和失败截图
- 采用单实例运行，避免重复启动导致重复浏览器操作

## 依赖

- Python 3.10+
- PySide6
- Playwright
- pandas
- openpyxl
- APScheduler
- keyring
- requests

## 使用方式

直接运行：

```bash
python main.py