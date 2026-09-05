## Windows / WSL 路径安全规范

涉及 Windows、WSL、VS Code、DSH Desktop、PowerShell、CMD、Git Bash、快捷方式、启动脚本、工作目录或文件路径时，必须遵守以下规则。

### 1. 禁止猜测路径

不得根据上下文猜测、补全、拼接或自行转换路径。

执行任何涉及路径的命令前，必须先确认：

`运行环境 / 当前 cwd / 实际目标路径 / 路径来源 / 路径是否真实存在`

未验证的路径不得用于写入、启动、删除、移动、覆盖或配置修改。

### 2. Windows 与 WSL 路径严格隔离

Windows 路径：

```text
C:\Users\...
D:\...
W:\...
```

WSL 路径：

```text
/home/...
/mnt/c/...
/mnt/d/...
/mnt/w/...
```

不得直接混用两种路径格式。

Windows 路径传入 WSL 前，必须确认对应 `/mnt/<drive>/` 挂载点真实存在。

WSL 路径传给 Windows 程序前，必须先完成明确、可验证的路径转换。

### 3. 拒绝残缺路径

禁止生成、传递或执行以下类型的路径：

```text
W\
W:/
\W
W:
```

以及：

* 缺少冒号的盘符
* 缺少目录部分的异常盘符
* 引号未闭合的路径
* 被 Shell 截断的路径
* 未验证的自动路径转换结果
* Windows / WSL 混合格式路径

发现异常路径时，必须停止当前修改，不得继续尝试执行。

### 4. Shell 边界必须显式处理

涉及空格、括号、中文、反斜杠、变量或特殊字符的路径，必须明确处理引号和转义。

不得假设以下环境具有相同解析规则：

* PowerShell
* Windows PowerShell
* CMD
* Git Bash
* WSL Bash
* VS Code Task
* Electron / Desktop App

跨 Shell 调用时，必须检查最终实际传递给目标进程的命令和参数。

### 5. 配置修改前先只读取证

修改以下内容前必须先检查当前真实配置：

* Shortcut Target
* Shortcut Start in
* Shortcut Arguments
* Windows Startup
* Registry Run
* Scheduled Tasks
* `.vscode/tasks.json`
* `.vscode/launch.json`
* Shell 启动脚本
* DSH Desktop 启动配置
* WSL 启动命令
* 环境变量
* cwd / WorkingDirectory

不得因为“看起来应该是这样”直接覆盖现有配置。

### 6. 路径错误必须追根因

遇到以下任一报错时，都按 Windows 路径 / 启动调用异常处理：

```text
Windows 找不到某文件
Windows 找不到 W\
Windows 找不到 \\ 文件。请确定拼写是否正确后再试一次，或通过单击“开始”按钮，然后单击“搜索”搜索该项目。
系统找不到指定的路径
No such file or directory
Path not found
```

其中：

`Windows 找不到 \\ 文件` 必须视为**路径被截断、反斜杠转义错误、路径前缀丢失、参数错误传递或 Windows / WSL 路径转换异常**的重点证据。

出现此类错误时，不得只搜索 `W:\` 或 `W\`，必须同时检查：

`\\ / 单独反斜杠 \ / 路径前缀丢失 / 引号截断 / 参数丢失 / Shell 转义错误 / cwd 异常 / Windows 与 WSL 路径转换错误`

必须沿真实调用链定位：

**启动者 → executable → arguments → cwd → 原始路径 → Shell / 转义过程 → 最终传给 Windows 的实际字符串**

必须明确：

**哪个进程传入了 `\\`，原本应该是什么路径，以及路径在哪一层被截断或转义成了 `\\`。**

在根因确认前，不得通过创建假目录、软链接、映射盘符、复制文件或重装软件来掩盖错误。

### 7. 只修源头

路径错误修复必须修改产生错误路径的源配置、源代码或调用参数。

禁止通过创建与错误路径匹配的目录、复制文件、软链接、额外映射盘符等方式让错误配置“碰巧可以运行”。

### 8. 修改前输出路径证据

涉及路径的实际修改前，必须先明确：

```text
运行环境：
调用入口：
当前 cwd：
源路径：
目标路径：
路径来源：
存在性验证：
路径转换方式：
```

验证通过后才能继续修改。

### 核心原则

**路径不是推理结果，而是环境事实。**

任何跨 Windows / WSL 的路径都必须先验证，再转换，再执行。

出现路径故障时，目标不是消除报错，而是找到并修复产生错误路径的源头。
