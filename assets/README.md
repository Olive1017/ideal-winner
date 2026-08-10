# assets

放打包需要的静态资源。

目前托盘图标是 `ui/tray.py` 里用 QPainter 画的，不依赖图片文件，
所以这个目录现在是空的。

以后要换成真图标时，把 `app.ico` 放进来，然后：

```bash
pyinstaller main.py --noconsole --onefile \
  --icon assets/app.ico \
  --add-data "assets;assets"
```

代码里用 `services.config.resource_path("assets/app.ico")` 取路径，
它会自动处理 PyInstaller 解包目录 `sys._MEIPASS`，源码运行和打包后都能找到。
